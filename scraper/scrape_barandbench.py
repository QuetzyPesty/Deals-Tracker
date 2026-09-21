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
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

SCRAPER_DIR = Path(__file__).parent
BASE = SCRAPER_DIR.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(SCRAPER_DIR))
from build_directory import (  # noqa: E402
    FIRM_ALIASES,
    KNOWN_FIRMS,
    canonical_firm,
    is_personnel_move,
    normalize_name,
)
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
    rf"({NAME_PATTERN})\s*\(({ROLE_ALTERNATION})(?:(?:[,;]|\s+and\s)[^)]*)?\)"
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


# Bar & Bench is a publisher, not an API. A reparse walks every article we
# have ever seen, so without a pause that is a couple of hundred requests as
# fast as the network allows. One request at a time, with a gap.
FETCH_DELAY_SECONDS = float(os.environ.get("BB_FETCH_DELAY", "1.0"))
_last_fetch = 0.0


def fetch(url):
    global _last_fetch
    wait = FETCH_DELAY_SECONDS - (time.monotonic() - _last_fetch)
    if wait > 0:
        time.sleep(wait)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    _last_fetch = time.monotonic()
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
    # Some headlines carry a subtitle before the firm list, e.g. "LIC Stake
    # Sale: Dentons Link Legal, Trilegal ..." -- drop everything up to and
    # including the colon or the subtitle leaks in as a fake "firm". BUT at
    # least one real firm name itself contains a colon ("Samvād: Partners"),
    # so only strip when a comma or " and " follows the colon -- that's the
    # subtitle-then-firm-LIST pattern; a single firm name with an internal
    # colon has neither.
    if ":" in firms_raw:
        before, after = firms_raw.rsplit(":", 1)
        if "," in after or " and " in after.lower():
            firms_raw = after.strip()
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


# Words that make an otherwise-matched "name" span untrustworthy: quantity
# words ("Two lawyers join...") and the conjunction "and" (a two-person
# headline like "Anchal Dhir and Shubham Rastogi join..." would otherwise
# have both people's names captured as one garbled fake compound name --
# found via adversarial testing, not a hypothetical).
_PERSONNEL_NAME_REJECT_WORDS = {
    "and", "two", "three", "four", "five", "six", "seven", "eight",
    "several", "multiple", "many", "some", "few", "a", "the", "lawyers",
    "partners", "associates", "advocates", "counsels",
}


def _looks_like_real_name(name):
    words = name.split()
    if len(words) < 2:
        return False
    if any(w.lower() in _PERSONNEL_NAME_REJECT_WORDS for w in words):
        return False
    for w in words:
        bare = w.strip(".").replace("-", "")
        if not bare:
            return False
        # a real name's words are capitalized/all-caps, except for known
        # lowercase particles ("de", "van", ...) -- anything else in
        # lowercase (like "lawyers" or "join") means we over-captured a
        # sentence fragment, not an actual name
        if bare[0].islower() and bare.lower() not in _NAME_PARTICLES:
            return False
    return True


def parse_personnel_move_people(headline):
    """Best-effort name/firm/role extraction for a headline already
    classified as a personnel move (see build_directory.is_personnel_move).
    Returns [] if the headline doesn't match a known shape, or if the
    matched "name" fails _looks_like_real_name() -- e.g. a multi-person
    headline ("X and Y join...") or a quantity-word headline ("Two
    lawyers join..."), both of which this best-effort parser isn't
    sophisticated enough to split correctly, so it correctly abstains
    rather than fabricating a single garbled/wrong person."""
    for pat in PERSONNEL_HEADLINE_PATTERNS:
        m = pat.match(headline.strip())
        if not m:
            continue
        gd = m.groupdict()
        name = normalize_name(gd.get("name") or "")
        if not _looks_like_real_name(name):
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



# --- firm hand-over detection ------------------------------------------------
# Bar & Bench credits each firm's team in its own block, and the block opens by
# naming the firm -- but the verb after the name varies from article to article
# ("advised", "acted for", "served as international legal counsel to", "was
# instructed by" ...). Matching a fixed verb list silently lost every firm the
# newer template introduced with a verb we had not listed, so the previous
# firm's label was carried onto the next firm's lawyers. So the hand-over is
# keyed on the thing that never changes -- a firm we KNOW opening the paragraph
# -- and the verb is ignored.
_ALIAS_TEXTS = sorted(
    {a for a in FIRM_ALIASES} | {v.lower() for v in FIRM_ALIASES.values()},
    key=len, reverse=True,
)
# Raw spellings Bar & Bench uses that differ from our aliases only by "and"/"&".
_ALIAS_TEXTS = sorted(
    set(_ALIAS_TEXTS) | {a.replace("&", "and") for a in _ALIAS_TEXTS if "&" in a},
    key=len, reverse=True,
)


