"""
Weekly scraper for Bar & Bench's Dealstreet section.

Fetches the listing page, finds article URLs not already in seen_urls.json,
fetches each new article, extracts headline/firms/people/transaction info,
and appends results to barandbench_deals.json in the same schema as
structured_deals.json entries so build_directory.py can merge both sources
untouched.

NOTE: the person/firm extraction regexes below are a first pass based on
Bar & Bench's typical headline style ("X, Y act on <client> <deal>",
"X advises <client> on <deal>") and common body phrasing ("led by Partner
NAME", "assisted by Associates A and B"). Run this against a handful of
real fetched articles and adjust EXTRACT_PATTERNS / ROLE_PATTERN before
trusting the output at scale — do not assume it's correct out of the box.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SCRAPER_DIR = Path(__file__).parent
BASE = SCRAPER_DIR.parent
sys.path.insert(0, str(BASE))
from build_directory import canonical_firm  # noqa: E402

LISTING_URL = "https://www.barandbench.com/dealstreet"
SITE_ROOT = "https://www.barandbench.com"
SEEN_PATH = SCRAPER_DIR / "seen_urls.json"
OUT_PATH = BASE / "barandbench_deals.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; legal-directory-bot/1.0; +https://github.com/)"
}

# Headline verb patterns: "<firms> <verb> <client> <deal desc>"
HEADLINE_VERB = re.compile(
    r"^(?P<firms>.+?)\s+(?:act(?:s)? on|advises|advise|assists|assist|represents|represent)\s+(?P<rest>.+)$",
    re.I,
)

# Body phrasing for named individuals, e.g. "led by Partner Rahul Sharma" or
# "Associates Priya Mehta and Arjun Rao advised on the deal".
ROLE_PATTERN = re.compile(
    r"(?P<role>Partner|Senior Partner|Managing Partner|Counsel|Of Counsel|"
    r"Senior Associate|Principal Associate|Associate)s?\s+"
    r"(?P<names>[A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3}"
    r"(?:(?:,|\s+and)\s+[A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})*)"
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


def find_article_links(listing_html):
    soup = BeautifulSoup(listing_html, "lxml")
    links = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(SITE_ROOT, a["href"])
        if not href.startswith(SITE_ROOT):
            continue
        text = a.get_text(strip=True)
        # article links are long slugs with real headline text; skip nav/footer chrome
        if len(text) < 20:
            continue
        if "/dealstreet/" not in href and re.search(r"-\d{5,}$", href) is None:
            # Bar & Bench article URLs typically end in a numeric story id or
            # live under /dealstreet/<slug>. Keep both patterns permissive.
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
    firms = split_firm_list(m.group("firms"))
    rest = m.group("rest")
    client = rest.split(" on ")[0].strip() if " on " in rest else rest.strip()
    # trim trailing deal-size/description noise: "Arboreal ₹230 crore Series A
    # fundraise" -> "Arboreal" (cut at first currency symbol or digit)
    client = re.split(r"[₹$]|\s+\d", client)[0].strip()
    txn_types = []
    for key in ("IPO", "QIP", "Series A", "Series B", "Series C", "acquisition",
                "stake acquisition", "merger", "amalgamation", "demerger",
                "buyout", "divestment", "financing", "joint venture",
                "fundraise", "investment", "restructuring"):
        if key.lower() in headline.lower():
            txn_types.append(key)
    return {"law_firms": firms, "client": client, "transaction_types": txn_types}


def extract_people(body_text):
    seen = set()
    people = []
    for m in ROLE_PATTERN.finditer(body_text):
        role = m.group("role")
        names_raw = m.group("names")
        # split on commas/"and"/sentence-ending periods (a stray ". Capital"
        # inside the match means the regex ran past the end of the sentence)
        for name in re.split(r",\s*|\s+and\s+|\.\s+", names_raw):
            name = name.strip().rstrip(".")
            if len(name) < 4 or " " not in name:
                continue
            key = (name.lower(), role)
            if key in seen:
                continue
            seen.add(key)
            people.append({"name": name, "role": role})
    return people


def parse_article(url, headline):
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup.find(attrs={"itemprop": "articleBody"}) or soup
    paragraphs = [p.get_text(" ", strip=True) for p in article.find_all("p")]
    body_text = " ".join(paragraphs)
    snippet = body_text[:500]

    parsed = parse_headline(headline)
    people = extract_people(body_text)

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


def main():
    seen = load_seen()
    deals = load_existing_deals()

    listing_html = fetch(LISTING_URL)
    links = find_article_links(listing_html)
    new_links = {url: text for url, text in links.items() if url not in seen}

    print(f"found {len(links)} article links, {len(new_links)} new")

    added = 0
    for url, headline in new_links.items():
        try:
            deal = parse_article(url, headline)
        except Exception as e:
            print(f"failed to parse {url}: {e}")
            continue
        deals.append(deal)
        seen.add(url)
        added += 1

    OUT_PATH.write_text(json.dumps(deals, indent=2, ensure_ascii=False))
    save_seen(seen)
    print(f"added {added} new deals; {len(deals)} total in {OUT_PATH.name}")


if __name__ == "__main__":
    main()
