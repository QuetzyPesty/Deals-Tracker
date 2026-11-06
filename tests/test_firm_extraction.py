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
import extraction as _ex  # noqa: E402

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
    parse.last_people = people
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


# name -> client the article's own attribution sentence gives that lawyer's firm.
# The headline names only one client, so crediting everyone with it is wrong for
# the other side of the deal. None = ambiguous, no client may be recorded.
CLIENTS = [
    ("brahma-two-clients.html.gz", {
        "David Griffith-Jones": "Brahma AI",           # Slaughter and May
        "Shishir Vayttaden": "Multiples",              # Khaitan & Co
        "Angad Banerji": "Multiples",
        "Chrissy Findlay": "Multiples"}),              # Eversheds Sutherland
    ("adani-act-as.html.gz", {
        "Subhalakshmi Naskar": "Adani Airports",
        "Vikram Raghani": "Temasek Holdings",
        "Sonali Mahapatra": "BlackRock managed funds",
        # AZB "acted for both Alpha Wave Global and Premji Invest": ambiguous
        "Vaidhyanathan Iyer": None}),
    ("shiprocket-served-as.html.gz", {
        # the Selling Shareholders / BRLMs are classes of parties, not names
        "Gautham Srinivas": None}),
]


# Whole-article cases (headline + body), for bugs that live in the headline split
# or in the firm hand-over rather than in the credit extraction alone.
LL_BASE = "https://www.livelaw.in/law-firms/deals/"
WHOLE = [
    # "Slaughter and May" was split at the "and" into "Slaughter" + a fake firm
    # "May", whose presence made the real name ambiguous and left all five of
    # its lawyers without a firm.
    ("bb", "slaughter-and-may.html.gz",
     "https://www.barandbench.com/law-firms/dealstreet/slaughter-and-may-eversheds-sutherland-khaitan-co-act-on-brahma-ai-150-million-fundraise",
     "Slaughter and May, Eversheds Sutherland, Khaitan & Co act on Brahma AI $150 million fundraise",
     {"Slaughter and May", "Eversheds Sutherland", "Khaitan & Co"}, {"David Griffith-Jones": "Slaughter and May"}),
    # Legal Era / LiveLaw spell it "And"; it was split into two fake firms
    ("ll", "lks-and-brenntag.html.gz",
     LL_BASE + "lakshmikumaran-and-sridharan-attorneys-brenntag-group-raj-petro-specialities-296838",
     "Lakshmikumaran And Sridharan Attorneys Advises Brenntag Group On Sale Of Raj Petro Specialities To Shell...",
     {"Lakshmikumaran & Sridharan Attorneys"}, {"Srabonee Roy": "Lakshmikumaran & Sridharan Attorneys"}),
    # the paragraph after the law firm's introduction names a banker
    # ("acted as the Book Running Lead Manager"), which wiped the firm
    ("ll", "singhania-banker.html.gz",
     LL_BASE + "singhania-co-anondita-medicare-695-crore-sme-ipo-listing-law-firms-deal-update-303316",
     "Singhania & Co. Advises Anondita Medicare On ₹69.5 Crore SME IPO Listing",
     {"Singhania & Co"}, {"Rohit Jain": "Singhania & Co"}),
    ("ll", "lks-toyota.html.gz",
     LL_BASE + "lakshmikumaran-sridharan-attorneys-advises-toyota-group-acquisition-car-dealership-business-ravindu-motors-private-limited-310363",
     "Lakshmikumaran & Sridharan Attorneys Advises Toyota Group On Acquisition Of Car Dealership Business From...",
     {"Lakshmikumaran & Sridharan Attorneys"}, {"Srabonee Roy": "Lakshmikumaran & Sridharan Attorneys"}),
]


def whole_article(kind, fixture, url, headline):
    html = gzip.open(FIXTURES / fixture, "rt", encoding="utf8").read()
    if kind == "bb":
        old, S.fetch = S.fetch, (lambda u: html)
        try:
            return S.parse_article(url, headline)
        finally:
            S.fetch = old
    import news_source as ns
    from scrape_livelaw import SOURCE as LL
    return ns.parse_article(LL, url, html)


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

    for fixture, want in CLIENTS:
        parse(fixture)
        got = {p["name"]: p.get("client") for p in parse.last_people}
        for name, client in want.items():
            if got.get(name, "<missing>") != client:
                failures.append(f"{fixture}: {name} client -> {got.get(name, '<missing>')!r}, expected {client!r}")
        print(f"  ok   per-firm clients {fixture}" if not [f for f in failures if f.startswith(fixture)] else f"  FAIL per-firm clients {fixture}")

    for kind, fixture, url, headline, want_firms, want_people in WHOLE:
        rec = whole_article(kind, fixture, url, headline)
        before = len(failures)
        if set(rec["law_firms"]) != want_firms:
            failures.append(f"{fixture}: firms {rec['law_firms']}, expected {sorted(want_firms)}")
        got = {p["name"]: p["firm"] for p in rec["people"]}
        for name, firm in want_people.items():
            if got.get(name) != firm:
                failures.append(f"{fixture}: {name} -> {got.get(name)!r}, expected {firm!r}")
        lost = [p["name"] for p in rec["people"] if not p["firm"]]
        if lost:
            failures.append(f"{fixture}: unattributed on a single-firm article: {lost[:4]}")
        print(f"  {'ok  ' if len(failures) == before else 'FAIL'} whole article {fixture}")

    # the generic rule, on a firm nobody has heard of
    body_one = ["Quill and Quire Legal advised Acme on this transaction."]
    got = _ex.split_firm_names("Quill and Quire Legal, Trilegal", body_one)
    if got != ["Quill and Quire Legal", "Trilegal"]:
        failures.append(f"unknown 'X and Y' firm not rejoined from the body: {got}")
    # two firms that each get their own sentence stay two firms
    body_two = ["Sidley Austin advised Acme.", "FBT Gibbons advised Acme."]
    got = _ex.split_firm_names("Sidley Austin and FBT Gibbons", body_two)
    if got != ["Sidley Austin", "FBT Gibbons"]:
        failures.append(f"two separate firms were fused: {got}")
    # no evidence at all: behaviour is unchanged (split)
    if _ex.split_firm_names("Quill and Quire Legal") != ["Quill", "Quire Legal"]:
        failures.append("splitting without evidence changed")
    from build_directory import canonical_firm
    for spellings, want in [
        (["TLH Advocates", "TLH, Advocates & Solicitors"], "TLH Advocates & Solicitors"),
        (["Vidhigya Associates", "Vidhigya Associates, Advocates"], "Vidhigya Associates"),
        (["Chandhiok & Mahajan", "Chandiok & Mahajan"], "Chandhiok & Mahajan"),
        (["Singhania & Co.", "S&Co."], "Singhania & Co"),
        (["LKS", "Lakshmikumaran And Sridharan Attorneys", "Lakshmikumaran & Sridharan Attorneys"],
         "Lakshmikumaran & Sridharan Attorneys"),
    ]:
        got = {canonical_firm(x) for x in spellings}
        if got != {want}:
            failures.append(f"spellings of one firm not folded together: {spellings} -> {got}")
    print("  ok   generic 'and' rejoin" if not any("'X and Y'" in f or "fused" in f or "without evidence" in f for f in failures) else "  FAIL generic 'and' rejoin")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\nall {len(CASES)} fixtures pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
