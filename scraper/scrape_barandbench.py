"""
Weekly (well, every-2-days) scraper for Bar & Bench's Dealstreet section.

Fetches the listing page, finds article URLs not already in seen_urls.json,
fetches each new article, extracts headline/firms/people/transaction info,
and appends results to barandbench_deals.json in the same schema as
structured_deals.json entries so build_directory.py can merge both sources
untouched.

Person/role/firm extraction is based on inspecting ~20 real Bar & Bench
articles directly (not guessed): the dominant, highly reliable format is
literal "Name (Role)" pairs, e.g. "Hardik Bhatia (Partner), Nishant Chris
Mathews (Principal Associate)". A secondary format lists several people
under one plural role marker with no individual parens, e.g. "Associates
Archit Jain, Arikta Shetty, Janhavi Deshmukh, Harsha Menon, Akshat Sharma
and Sajal Soni." That second format is handled with plain word-by-word
scanning instead of a bigger/fragile regex, since name-list length and
separators vary unpredictably.

Each article typically has one paragraph naming the advising firm ("Khaitan
& Co advised Arboreal on this fundraise.") immediately followed by that
firm's team-credit paragraph(s). We track this "current firm" per
paragraph so each extracted person gets their real firm, not a deal-wide
guess -- this fixes the earlier version, which only had one firm list per
deal and could misattribute people from a multi-firm deal.
"""
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

SCRAPER_DIR = Path(__file__).parent
BASE = SCRAPER_DIR.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(SCRAPER_DIR))
from build_directory import canonical_firm, is_personnel_move, normalize_name  # noqa: E402
import ner_supplement  # noqa: E402

LISTING_URL = "https://www.barandbench.com/dealstreet"
SITE_ROOT = "https://www.barandbench.com"
SEEN_PATH = SCRAPER_DIR / "seen_urls.json"
OUT_PATH = BASE / "barandbench_deals.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; legal-directory-bot/1.0; +https://github.com/)"
}

# Longest/most-specific phrases first, since regex alternation is
# first-match, not longest-match -- "Associate Partner" must be tried
# before "Partner" and "Associate" or it'd get chopped to just "Partner".
ROLE_WORDS = [
    "Joint Managing Partner",
    "Managing Partner",
    "Senior Partner",
    "Associate Partner",
    "Principal Associate",
    "Senior Associate",
    "Of Counsel",
    "Counsel",
    "Partner",
    "Associate",
]

ROLE_ALTERNATION = "|".join(re.escape(r) for r in ROLE_WORDS)
# Indian/international names commonly include bare middle initials
# ("Ujwala K Adikey", "Aditya J Nair") and lowercase particles ("Chad de
# Souza") -- both must be allowed as inner words, or the whole match breaks
# at that word and only the trailing surname gets captured.
_NAME_FIRST_WORD = r"[A-Z][A-Za-z’'-]*"
_NAME_INNER_WORD = r"(?:[A-Z][A-Za-z’'-]*|de|van|bin|al|la|von|der|del)"
# require >=2 name words -- a single capitalized word before "(Role)" is
# almost always a stray leftover from a nickname-in-parens (see
# _strip_nickname_parens), not a full name.
NAME_PATTERN = rf"{_NAME_FIRST_WORD}(?:\s+{_NAME_INNER_WORD}){{1,4}}"

# Primary, high-confidence extractor: "Name (Role)" or "Name (Role, extra
# title text)" -- the extra-title suffix (e.g. "Partner, Regional Co-Head –
# Capital Markets – West") is discarded, we only keep the matched role word.
PAREN_ROLE_RE = re.compile(
    rf"({NAME_PATTERN})\s*\(({ROLE_ALTERNATION})(?:,[^)]*)?\)"
)

# Mid-name nicknames like "Kyungwon (Won) Lee" break paren-role matching --
# strip any single-word parenthetical that isn't one of our role words
# before running extraction.
_NICKNAME_PAREN_RE = re.compile(r"\s\(([A-Za-z]+)\)")


def _strip_nickname_parens(text):
    return _NICKNAME_PAREN_RE.sub(
        lambda m: m.group(0) if m.group(1) in ROLE_WORDS else "", text
    )

# Plural role markers used when several names share one un-parenthesized
# role mention, e.g. "Associates Archit Jain, Arikta Shetty ... and Sajal
# Soni." Matched via plain word scanning below, not a single regex, since
# the name-list length is unbounded and regex would get unreadable fast.
PLURAL_ROLE_MARKERS = {
    "Senior Partners": "Senior Partner",
    "Associate Partners": "Associate Partner",
    "Principal Associates": "Principal Associate",
    "Senior Associates": "Senior Associate",
    "Counsels": "Counsel",
    "Associates": "Associate",
    "Partners": "Partner",
}
# longest marker (by word count) checked first at each position
PLURAL_MARKERS_BY_LENGTH = sorted(
    PLURAL_ROLE_MARKERS, key=lambda k: -len(k.split())
)

