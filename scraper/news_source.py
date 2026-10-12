"""
Shared machinery for sources that publish schema.org NewsArticle JSON-LD
(Legal Era, LiveLaw). Each source module only supplies a `Source` config; the
fetching, article parsing and run loop live here so the two cannot drift.

What an article gives us, and how far each part is trusted:

  * JSON-LD `headline`, `datePublished`, `keywords`: machine-readable and
    stable, so they are the primary fields. `keywords` is a curated list of
    the firms, clients and topics in the story.
  * Body paragraphs: the "Name (Role)" credits. Same extractors as Bar &
    Bench, because the outlets reprint the same firm press releases.
  * A firm is accepted only when a keyword AND a body attribution sentence
    both name it (the rule extraction.firms_from_topics already uses), or
    when the headline names a firm the corpus already knows.

Politeness: one request at a time with a delay, an honest User-Agent that
names this project (these sites answer 403 to user agents containing "bot"),
a hard stop on 403/429 for the source, and a cap on new articles per run.
"""
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
from extraction import (  # noqa: E402
    discover_corpus_firms,
    extract_deal_value,
    extract_people,
    firms_from_names,
    is_personnel_move,
    _drop_fragment_firms,
    KNOWN_FIRMS,
    canonical_firm,
    parse_headline_loose,
    parse_personnel_move_people,
)

USER_AGENT = os.environ.get(
    "SCRAPER_UA", "legal-directory-scraper/1.0 (+https://github.com/)"
)
OTHER_SOURCE_FILES = [
    "structured_deals.json", "barandbench_deals.json",
    "legalera_deals.json", "livelaw_deals.json",
]


class Blocked(Exception):
    """The site refused us (403/429). Stop hitting it this run."""


class Source:
    slug = ""            # file stem and record `source_id`
    label = ""           # shown in the UI
    host = ""            # netloc without www.
    article_path = None  # compiled regex the URL path must match
    containers = ()      # CSS selectors tried in order for the body
    delay = 1.5

    # -- discovery: subclasses return absolute URLs --------------------------
    def listing_urls(self, pages):
        return []

    def sitemap_urls(self):
        return []

    @property
    def out_path(self):
        return BASE / f"{self.slug}_deals.json"

    @property
    def seen_path(self):
        return SCRAPER_DIR / f"seen_{self.slug}.json"


# ---- HTTP -------------------------------------------------------------------
class Fetcher:
    def __init__(self, delay):
        self.delay = delay
        self.last = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def get(self, url, retries=2):
        for attempt in range(retries + 1):
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            resp = self.session.get(url, timeout=30)
            self.last = time.monotonic()
            if resp.status_code in (403, 429):
                raise Blocked(f"{resp.status_code} from {urlsplit(url).netloc} for {url}")
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        raise RuntimeError("unreachable")


# ---- URL helpers --------------------------------------------------------------
def canonical_url(url):
    p = urlsplit(url.strip())
    host = p.netloc.lower()
    return urlunsplit(("https", host, p.path.rstrip("/"), "", ""))


# ---- article parsing ------------------------------------------------------------
_BOILERPLATE = [
    re.compile(r"^click to know more", re.I),
    re.compile(r"^if you have a news or deal publication", re.I),
    re.compile(r"^\d{1,2}\s+\w{3,9}\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?\s*(?:IST)?)?$", re.I),
    re.compile(r"^(also read|read more|subscribe|follow us|share this|listen to this)\b", re.I),
    re.compile(r"^(image|photo|source)\s*:", re.I),
]
_LEADING_DESCRIPTOR = re.compile(
    r"^(?:the\s+)?(?:leading|premier|top|full[- ]service|well[- ]known)?\s*"
    r"(?:indian\s+)?law\s+firm(?:\s+in\s+india)?\s*,\s*", re.I
)
_LEADING_THE = re.compile(r"^The\s+(?=[A-Z])")
_VERB_AFTER_COMMA = re.compile(
    r"^(?P<firm>[^,]{2,80}?)\s*,\s+(?P<verb>advised|advises|acted|acts|represented|represents|assisted)\b"
)


def clean_paragraph(text):
    t = text.replace("`", " ").replace(" ", " ")
    t = " ".join(t.split())
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    t = _LEADING_DESCRIPTOR.sub("", t)
    # "Khaitan & Co, advised X" -> "Khaitan & Co advised X"
    m = _VERB_AFTER_COMMA.match(t)
    if m:
        t = f"{m.group('firm')} {t[m.end('firm'):].lstrip(', ')}"
    t = _LEADING_THE.sub("", t)
    return t.strip()


