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
from extraction import (  # noqa: E402
    BASE,
    CURRENCY_SYMBOL,
    DEAL_SENTENCE,
    FIRM_CONTEXT_RE,
    FIRM_TAIL_RE,
    FX_TO_INR,
    FX_TO_INR_ASOF,
    HEADLINE_VERB,
    MAGNITUDE,
    MONEY_RE,
    NAME_PATTERN,
    PAREN_ROLE_RE,
    PERSONNEL_HEADLINE_PATTERNS,
    PLURAL_MARKERS_BY_LENGTH,
    PLURAL_ROLE_MARKERS,
    ROLE_ALTERNATION,
    ROLE_WORDS,
    TOPIC_HREF_RE,
    TXN_KEYWORDS,
    _ALIAS_TEXTS,
    _APPROX,
    _NAME_FIRST_WORD,
    _NAME_INNER_WORD,
    _NAME_PARTICLES,
    _NICKNAME_PAREN_RE,
    _NOT_A_FIRM_LEAD,
    _NOT_A_FIRM_WORDS,
    _PERSONNEL_NAME_REJECT_WORDS,
    _drop_fragment_firms,
    _is_firm_not_person,
    _is_name_word,
    _looks_like_firm_name,
    _looks_like_real_name,
    _slugify_firm,
    _strip_nickname_parens,
    _topic_match,
    body_attribution_firms,
    discover_corpus_firms,
    extract_deal_value,
    extract_firm_context,
    extract_paren_credits,
    extract_people,
    extract_plural_list_credits,
    firms_from_topics,
    leading_known_firm,
    parse_headline,
    parse_money,
    parse_personnel_move_people,
    split_firm_list,
    topic_slugs,
)
import ner_supplement  # noqa: E402

LISTING_URL = "https://www.barandbench.com/dealstreet"
SITE_ROOT = "https://www.barandbench.com"
SEEN_PATH = SCRAPER_DIR / "seen_urls.json"
OUT_PATH = BASE / "barandbench_deals.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; legal-directory-bot/1.0; +https://github.com/)"
}



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