# Firm-attribution sentence: "<Firm> advised <client> on ..." /
# "<Firm> acted as ..." / "<Firm> represented ...". Kept deliberately
# simple -- one line, literal keywords -- since it only needs to catch the
# firm name at the *start* of a sentence, which is Bar & Bench's house style.
FIRM_CONTEXT_RE = re.compile(
    r"^([A-Z][\w &.,’'-]*?)\s+"
    r"(advised|advises|is advising|represented|represents|is representing|"
    r"acted for|acted as|acts for|acts as)\b"
)

# Headline verb pattern: "<firms> <verb> <client> <deal desc>"
HEADLINE_VERB = re.compile(
    r"^(?P<firms>.+?)\s+(?:act(?:s)? on|advises|advise|assists|assist|represents|represent)\s+(?P<rest>.+)$",
    re.I,
)

TXN_KEYWORDS = (
    "IPO", "QIP", "Series A", "Series B", "Series C", "Series D", "Series E",
    "acquisition", "stake acquisition", "merger", "amalgamation", "demerger",
    "buyout", "divestment", "financing", "joint venture", "fundraise",
    "investment", "restructuring",
)


def load_seen():
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text()))
    return set()


def save_seen(seen):
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2))


def load_existing_deals():
    if OUT_PATH.exists():
        return json.loads(OUT_PATH.read_text())
    return []


def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _slug(url):
    """Last path segment -- Bar & Bench restructured URLs at some point
    (bare /dealstreet/<slug> vs newer /law-firms/dealstreet/<slug>) and the
    same article can exist at both paths. Dedupe on this, not the full
    URL, or the same deal gets scraped and counted twice."""
    return url.rstrip("/").rsplit("/", 1)[-1].lower()