def body_paragraphs(soup, containers, headline):
    root = None
    for sel in containers:
        root = soup.select_one(sel)
        if root is not None:
            break
    if root is None:
        return []
    head_key = re.sub(r"\W+", " ", headline or "").strip().lower()
    out = []
    for p in root.find_all("p"):
        raw = p.get_text(" ", strip=True)
        if not raw.strip(" ` "):
            continue
        if re.sub(r"\W+", " ", raw).strip().lower() == head_key:
            continue
        if any(rx.search(raw.strip()) for rx in _BOILERPLATE):
            continue
        cleaned = clean_paragraph(raw)
        if cleaned and not any(rx.search(cleaned) for rx in _BOILERPLATE):
            out.append(cleaned)
    return out


def jsonld_article(soup):
    best = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(node["@graph"])
                kind = node.get("@type")
                kinds = kind if isinstance(kind, list) else [kind]
                if any(k in ("NewsArticle", "Article", "ReportageNewsArticle") for k in kinds):
                    best = node
    return best or {}


def _keywords(ld):
    kw = ld.get("keywords") or []
    if isinstance(kw, str):
        kw = kw.split(",")
    return [k.strip() for k in kw if isinstance(k, str) and k.strip()]


# Both outlets file some non-deal stories under their deals section: court
# rulings the firm acted in, office openings, hires. A directory of deals must
# not carry those as deals.
NON_DEAL_HEADLINE = re.compile(
    # Unambiguous litigation / firm-news phrases only. Deliberately absent:
    #  - "NCLT", "High Court", "Tribunal": also in headlines about court-
    #    sanctioned mergers, which are real deals;
    #  - "elevate", "appoint", "promote", "induct": hires and promotions are
    #    caught by is_personnel_move(), and a bare word match dropped a real
    #    mandate for "Elevate Campuses".
    r"\b(arbitra\w*|upholds?|quash\w*|dismiss\w*|writ petition|verdict|judgment|"
    r"(?:expands?|expansion)\s+(?:of\s+|its\s+|their\s+)?(?:presence|team|footprint|practice|office|offices)|"
    r"new office|opens (?:a )?(?:new )?office|"
    r"announces (?:strategic |new )?(?:team|office|hire|partner|practice)|"
    r"launches (?:new )?(?:practice|office))\b",
    re.I,
)


def _flat(name):
    """Comparable form of a firm name: '&' and 'and' are the same word, case
    and punctuation do not matter (the headline says "Lakshmikumaran &
    Sridharan", the keyword list says "Lakshmikumaran and Sridharan")."""
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\band\b|&", "", name.lower()))


def parse_article(source, url, html):
    """Returns a deal record, or None when the page is not a usable article."""
    soup = BeautifulSoup(html, "lxml")
    ld = jsonld_article(soup)
    headline = (ld.get("headline") or "").strip()
    if not headline:
        og = soup.find("meta", property="og:title")
        h1 = soup.find("h1")
        headline = ((og and og.get("content")) or (h1 and h1.get_text(" ", strip=True)) or "").strip()
    headline = " ".join(headline.replace("`", " ").split())
    if not headline:
        return None

    published = (ld.get("datePublished") or "").strip()
    if not published:
        meta = soup.find("meta", property="article:published_time")
        published = (meta.get("content") if meta else "") or ""
    paragraphs = body_paragraphs(soup, source.containers, headline)
    keywords = _keywords(ld)
    snippet = " ".join(paragraphs)[:500]

    record = {
        "headline": headline,
        "client": None,
        "source": source.label,
        "source_id": source.slug,
        "url": canonical_url(url),
        "snippet": snippet,
        "law_firms": [],
        "transaction_types": [],
        "people": [],
        "published_at": published[:25] if published else None,
        "keywords": keywords,
    }

    if NON_DEAL_HEADLINE.search(headline):
        return None
    parsed = parse_headline_loose(headline)
    if not parsed["law_firms"] and is_personnel_move(headline):
        people = parse_personnel_move_people(headline)
        record["people"] = people
        record["law_firms"] = sorted({p["firm"] for p in people if p.get("firm")})
        return record

    tagged = firms_from_names(keywords, paragraphs)
    firms = list(tagged)
    kw_slugs = {_flat(k) for k in keywords}
    for f in parsed["law_firms"]:
        if f in firms:
            continue
        # a headline-only firm is trusted when the corpus knows it or the
        # curated keyword list also names it
        flat = _flat(f)
        if f.lower() in KNOWN_FIRMS or any(flat and (flat in k or k in flat) for k in kw_slugs if k):
            firms.append(f)
    firms = _drop_fragment_firms(discover_corpus_firms(paragraphs, firms))

    record["client"] = parsed["client"]
    record["transaction_types"] = parsed["transaction_types"]
    record["law_firms"] = firms
    record["people"] = extract_people(
        paragraphs, firms[0] if firms else None, known_firms=firms
    )
    value = extract_deal_value(headline, paragraphs)
    if value:
        record["deal_value"] = value
    if not record["law_firms"] and not record["people"]:
        # nothing a lawyer directory can use: no firm, no named lawyer
        return None
    return record