def leading_known_firm(paragraph):
    """Canonical firm if the paragraph OPENS with a firm from our corpus."""
    text = paragraph.strip()
    low = text.lower()
    for alias in _ALIAS_TEXTS:
        if not low.startswith(alias):
            continue
        rest = text[len(alias):]
        if rest and (rest[0].isalnum() or rest[0] in "-_"):
            continue  # "sam" inside "Samuel", "cam" inside "Cameron"
        # short abbreviations must be written as abbreviations (SAM, CAM,
        # TT&A), not be ordinary capitalised words at a sentence start
        if len(alias) <= 4 and text[:len(alias)] != text[:len(alias)].upper():
            continue
        return canonical_firm(alias)
    return None


_NOT_A_FIRM_LEAD = {
    "the", "a", "an", "this", "these", "that", "advice", "tax", "team",
    "transaction", "deal", "in", "with", "for", "as", "it", "its", "his", "her",
}
_NOT_A_FIRM_WORDS = {"was", "were", "is", "are", "been", "being", "by", "the", "on", "who"}


def _looks_like_firm_name(raw):
    """A sentence subject that could plausibly be a firm name. Used only to
    decide whether an UNRESOLVED opening subject should end the current
    firm's block. "The tax aspects of the transaction were advised by ..."
    is a sentence about the deal, not a change of firm, and must not wipe the
    firm we are tracking."""
    words = raw.split()
    if not words or len(words) > 7:
        return False
    if words[0].lower() in _NOT_A_FIRM_LEAD:
        return False
    return not any(w.lower() in _NOT_A_FIRM_WORDS for w in words)


def _is_firm_not_person(name):
    """A credited "name" that is really a firm: exact corpus match, opens with
    a corpus firm, or is the truncated front of one ("Duane Morris" for
    "Duane Morris & Selvam"). Wrongly dropping a person is a miss; keeping a
    firm as a person is a hallucination."""
    low = name.lower()
    return (low in KNOWN_FIRMS or bool(leading_known_firm(name))
            or any(k.startswith(low + " ") for k in KNOWN_FIRMS))


def discover_corpus_firms(paragraphs, law_firms):
    """Corpus firms that open a paragraph describing their role in the deal
    but are missing from the deal's firm list (e.g. the international counsel
    a headline leaves out). Returns law_firms extended, order preserved."""
    cue = re.compile(r"\b(advis|act(ed|s|ing)|represent|counsel|instruct|assist)", re.I)
    out = list(law_firms)
    for para in paragraphs:
        f = leading_known_firm(para)
        if f and f not in out and cue.search(para[:200]):
            out.append(f)
    return out


def _drop_fragment_firms(firms):
    """"Cyril" left over from a headline is a prefix of "Cyril Amarchand
    Mangaldas" -- a truncated duplicate, not another firm."""
    return [
        f for f in firms
        if not any(g != f and g.lower().startswith(f.lower() + " ") for g in firms)
    ]


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
        lead_firm = leading_known_firm(para)
        context_firm = lead_firm or extract_firm_context(para, known_firms)
        if context_firm:
            current_firm = context_firm
        else:
            m = FIRM_CONTEXT_RE.match(para.strip())
            if (m and not para.strip().lower().startswith("the firm")
                    and _looks_like_firm_name(m.group(1).strip())):
                # This paragraph hands over to a firm we could not resolve. Carrying
                # the previous firm forward is how TT&A's three lawyers ended up
                # labelled JSA: a confident wrong answer, which is worse than none.
                # Drop the context instead and let them come out unattributed.
                current_firm = None
        para_firms.append((para, current_firm))

        credits = extract_paren_credits(para) + extract_plural_list_credits(para)
        for name, role in credits:
            # a firm name sitting next to a role word ("Cyril Amarchand
            # Mangaldas (Partner)", crediting the firm itself rather than
            # a lawyer at it) would otherwise be captured as a fake person
            # -- reject anything that matches our own known-firm corpus
            if _is_firm_not_person(name):
                continue
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