def _normalize_url(url):
    """Strip query string, fragment, and trailing slash -- two links to
    the exact same page can differ by a tracking param (?utm_source=...)
    or a trailing '/' and still be "the same URL" for dedup purposes."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def _normalize_headline(headline):
    """Lowercase, collapsed-whitespace headline text -- catches the case
    where the same article gets re-listed with an identical headline under
    a completely different URL/slug (e.g. a manual re-publish)."""
    return re.sub(r"\s+", " ", headline).strip().lower()


def _fingerprint_text(text):
    normalized = re.sub(r"\s+", " ", text).strip().lower()[:500]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _content_fingerprint(paragraphs):
    """Hash of the first ~500 chars of normalized body text -- the last
    line of defense: if URL, slug, and headline all differ but the actual
    article content is identical, it's still the same real-world article
    and must not be double-counted as two deals."""
    return _fingerprint_text(" ".join(paragraphs))


class DedupIndex:
    """Tracks every signal we've seen so far (normalized URL, slug,
    normalized headline, content fingerprint) and flags a new candidate as
    a duplicate if it matches on ANY single signal -- these are independent,
    cheap-to-spoof-individually signals (a site restructure changes the
    URL, a re-publish can change the headline, a redirect can change the
    slug), so requiring all of them to agree would under-protect; matching
    on any one is what actually catches "this is the same real article"."""

    def __init__(self):
        self.urls = set()
        self.slugs = set()
        self.headlines = set()
        self.fingerprints = set()

    def add_known(self, url, headline):
        self.urls.add(_normalize_url(url))
        self.slugs.add(_slug(url))
        self.headlines.add(_normalize_headline(headline))

    def add_fingerprint(self, fingerprint):
        self.fingerprints.add(fingerprint)

    def is_known_link(self, url, headline):
        return (
            _normalize_url(url) in self.urls
            or _slug(url) in self.slugs
            or _normalize_headline(headline) in self.headlines
        )

    def is_known_fingerprint(self, fingerprint):
        return fingerprint in self.fingerprints

    @classmethod
    def from_deals(cls, deals):
        index = cls()
        for d in deals:
            index.add_known(d["url"], d["headline"])
            if d.get("snippet"):
                index.add_fingerprint(_fingerprint_text(d["snippet"]))
        return index


def find_article_links(listing_html):
    soup = BeautifulSoup(listing_html, "lxml")
    links = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(SITE_ROOT, a["href"])
        if not href.startswith(SITE_ROOT):
            continue
        text = a.get_text(strip=True)
        if len(text) < 20:
            continue
        if "/dealstreet" not in href:
            continue
        links[href] = text
    return links


def split_firm_list(raw):
    parts = re.split(r",\s*|\s+and\s+", raw.strip())
    firms = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        cname = canonical_firm(p)
        if cname:
            firms.append(cname)
    return firms


def parse_headline(headline):
    m = HEADLINE_VERB.match(headline)
    if not m:
        return {"law_firms": [], "client": None, "transaction_types": []}
    firms_raw = m.group("firms")
    # some headlines carry a subtitle before the firm list, e.g. "LIC Stake
    # Sale: Dentons Link Legal, Trilegal ..." -- drop everything up to and
    # including the colon or the subtitle leaks in as a fake "firm"
    if ":" in firms_raw:
        firms_raw = firms_raw.rsplit(":", 1)[-1].strip()
    firms = split_firm_list(firms_raw)
    rest = m.group("rest")
    client = rest.split(" on ")[0].strip() if " on " in rest else rest.strip()
    # trim trailing deal-size/description noise: "Arboreal ₹230 crore Series
    # A fundraise" -> "Arboreal" (cut at first currency symbol or digit)
    client = re.split(r"[₹$]|\s+\d", client)[0].strip()
    txn_types = [k for k in TXN_KEYWORDS if k.lower() in headline.lower()]
    return {"law_firms": firms, "client": client, "transaction_types": txn_types}


# Personnel-move headlines don't have a "<firms> act on/advise <client>"
# shape, so parse_headline()'s HEADLINE_VERB never matches them and there's
# no headline-declared firm list to snap body-text mentions onto. These
# patterns pull name/firm/role straight from the headline instead, since
# Bar & Bench's headlines for this category are themselves quite
# structured. Best-effort by design -- a personnel-move headline that
# doesn't match any of these still gets correctly excluded from the deal
# count (via is_personnel_move in build_directory.py); it just won't have
# a firm/role attached, which is a safe (if less useful) fallback rather
# than a wrong one.
PERSONNEL_HEADLINE_PATTERNS = [
    # "X joins Y as Z [in City]"
    re.compile(
        r"^(?P<name>[A-Z][\w .’'-]+?)\s+(?:re-?)?joins?\s+"
        r"(?P<firm>[A-Z][\w &.,’'-]+?)\s+as\s+(?P<role>[A-Za-z ]+?)"
        r"(?:\s+in\s+[A-Za-z ]+)?$",
        re.I,
    ),
    # "X quits Y" / "X resigns from Y"
    re.compile(
        r"^(?P<name>[A-Z][\w .’'-]+?)\s+(?:quits|resigns(?: from)?)\s+"
        r"(?P<firm>[A-Z][\w &.,’'-]+)$",
        re.I,
    ),
    # "X elevated/promoted to Y at Z"
    re.compile(
        r"^(?P<name>[A-Z][\w .’'-]+?)\s+(?:elevated|promoted)\s+(?:to|as)\s+"
        r"(?P<role>[A-Za-z ]+?)\s+at\s+(?P<firm>[A-Z][\w &.,’'-]+)$",
        re.I,
    ),
    # "Former <firm> lawyer X sets up [his/her] [own] chambers"
    re.compile(
        r"^(?:Former\s+(?P<prev_firm>[A-Z][\w &.,’'-]+?)\s+lawyer\s+)?"
        r"(?P<name>[A-Z][\w .’'-]+?)\s+sets?\s+up\s+(?:his|her|their|own)?\s*"
        r"(?:own\s+)?chambers",
        re.I,
    ),
]


def parse_personnel_move_people(headline):
    """Best-effort name/firm/role extraction for a headline already
    classified as a personnel move (see build_directory.is_personnel_move).
    Returns [] if the headline doesn't match a known shape -- the article
    is still correctly excluded from the deal count either way."""
    for pat in PERSONNEL_HEADLINE_PATTERNS:
        m = pat.match(headline.strip())
        if not m:
            continue
        gd = m.groupdict()
        name = normalize_name(gd.get("name") or "")
        if not name or len(name.split()) < 2:
            continue
        role = (gd.get("role") or "").strip().title() or None
        firm_raw = gd.get("firm")
        if firm_raw:
            firm = canonical_firm(firm_raw)
        elif "chambers" in headline.lower():
            firm = canonical_firm(f"Chambers of {name}")
            role = role or "Advocate"
        else:
            firm = None
        return [{"name": name, "role": role, "firm": firm}]
    return []


def extract_paren_credits(paragraph):
    return [(m.group(1).strip(), m.group(2)) for m in PAREN_ROLE_RE.finditer(paragraph)]


_NAME_PARTICLES = {"de", "van", "bin", "al", "la", "von", "der", "del"}


def _is_name_word(word):
    bare = word.strip(".").replace("-", "")
    if not bare.isalpha():
        return False
    return bare[:1].isupper() or bare.lower() in _NAME_PARTICLES


def extract_plural_list_credits(paragraph):
    """Plain word-by-word scan (no regex) for 'Associates A, B and C' style
    credits, where several names share one plural role marker."""
    tokens = paragraph.replace(",", " , ").split()
    credits = []
    i = 0
    while i < len(tokens):
        matched_marker = None
        for marker in PLURAL_MARKERS_BY_LENGTH:
            marker_words = marker.split()
            if tokens[i:i + len(marker_words)] == marker_words:
                matched_marker = marker
                i += len(marker_words)
                break
        if not matched_marker:
            i += 1
            continue
        role = PLURAL_ROLE_MARKERS[matched_marker]
        names = []
        current = []
        while i < len(tokens):
            w = tokens[i]
            if w in (",", "and"):
                if current:
                    names.append(" ".join(current))
                    current = []
                i += 1
                continue
            if _is_name_word(w):
                sentence_ended = w.endswith(".")
                current.append(w.rstrip("."))
                i += 1
                if sentence_ended:
                    # e.g. "... and Arshan Kazi." -- stop here, don't let
                    # the next sentence's capitalized first word ("The",
                    # "Trilegal", ...) bleed into this name
                    break
                continue
            break
        if current:
            names.append(" ".join(current))
        for name in names:
            if len(name.split()) >= 2:
                credits.append((name, role))
    return credits


def extract_firm_context(paragraph, known_firms=()):
    """Find a firm-attribution sentence and snap it onto one of the deal's
    own headline-declared firms.

    The raw regex match is not trustworthy on its own -- it can include
    trailing junk ("Cyril Amarchand Mangaldas also", "Fox Mandal &
    Associates has") or run on into an unrelated sentence ("JSA ... secured
    the CCI approval for this merger. The Firm"), or pick up something
    that isn't a firm at all ("His chambers", "Independent Chambers", "US
    law firms", a client's in-house team). A headline like "Khaitan & Co,
    JSA act on ..." already gives us the deal's real, clean firm list, so
    body-text mentions are only trusted when they can be matched back onto
    it. If the match is ambiguous (contains more than one known firm, e.g.
    "AZB & Partners and Duane Morris & Selvam" in one sentence) or matches
    none of them, it's discarded rather than stored as a new "firm".
    """
    m = FIRM_CONTEXT_RE.match(paragraph.strip())
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.lower() in ("the firm", "the firm also"):
        return None
    candidate = (canonical_firm(raw) or "").lower()
    if not candidate:
        return None
    matches = {
        hf for hf in known_firms
        if hf.lower() in candidate or candidate in hf.lower()
    }
    if len(matches) == 1:
        return next(iter(matches))
    return None


def extract_people(paragraphs, fallback_firm, known_firms=()):
    seen = set()
    people = []
    para_firms = []  # (cleaned_paragraph, firm_in_effect) -- reused below
    current_firm = fallback_firm
    for raw_para in paragraphs:
        para = _strip_nickname_parens(raw_para)
        context_firm = extract_firm_context(para, known_firms)
        if context_firm:
            current_firm = context_firm
        para_firms.append((para, current_firm))

        credits = extract_paren_credits(para) + extract_plural_list_credits(para)
        for name, role in credits:
            key = (name.lower(), role, current_firm)
            if key in seen:
                continue
            seen.add(key)
            people.append({"name": name, "role": role, "firm": current_firm})

    # Secondary, precision-gated NER pass (see ner_supplement.py) -- only
    # ever fills genuine gaps the regex above missed. Every candidate must
    # independently satisfy: spaCy PERSON tag, a full "(Role)" match, a
    # firm that resolves to one of this deal's own headline firms, and not
    # be a fragment of a name already found above. Silently contributes
    # nothing if spaCy isn't installed.
    known_names_lower = {p["name"].lower() for p in people}
    known_firms_set = set(known_firms)
    for para, firm in para_firms:
        for extra in ner_supplement.supplement_credits(
            para, firm, known_firms_set, ROLE_WORDS, known_names_lower
        ):
            key = (extra["name"].lower(), extra["role"], extra["firm"])
            if key in seen:
                continue
            seen.add(key)
            known_names_lower.add(extra["name"].lower())
            people.append(extra)

    return people


def parse_article(url, headline):
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup.find(attrs={"itemprop": "articleBody"}) or soup
    paragraphs = [p.get_text(" ", strip=True) for p in article.find_all("p")]
    snippet = " ".join(paragraphs)[:500]

    if is_personnel_move(headline):
        # not a deal -- build_directory.py excludes these from the deals
        # table/count regardless, but extract what we can about the
        # person's (now-current) firm/role directly from the headline,
        # since there's no "<firms> act on <client>" structure to parse
        people = parse_personnel_move_people(headline)
        law_firms = sorted({p["firm"] for p in people if p.get("firm")})
        return {
            "headline": headline,
            "client": None,
            "source": "Bar & Bench",
            "url": url,
            "snippet": snippet,
            "law_firms": law_firms,
            "transaction_types": [],
            "people": people,
        }

    parsed = parse_headline(headline)
    fallback_firm = parsed["law_firms"][0] if parsed["law_firms"] else None
    people = extract_people(paragraphs, fallback_firm, known_firms=parsed["law_firms"])

    return {
        "headline": headline,
        "client": parsed["client"],
        "source": "Bar & Bench",
        "url": url,
        "snippet": snippet,
        "law_firms": parsed["law_firms"],
        "transaction_types": parsed["transaction_types"],
        "people": people,
    }


def reparse_all():
    """Re-fetch and re-parse every already-scraped URL with the current
    extraction logic, replacing barandbench_deals.json in place. Use this
    whenever extraction rules change -- the seen_urls.json checkpoint (and
    thus which articles have been scraped at all) is untouched, only the
    quality of already-scraped entries improves.

    Also drops duplicates found via DedupIndex (URL / slug / headline /
    content fingerprint) -- keeps the first occurrence of each. Bar &
    Bench's URL restructuring caused some articles to be scraped twice
    under different paths, and this catches the broader duplicate classes
    beyond just that one case (see DedupIndex's docstring)."""
    existing = load_existing_deals()
    index = DedupIndex()
    to_parse = []
    for old in existing:
        if index.is_known_link(old["url"], old["headline"]):
            continue
        fp = _fingerprint_text(old["snippet"]) if old.get("snippet") else None
        if fp and index.is_known_fingerprint(fp):
            continue
        index.add_known(old["url"], old["headline"])
        if fp:
            index.add_fingerprint(fp)
        to_parse.append(old)
    dropped = len(existing) - len(to_parse)
    if dropped:
        print(f"dropping {dropped} duplicate deal(s)")

    print(f"reparsing {len(to_parse)} existing deals...")
    deals = []
    for i, old in enumerate(to_parse, 1):
        try:
            deal = parse_article(old["url"], old["headline"])
        except Exception as e:
            print(f"failed to reparse {old['url']}: {e}")
            deals.append(old)
            continue
        deals.append(deal)
        if i % 10 == 0:
            print(f"  {i}/{len(to_parse)}")
    OUT_PATH.write_text(json.dumps(deals, indent=2, ensure_ascii=False))
    print(f"reparsed {len(deals)} deals")


def main():
    if "--reparse-all" in sys.argv:
        reparse_all()
        return

    seen = load_seen()
    deals = load_existing_deals()
    index = DedupIndex.from_deals(deals)
    for u in seen:
        # a URL can be "seen" (recorded so we never re-fetch it) without a
        # matching deals[] entry, e.g. it 404'd or was dropped as a
        # duplicate previously -- still register it so we don't re-add it
        if not any(d["url"] == u for d in deals):
            index.urls.add(_normalize_url(u))
            index.slugs.add(_slug(u))

    listing_html = fetch(LISTING_URL)
    links = find_article_links(listing_html)
    new_links = {
        url: text for url, text in links.items()
        if url not in seen and not index.is_known_link(url, text)
    }

    print(f"found {len(links)} article links, {len(new_links)} new")

    added = 0
    skipped_duplicates = 0
    for url, headline in new_links.items():
        try:
            deal = parse_article(url, headline)
        except Exception as e:
            print(f"failed to parse {url}: {e}")
            continue

        fp = _fingerprint_text(deal["snippet"]) if deal.get("snippet") else None
        if fp and index.is_known_fingerprint(fp):
            print(f"skipping content-duplicate: {url}")
            seen.add(url)
            skipped_duplicates += 1
            continue

        index.add_known(url, headline)
        if fp:
            index.add_fingerprint(fp)
        deals.append(deal)
        seen.add(url)
        added += 1

    OUT_PATH.write_text(json.dumps(deals, indent=2, ensure_ascii=False))
    save_seen(seen)
    print(
        f"added {added} new deals ({skipped_duplicates} content-duplicates skipped); "
        f"{len(deals)} total in {OUT_PATH.name}"
    )


if __name__ == "__main__":
    main()