# ---- discovery -------------------------------------------------------------------
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


def discover(source, fetcher, pages):
    """Article URLs from sitemaps and listing pages, newest-ish first, deduped.
    A sitemap only spans about a day, so listing pages are what keep a two-day
    schedule from missing articles."""
    urls = []

    def add(u):
        u = canonical_url(urljoin(f"https://www.{source.host}", u))
        if urlsplit(u).netloc.replace("www.", "") != source.host:
            return
        if source.article_path and not source.article_path.search(urlsplit(u).path):
            return
        if u not in urls:
            urls.append(u)

    for sm in source.sitemap_urls():
        try:
            for loc in _LOC_RE.findall(fetcher.get(sm)):
                add(loc)
        except Blocked:
            raise
        except Exception as e:  # a missing sitemap must not sink the run
            print(f"  sitemap {sm} unavailable: {e}")
    for page in source.listing_urls(pages):
        try:
            html = fetcher.get(page)
        except Blocked:
            raise
        except Exception as e:
            print(f"  listing {page} unavailable: {e}")
            continue
        for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
            add(a["href"])
    return urls


# ---- persistence -------------------------------------------------------------------
def load_json(path, default):
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def known_urls_everywhere():
    """Normalised URLs and slugs from every source file, so a URL some other
    source already carries is not fetched again. This is only the cheap
    pre-fetch filter; the authoritative cross-source merge is dedup.py."""
    urls, slugs = set(), set()
    for name in OTHER_SOURCE_FILES:
        for d in load_json(BASE / name, []):
            if d.get("url"):
                urls.add(canonical_url(d["url"]))
    return urls, slugs


def run(source, argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pages = int(argv[argv.index("--pages") + 1]) if "--pages" in argv else 2
    max_new = int(argv[argv.index("--max-new") + 1]) if "--max-new" in argv else 40
    reparse = "--reparse-all" in argv

    fetcher = Fetcher(float(os.environ.get("SCRAPE_DELAY", source.delay)))
    deals = load_json(source.out_path, [])
    seen = set(load_json(source.seen_path, []))

    if reparse:
        out = []
        try:
            for i, old in enumerate(deals, 1):
                try:
                    rec = parse_article(source, old["url"], fetcher.get(old["url"]))
                except Blocked:
                    raise
                except Exception as e:
                    print(f"failed to reparse {old['url']}: {e}")
                    out.append(old)
                    continue
                if rec is None:
                    print(f"dropping non-deal / empty record: {old['url']}")
                    continue
                out.append(rec)
        except Blocked as e:
            print(f"BLOCKED: {e}; keeping stored records for the rest")
            out.extend(deals[len(out):])
        save_json(source.out_path, out)
        print(f"reparsed {len(out)} {source.label} deals")
        return 0

    have = {canonical_url(d["url"]) for d in deals}
    try:
        urls = discover(source, fetcher, pages)
    except Blocked as e:
        print(f"BLOCKED during discovery: {e}")
        return 0
    elsewhere, _ = known_urls_everywhere()
    todo = [u for u in urls if u not in seen and u not in have and u not in elsewhere]
    print(f"{source.label}: {len(urls)} article links, {len(todo)} new")
    if len(todo) > max_new:
        print(f"  capping at {max_new} (rerun, or pass --max-new, to continue)")
        todo = todo[:max_new]

    added = 0
    for url in todo:
        try:
            rec = parse_article(source, url, fetcher.get(url))
        except Blocked as e:
            print(f"BLOCKED: {e}; stopping {source.label} for this run")
            break
        except Exception as e:
            print(f"failed to parse {url}: {e}")
            continue
        seen.add(url)
        if rec is None:
            continue
        deals.append(rec)
        added += 1

    save_json(source.out_path, deals)
    save_json(source.seen_path, sorted(seen))
    print(f"added {added} new deals; {len(deals)} total in {source.out_path.name}")
    return 0