# --- firm extraction from the page's own entity tags -------------------------
# Bar & Bench tags each article with /topic/ links -- an editorially curated
# entity list. It is strictly better than the headline: the Adani article tags
# /topic/tta even though TT&A never appears in the headline, which is exactly
# the firm the headline-only approach lost.
#
# The list mixes firms, lawyers and clients, so it cannot be used raw. A topic
# is treated as a firm only when it ALSO opens a body attribution sentence
# ("X advised Y on this transaction") -- two independent signals agreeing.
# Clients never open one; nor do individual lawyers. Across five test articles
# that rule found 16 of 16 firms with no false positives, and it needs no
# whitelist, which is what stops this breaking again the next time a boutique
# nobody has heard of appears.

TOPIC_HREF_RE = re.compile(r'href="/topic/([^"/]+)"')
# Trailing filler the attribution regex drags in: "River Law also advised ...".
FIRM_TAIL_RE = re.compile(r"\s+(also|has|have|had|is|was|were|and)$", re.I)


def topic_slugs(html):
    return sorted(set(TOPIC_HREF_RE.findall(html)))


def _slugify_firm(name):
    """Match Bar & Bench's own slug convention.

    Theirs DROPS the ampersand rather than expanding it: "Khaitan & Co" is
    khaitan-co and "TT&A" is tta. Expanding "&" to "and" here silently breaks
    the match for every firm with an ampersand in its name, which is most of
    the big ones.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", "")).strip("-")


def _topic_match(name, topics):
    """The topic slug that names this firm, if any.

    Matched loosely in both directions because the body and the tag disagree
    on formality: the body says "Pinac", the tag says
    pinac-advocates-and-solicitors; the body says "JSA Advocates and
    Solicitors", the tag says jsa.
    """
    sl = _slugify_firm(name)
    if not sl:
        return None
    sq = sl.replace("-", "")
    for t in topics:
        tq = t.replace("-", "")
        if t == sl or t.startswith(sl + "-") or sl.startswith(t + "-"):
            return t
        if tq == sq or tq.startswith(sq) or sq.startswith(tq):
            return t
    return None


def body_attribution_firms(paragraphs):
    """Names that open a firm-attribution sentence, in document order."""
    out = []
    for para in paragraphs:
        m = FIRM_CONTEXT_RE.match(para.strip())
        if not m:
            continue
        raw = FIRM_TAIL_RE.sub("", m.group(1).strip())
        if not raw or raw.lower().startswith("the firm"):
            continue
        if raw not in out:
            out.append(raw)
    return out


def firms_from_topics(html, paragraphs):
    """Firms confirmed by BOTH the page's topic tags and a body attribution
    sentence. Returns canonical names, in the order the article introduces
    them, so firms_from_topics()[0] is the lead firm."""
    topics = topic_slugs(html)
    if not topics:
        return []
    firms = []
    for raw in body_attribution_firms(paragraphs):
        if not _topic_match(raw, topics):
            continue
        cname = canonical_firm(raw)
        if cname and cname not in firms:
            firms.append(cname)
    return firms


# --- deal size ---------------------------------------------------------------
# 59% of Dealstreet articles state a figure, nearly always in the headline
# ("... on ₹9,825 crore ...", "... for $206 million"). Three currencies and
# four magnitude words cover essentially all of it.

CURRENCY_SYMBOL = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP"}
MAGNITUDE = {
    "crore": 10_000_000, "cr": 10_000_000,
    "lakh": 100_000, "lac": 100_000,
    "million": 1_000_000, "mn": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000,
}

# Indicative rates, deliberately hard-coded rather than fetched. A scheduled
# job that looked up live FX would silently restate the value of every deal
# already recorded, and two runs of the same article would disagree. These
# exist ONLY to order deals against each other and to answer range filters --
# the figure shown to a reader is always the original currency, unconverted.
FX_TO_INR_ASOF = "2026-09-01"
FX_TO_INR = {"INR": 1.0, "USD": 88.0, "EUR": 96.0, "GBP": 112.0}

_APPROX = r"(?:~|about|approx\w*|around|over|up\s?to|more\s+than|nearly|almost|upwards\s+of)\s*"
MONEY_RE = re.compile(
    rf"(?P<approx>{_APPROX})?"
    r"(?P<cur>[₹$€£])\s?(?P<amt>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>crore|cr|lakh|lac|million|mn|billion|bn)?"
    r"(?P<plus>\s*\+)?",
    re.I,
)

# A body figure is only trusted when its sentence is about the transaction --
# otherwise it can be a valuation, a revenue line or last year's raise.
DEAL_SENTENCE = re.compile(
    r"\b(acqui|invest|rais|fundrais|fund-rais|issu|subscri|sell|sale|sold|buy|purchas|"
    r"merg|divest|stake|transact|deal|IPO|QIP|placement|financ|loan|facilit)",
    re.I,
)


def parse_money(text):
    """First money-shaped figure in `text`, normalised. None if there isn't one."""
    m = MONEY_RE.search(text or "")
    if not m:
        return None
    currency = CURRENCY_SYMBOL.get(m.group("cur"))
    if not currency:
        return None
    try:
        amount = float(m.group("amt").replace(",", ""))
    except ValueError:
        return None
    unit = (m.group("unit") or "").lower()
    amount *= MAGNITUDE.get(unit, 1)
    if amount <= 0:
        return None
    return {
        "raw": m.group(0).strip(),
        "currency": currency,
        # base units of the stated currency: rupees, dollars, euros
        "amount": int(round(amount)),
        # a single comparable number, for ordering only -- see FX_TO_INR
        "amount_inr": int(round(amount * FX_TO_INR.get(currency, 1.0))),
        "approx": bool(m.group("approx") or m.group("plus")),
        "fx_asof": FX_TO_INR_ASOF if currency != "INR" else None,
    }


