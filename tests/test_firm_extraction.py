"""Regression tests for firm attribution.

Every fixture is a real Bar & Bench article saved verbatim, so these run
offline and pin the exact failures that motivated the rewrite:

  * "act as" and trailing firm lists ("...; CAM, Khaitan, JSA advise") broke
    parse_headline(), which left known_firms empty, which made every body-text
    firm mention untrusted, which left every lawyer in the article unattributed;
  * TT&A acted in the Adani deal without appearing in the headline, and the
    paragraph tracker handed its three lawyers to the previous firm;
  * Pinac and GameChanger Law Advisors are not in KNOWN_FIRMS and never will
    be reliably -- the whitelist is an open set and cannot be the gate.

Run:  python3 tests/test_firm_extraction.py
"""
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scraper"))

from bs4 import BeautifulSoup  # noqa: E402
import scrape_barandbench as S  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def parse(name):
    html = gzip.open(FIXTURES / name, "rt", encoding="utf8").read()
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup
    paragraphs = [p.get_text(" ", strip=True) for p in article.find_all("p")]
    # mirror parse_article(): topic-tagged firms, then corpus firms that open a
    # paragraph about their role (the international counsel a headline omits)
    firms = S._drop_fragment_firms(
        S.discover_corpus_firms(paragraphs, S.firms_from_topics(html, paragraphs)))
    people = S.extract_people(paragraphs, firms[0] if firms else None, known_firms=firms)
    return firms, {p["name"]: p["firm"] for p in people}


CASES = [
    # fixture, firms that must be found, name -> firm that must hold
    ("adani-act-as.html.gz",
     {"Cyril Amarchand Mangaldas", "JSA Advocates & Solicitors",
      "Talwar Thakore & Associates", "AZB & Partners"},
     {"Vikram Raghani": "JSA Advocates & Solicitors",
      "Subhalakshmi Naskar": "Cyril Amarchand Mangaldas",
      # the orphan-firm case: TT&A is absent from the headline, and these
      # three were being handed to JSA
      "Sonali Mahapatra": "Talwar Thakore & Associates",
      "Rebha Dakshini": "Talwar Thakore & Associates",
      "Jasel Mundhra": "Talwar Thakore & Associates",
      "Vaidhyanathan Iyer": "AZB & Partners"}),
    ("pinac.html.gz", {"Pinac"}, {}),
    ("gamechanger.html.gz",
     {"GameChanger Law Advisors", "First Principles Law"},
     {"Samheeta Rao": "GameChanger Law Advisors"}),
    ("warburg-trailing.html.gz",
     {"Cyril Amarchand Mangaldas", "Khaitan & Co", "JSA Advocates & Solicitors"},
     {"Vikram Raghani": "JSA Advocates & Solicitors"}),
    ("upgrad-trailing.html.gz",
     {"JSA Advocates & Solicitors", "CMS INDUSLAW", "Khaitan & Co",
      "River Law", "Shardul Amarchand Mangaldas & Co", "Bharucha & Partners"},
     {"Siddharth Manchanda": "JSA Advocates & Solicitors",
      "Vandana Pai": "Bharucha & Partners"}),
    # Newer template: "<Firm> served as the international legal counsel ..."
    # and sentences like "The tax aspects ... were advised by X (Partner)".
    # Neither opens with a verb we listed, so the firm hand-over was missed (or,
    # worse, a non-firm subject wiped the current firm) and lawyers were left
    # unattributed or handed to the previous firm.
    ("idfc-served-as.html.gz",
     {"Talwar Thakore & Associates", "Shardul Amarchand Mangaldas & Co", "Linklaters"},
     {"Rahul Gulati": "Talwar Thakore & Associates",
      "Shubhangi Garg": "Shardul Amarchand Mangaldas & Co",
      "Gouri Puri": "Shardul Amarchand Mangaldas & Co",
      "Nimish Malpani": "Shardul Amarchand Mangaldas & Co",
      "Prashant Gupta": "Shardul Amarchand Mangaldas & Co",
      "Amit Singh": "Linklaters",
      "Xunming Lim": "Linklaters",
      "Edward Lee": "Linklaters"}),
    ("leap-served-as.html.gz",
     {"Cyril Amarchand Mangaldas", "Shardul Amarchand Mangaldas & Co", "A&O Shearman"},
     {"Devaki Mankad": "Cyril Amarchand Mangaldas",
      "Sayantan Dutta": "Shardul Amarchand Mangaldas & Co",
      "Pallavi Gopinath Aney": "A&O Shearman",
      "Mark Leemen": "A&O Shearman"}),
    ("shiprocket-served-as.html.gz",
     {"Cyril Amarchand Mangaldas", "Khaitan & Co", "Shardul Amarchand Mangaldas & Co",
      "Latham & Watkins"},
     {"Aashima Johur": "Cyril Amarchand Mangaldas",
      "Bharath Reddy": "Cyril Amarchand Mangaldas",
      "Gautham Srinivas": "Khaitan & Co",
      "Avinash Gautam": "Khaitan & Co",
      "Rajiv Gupta": "Latham & Watkins",
      "Elena Romanova": "Latham & Watkins"}),
]


def main():
    failures = []
    for fixture, want_firms, want_people in CASES:
        firms, people = parse(fixture)
        missing = want_firms - set(firms)
        if missing:
            failures.append(f"{fixture}: firms not found: {sorted(missing)} (got {firms})")
        for name, firm in want_people.items():
            got = people.get(name, "<person not extracted>")
            if got != firm:
                failures.append(f"{fixture}: {name} -> {got!r}, expected {firm!r}")
        # nobody may be attributed to a firm the article never confirms
        stray = {f for f in people.values() if f and f not in set(firms)}
        if stray:
            failures.append(f"{fixture}: people attributed to unconfirmed firms: {sorted(stray)}")
        print(f"  {'ok  ' if not missing else 'FAIL'} {fixture:28} {len(firms)} firms, {len(people)} people")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\nall {len(CASES)} fixtures pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