def extract_deal_value(headline, paragraphs):
    """Deal size, preferring the headline.

    The headline figure is the one an editor chose to describe the deal, and
    it is unambiguous. The body is only consulted when the headline has none,
    and then only from a sentence that is actually about the transaction --
    the Adani article, for instance, restates its ₹9,825 crore headline figure
    as "(~$1 billion)" one paragraph later, and taking both would record the
    same deal twice at two different values.
    """
    from_headline = parse_money(headline)
    if from_headline:
        from_headline["source"] = "headline"
        return from_headline
    for para in paragraphs[:4]:
        if not DEAL_SENTENCE.search(para):
            continue
        hit = parse_money(para)
        if hit:
            hit["source"] = "body"
            return hit
    return None


def parse_article(url, headline):
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup.find(attrs={"itemprop": "articleBody"}) or soup
    paragraphs = [p.get_text(" ", strip=True) for p in article.find_all("p")]
    snippet = " ".join(paragraphs)[:500]
    tagged_firms = firms_from_topics(html, paragraphs)

    # Try deal-shaped parsing FIRST. A headline can be genuinely deal-shaped
    # ("<Firm> advises <Client> on ...") while still containing a
    # personnel-move trigger word incidentally (e.g. "... advises promoter
    # as CEO steps down amid succession planning") -- found via adversarial
    # testing. If parse_headline() actually found real firms, trust that
    # over a keyword match; only fall back to the personnel-move extractor
    # when deal-shaped parsing found nothing to work with.
    parsed = parse_headline(headline)
    if not parsed["law_firms"] and is_personnel_move(headline):
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

    # The page's own tags outrank the headline. A headline names only the firms
    # an editor chose to fit in a title -- it dropped TT&A from the Adani deal
    # and broke entirely on shapes like "act as" and trailing firm lists. The
    # tags are complete, and each one here is corroborated by a body sentence.
    # Headline firms are unioned in rather than discarded, since they sometimes
    # name a firm whose body paragraph uses a phrasing the attribution regex
    # does not cover.
    law_firms = list(tagged_firms)
    for f in parsed["law_firms"]:
        if f not in law_firms:
            law_firms.append(f)

    law_firms = _drop_fragment_firms(discover_corpus_firms(paragraphs, law_firms))

    fallback_firm = law_firms[0] if law_firms else None
    people = extract_people(paragraphs, fallback_firm, known_firms=law_firms)

    return {
        "headline": headline,
        "client": parsed["client"],
        "source": "Bar & Bench",
        "url": url,
        "snippet": snippet,
        "law_firms": law_firms,
        "transaction_types": parsed["transaction_types"],
        "deal_value": extract_deal_value(headline, paragraphs),
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
