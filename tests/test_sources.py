"""Offline tests for the Legal Era and LiveLaw scrapers and their interaction
with de-duplication. Fixtures are real articles (trimmed to JSON-LD + article
container) captured 2026-10-12, so no test touches the network.

Run:  python3 tests/test_sources.py
"""
import gzip
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scraper"))

import news_source as ns  # noqa: E402
import dedup  # noqa: E402
from build_directory import is_personnel_move, best_role  # noqa: E402
from scrape_legalera import SOURCE as LE, LegalEra  # noqa: E402
from scrape_livelaw import SOURCE as LL, LiveLaw  # noqa: E402

RealFetcher = ns.Fetcher
FX = Path(__file__).parent / "fixtures" / "sources"
MANIFEST = json.loads((FX / "manifest.json").read_text())
FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def html_of(name):
    return gzip.open(FX / f"{name}.html.gz", "rt", encoding="utf8").read()


def parse(source, name):
    return ns.parse_article(source, MANIFEST[f"{name}.html.gz"], html_of(name))


def by_name(rec):
    return {p["name"]: p for p in rec["people"]}


KHAITAN = "Khaitan & Co"
ROLE_WORDS = {"partner", "partners", "associate", "associates", "counsel", "senior", "principal"}

# --------------------------------------------------------------- Legal Era
print("Legal Era")
r = parse(LE, "le_1")
check("headline, date and source come from JSON-LD",
      r["headline"].startswith("Khaitan & Co Advised Multiples Private Equity")
      and r["published_at"].startswith("2026-10-12") and r["source"] == "Legal Era"
      and r["source_id"] == "legalera")
check("firm confirmed by keyword + body", r["law_firms"] == [KHAITAN], r["law_firms"])
check("client from Title Case headline", r["client"] == "Multiples Private Equity", r["client"])
check("'USD 100 Million' is read as a value",
      r.get("deal_value", {}).get("currency") == "USD" and r["deal_value"]["amount"] == 100_000_000,
      r.get("deal_value"))
p = by_name(r)
check("team credits across practice-area paragraphs",
      len(p) >= 15 and p["Sahil Shah"]["role"] == "Partner"
      and p["Vinay Joy"]["firm"] == KHAITAN and p["Aditya J Nair"]["role"] == "Senior Associate",
      len(p))
check("single-letter surname kept (Aishwarya K)", "Aishwarya K" in p)

r = parse(LE, "le_2")
check("'Advised on <client>'s ...' headline yields the client", r["client"] == "Dilip Buildcon", r["client"])
check("an enterprise-value / project-cost figure is not mistaken for deal size",
      r.get("deal_value") is None, r.get("deal_value"))

r = parse(LE, "le_4")
check("'INR 300 Crore QIP' headline value",
      r["deal_value"]["amount"] == 3_000_000_000 and r["deal_value"]["source"] == "headline", r.get("deal_value"))
check("transaction type from headline", "QIP" in r["transaction_types"], r["transaction_types"])

# ----------------------------------------------------------------- LiveLaw
print("LiveLaw")
r = parse(LL, "ll_1")
check("source label and date", r["source"] == "LiveLaw" and r["published_at"].startswith("2026-10-12"))
check("rupee value with approximate USD in text keeps the rupee figure",
      r["deal_value"]["currency"] == "INR" and r["deal_value"]["amount"] == 190_000_000_000, r.get("deal_value"))

r = parse(LL, "ll_4")
p = by_name(r)
check("role-before-name credits are extracted (Partner Kartick Maheshwari)",
      p.get("Kartick Maheshwari", {}).get("role") == "Partner"
      and p.get("Debottam Chattopadhyay", {}).get("role") == "Principal Associate", sorted(p)[:5])
check("a plural list ends at the next role marker (Senior Associates ... not Partners)",
      all(p[n]["role"] == "Senior Associate"
          for n in ("Madhav Chandan", "Hardik Adlakha", "Poojita Saxena", "Radhika Goenka")),
      {n: p[n]["role"] for n in p if n in ("Madhav Chandan", "Hardik Adlakha")})
check("no name contains a role word",
      not any(set(w.lower() for w in n.split()) & ROLE_WORDS for n in p),
      [n for n in p if set(w.lower() for w in n.split()) & ROLE_WORDS])
check("same lawyer is not attributed twice", len(p) == len(r["people"]))

r = parse(LL, "ll_5")
check("'J Sagar Associates and Solicis Lex' does not produce a person",
      "Solicis Lex" not in by_name(r), [x["name"] for x in r["people"]])

r = parse(LL, "ll_6")
check("firm named in a trailing headline clause", r["law_firms"] == [KHAITAN], r["law_firms"])
check("transaction type from headline", "merger" in r["transaction_types"])

r = parse(LL, "ll_8")
check("article with no credits yields a deal and no invented people",
      r["law_firms"] == ["Dua Associates"] and r["people"] == [], (r["law_firms"], r["people"]))
check("EUR value", r["deal_value"]["currency"] == "EUR" and r["deal_value"]["amount"] == 7_000_000)

# --------------------------------------------------------- non-deal filter
print("non-deal filter")
nd = ns.NON_DEAL_HEADLINE
check("a client called 'Elevate' is not firm news (regression: dropped a real CAM IPO)",
      not nd.search("Cyril Amarchand Mangaldas Advised Book Running Lead Managers On Elevate Campuses’ IPO"))
check("'expansion' alone is not firm news", not nd.search("Khaitan & Co Advises Mahindra On Capacity Expansion Financing"))
check("court-sanctioned merger headlines are not dropped",
      not nd.search("Trilegal Advised Acme On NCLT-Approved Scheme Of Amalgamation"))
check("office openings and rulings are dropped",
      bool(nd.search("ASM Law Chambers Expands Presence With New Office In Mumbai"))
      and bool(nd.search("Gujarat High Court Upholds Land Acquisition; UNUC Legal Represents NHSRCL"))
      and bool(nd.search("Kochhar & Co. Represents Air Liquide in Arbitration Against Vedanta")))

# ------------------------------------------- firm names that are not firm names
print("garbage firm names (production regressions, 2026-10-13 scrape)")
import re as _re  # noqa: E402

NOT_A_FIRM_VERB = _re.compile(r"\b(is|are|was|were|has|have|had|secured|advised|acted)\b")


from extraction import clean_firm_candidate  # noqa: E402


def firm_ok(f):
    """Plausible = the pipeline's own cleaner would not change or reject it."""
    return clean_firm_candidate(f) == f


KEEP_NAMES = ["Khaitan & Co", "AZB & Partners", "Cyril Amarchand Mangaldas", "Shardul Amarchand Mangaldas & Co",
              "J Sagar Associates", "Luthra and Luthra Law Offices India", "S&R Associates", "Fox Mandal & Associates",
              "Talwar Thakore & Associates", "Law Offices of Panag & Babu", "Dentons Link Legal", "Trilegal",
              "Kochhar & Co.", "IC RegFin Legal", "Crawford Bayley & Co", "Anand and Anand", "Veritas Legal",
              "Linklaters Singapore Pte. Ltd"]
TRIM_NAMES = {"Khaitan & Co regularly": "Khaitan & Co", "DSK Legal teams": "DSK Legal", "JSA had previously": "JSA",
              "Khaitan & Co acted": "Khaitan & Co", "Trilegal also": "Trilegal", "AZB & Partners often": "AZB & Partners",
              "Dua Associates is a full-service Indian law firm": "Dua Associates",
              "JSA Advocates & Solicitors secured the CCI approval for this merger. The Firm": "JSA Advocates & Solicitors"}
check("legitimate firm names pass through the cleaner unchanged",
      all(clean_firm_candidate(n) == n for n in KEEP_NAMES), [n for n in KEEP_NAMES if clean_firm_candidate(n) != n])
check("a firm name followed by any lowercase word is cut there (not an enumerated verb list)",
      all(clean_firm_candidate(k) == v for k, v in TRIM_NAMES.items()),
      {k: clean_firm_candidate(k) for k, v in TRIM_NAMES.items() if clean_firm_candidate(k) != v})
check("a bank / company that is not a law firm is rejected",
      clean_firm_candidate("Motilal Oswal Investment Advisors Limited") is None
      and clean_firm_candidate("ICICI Securities Limited and Jefferies India Private Limited") is None)

r = parse(LE, "le_anta_regularly")
check("'Khaitan & Co regularly advised ...' yields Khaitan & Co (Legal Era / ANTA, real article)",
      r["law_firms"] == [KHAITAN], r["law_firms"])

r = parse(LL, "ll_dua_boilerplate")
check("an 'About the firm' paragraph does not become the firm name (LiveLaw / Dua Associates)",
      r["law_firms"] == ["Dua Associates"], r["law_firms"])
check("...and its lawyers are still attributed to the real firm",
      r["people"] and all(p["firm"] == "Dua Associates" for p in r["people"]),
      [(p["name"], p["firm"]) for p in r["people"]][:3])

r = parse(LE, "le_runwal_brlm")
check("the bankers named as BRLMs are not law firms (Legal Era / Runwal)",
      r["law_firms"] == [KHAITAN], r["law_firms"])
check("...and no lawyer is attributed to a bank",
      all(p["firm"] in (KHAITAN, None) for p in r["people"]), {p["firm"] for p in r["people"]})

import scrape_barandbench as _bb  # noqa: E402
_bb.fetch = lambda u: gzip.open(FX.parent / "interglobe-accor-sentence.html.gz", "rt", encoding="utf8").read()
r = _bb.parse_article("https://www.barandbench.com/law-firms/dealstreet/jsa-secures-cci-approval-for-interglobe-accor-hospitality-merger",
                      "JSA secures CCI approval for InterGlobe - Accor hospitality merger")
check("a sentence is not a firm name (Bar & Bench / JSA, pre-existing bug)",
      all(firm_ok(f) for f in r["law_firms"]) and "JSA Advocates & Solicitors" in r["law_firms"], r["law_firms"])
check("every firm in every committed source file is a plausible name",
      all(firm_ok(f) for name in ("barandbench_deals.json", "legalera_deals.json", "livelaw_deals.json")
          for d in json.loads((ROOT / name).read_text()) for f in d["law_firms"]),
      [f for name in ("barandbench_deals.json", "legalera_deals.json", "livelaw_deals.json")
       for d in json.loads((ROOT / name).read_text()) for f in d["law_firms"] if not firm_ok(f)])

# ----------------------------------------------- firms whose name contains "and"
print("fused and split firm names")
import extraction as _ex  # noqa: E402
from build_directory import canonical_firm  # noqa: E402

for parse_fn in (_ex.parse_headline, _ex.parse_headline_loose):
    check(f"{parse_fn.__name__}: 'Luthra and Luthra' stays one firm",
          parse_fn("Luthra and Luthra acts on Armee Infotech on ₹300 crore IPO")["law_firms"] == ["Luthra and Luthra"],
          parse_fn("Luthra and Luthra acts on Armee Infotech on ₹300 crore IPO")["law_firms"])
    check(f"{parse_fn.__name__}: 'Crawford Bayley, Luthra and Luthra' is two firms",
          parse_fn("Crawford Bayley, Luthra and Luthra act on Master Chains IPO")["law_firms"]
          == ["Crawford Bayley", "Luthra and Luthra"])
    check(f"{parse_fn.__name__}: 'AZB and Trilegal' is still two firms",
          parse_fn("AZB and Trilegal act on Foo IPO")["law_firms"] == ["AZB & Partners", "Trilegal"])

check("one firm written three ways is one firm",
      {canonical_firm(n) for n in ("Kanga & Co", "Kanga & Company, Advocates and Solicitors", "Kanga and Co")} == {"Kanga & Co"}
      and {canonical_firm(n) for n in ("Luthra and Luthra", "Luthra and Luthra Law Offices India", "Luthra & Luthra")}
      == {"Luthra and Luthra"})
check("two known firms joined by 'and' are split",
      _ex.split_fused_firms("Dentons Link Legal and Hogan Lovells Cadwalader") == ["Dentons Link Legal", "Hogan Lovells Cadwalader"])
check("a single firm with 'and' in its name is not split",
      all(_ex.split_fused_firms(n) == [n] for n in ("Luthra and Luthra", "Kanga & Company, Advocates and Solicitors")))
check("page topics vouch for a split: Sidley Austin and FBT Gibbons",
      _ex._attributed_names(["Sidley Austin and FBT Gibbons advised the Government on this offer."], ["sidley-austin"])
      == ["Sidley Austin", "FBT Gibbons"])
check("...but 'Saraf and Partners' / 'Advocates and Solicitors' are never split",
      _ex._attributed_names(["Saraf and Partners advised the company on this deal."], ["saraf-and-partners"]) == ["Saraf and Partners"]
      and _ex._attributed_names(["Advocates and Solicitors advised the company on this deal."], ["advocates"])
      == ["Advocates and Solicitors"])
_all_firms = {f for name in ("barandbench_deals.json", "legalera_deals.json", "livelaw_deals.json")
              for d in json.loads((ROOT / name).read_text()) for f in d["law_firms"]}
check("no committed firm is two firms fused together",
      not [f for f in _all_firms if len(_ex.split_fused_firms(f)) > 1], [f for f in _all_firms if len(_ex.split_fused_firms(f)) > 1])
check("no committed firm is a truncated 'Luthra' / 'Kanga & Company, ...' variant",
      not ({"Luthra", "Kanga & Company, Advocates and Solicitors"} & _all_firms))

# ---------------------------------------- headlines whose verb we do not know
print("headline fallbacks")
check("a firm opening the headline AND named in the curated keywords is the firm, whatever the verb",
      _ex.lead_keyword_firms("AMS Legal Concludes Title Due Diligence For Land Parcels Worth ₹1,000 Crores",
                             ["AMS Legal", "Real Estate", "Title Due Diligence"]) == ["AMS Legal"])
check("...a client opening the headline is not read as a firm",
      _ex.lead_keyword_firms("Samara Capital To Invest In Associated Road Carriers",
                             ["Samara Capital", "Associated Road Carriers"]) == [])
check("...and a keyword that is not at the start of the headline is not used",
      _ex.lead_keyword_firms("Foo Ltd Hires Khaitan & Co For Merger", ["Khaitan & Co"]) == [])
check("an office-expansion headline is firm news, not a deal, however it is worded",
      all(ns.NON_DEAL_HEADLINE.search(h) for h in (
          "Arogya Legal Expands Its Mumbai Presence", "ASM Law Chambers Expands Presence With New Office In Mumbai",
          "Kochhar & Co Announces Strategic Team Expansion In Delhi")))
check("...but a real deal headline is not caught by it",
      not any(ns.NON_DEAL_HEADLINE.search(h) for h in (
          "Khaitan & Co Advised Elevate Campuses On Its IPO", "Trilegal Advised Apollo On Expansion Of Its Hospital Business Financing")))
check("'$1,200 crore' is a typo, not a figure: no value (crore is a rupee unit)",
      _ex.parse_money("Trilegal acts on Laser Power & Infra $1,200 crore IPO") is None
      and _ex.parse_money("₹742 crore IPO")["amount_inr"] == 7_420_000_000
      and _ex.parse_money("a $3.3 billion offer")["currency"] == "USD")

import build_directory as _bd  # noqa: E402
check("a forum thread is not a deal, a dealstreet article is",
      _bd.is_non_deal_page({"url": "https://www.legallyindia.com/convos/topic/452520-good-pevc-team-in-trilegal-ggn"})
      and not _bd.is_non_deal_page({"url": "https://www.barandbench.com/law-firms/dealstreet/x"}))

import sqlite3  # noqa: E402
_db = sqlite3.connect(str(ROOT / "legal_directory.db"))
check("every built deal whose headline states a size carries that size (the legacy export had none)",
      not [h for h, v in _db.execute("select headline, value_raw from deals") if v is None and _ex.parse_money(h)],
      [h for h, v in _db.execute("select headline, value_raw from deals") if v is None and _ex.parse_money(h)][:3])
check("no forum thread is in the built deals",
      not list(_db.execute("select url from deals where url like '%legallyindia.com/convos/%'")))

# --------------------------------------------------------- fetch behaviour
print("fetching")
check("User-Agent identifies the project and avoids the words these sites block",
      "bot" not in ns.USER_AGENT.lower() and "crawler" not in ns.USER_AGENT.lower()
      and "scraper" in ns.USER_AGENT.lower(), ns.USER_AGENT)


class StubResp:
    def __init__(self, code, text="", enc="utf-8"):
        self.status_code, self.text, self.encoding = code, text, enc

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class StubSession:
    def __init__(self, pages, blocked=()):
        self.pages, self.blocked, self.headers, self.calls = pages, set(blocked), {}, []

    def get(self, url, timeout=30):
        self.calls.append(url)
        if url in self.blocked:
            return StubResp(403)
        if url in self.pages:
            return StubResp(200, self.pages[url])
        return StubResp(404)


def stub_fetcher(pages, blocked=()):
    f = RealFetcher(0)
    f.session = StubSession(pages, blocked)
    return f


f = stub_fetcher({}, blocked=["https://x.test/a"])
try:
    f.get("https://x.test/a")
    check("403 stops the source", False)
except ns.Blocked:
    check("403 stops the source", True)

# discovery: sitemap + listing, only article paths, no query-string pagination
pages = {
    "https://www.legaleraonline.com/news-sitemap-daily.xml": (FX / "le_sitemap.xml").read_text(),
    "https://www.legaleraonline.com/sitemap-daily.xml": "<urlset></urlset>",
    "https://www.legaleraonline.com/deal-street": (FX / "le_listing.html").read_text(),
}
fetch = stub_fetcher(pages)
urls = ns.discover(LE, fetch, pages=1)
check("discovery returns only Deal Street article URLs, deduped",
      urls and all(re.search(r"/deal-street/[^/]+-\d+$", u) for u in urls) and len(urls) == len(set(urls)),
      urls[:3])
check("discovery never asks for ?page= (robots.txt)", not any("?" in c for c in fetch.session.calls))
check("listing URLs use path-suffix pagination",
      LE.listing_urls(3)[-1].endswith("/deal-street/3") and LL.listing_urls(3)[-1].endswith("/law-firms/deals/3"))
ll_urls = ns.discover(LL, stub_fetcher({"https://www.livelaw.in/law-firms/deals": (FX / "ll_listing.html").read_text()}), pages=1)
check("LiveLaw discovery keeps only /law-firms/deals/ articles",
      ll_urls and all("/law-firms/deals/" in u for u in ll_urls), ll_urls[:2])

# run loop: writes records, second run adds nothing, a block mid-run keeps what was saved
with tempfile.TemporaryDirectory() as tmp:
    class TmpLiveLaw(LiveLaw):
        out_path = Path(tmp) / "out.json"
        seen_path = Path(tmp) / "seen.json"

    art = {u: html_of(n.replace(".html.gz", "")) for n, u in MANIFEST.items() if n.startswith("ll_")}
    listing = "<html><body>" + "".join(f'<a href="{u}">a</a>' for u in art) + "</body></html>"
    pg = dict(art)
    pg["https://www.livelaw.in/law-firms/deals"] = listing
    orig_fetcher, orig_known = ns.Fetcher, ns.known_urls_everywhere
    ns.Fetcher = lambda delay: stub_fetcher(pg)
    ns.known_urls_everywhere = lambda: (set(), set())
    try:
        ns.run(TmpLiveLaw(), ["--pages", "1", "--max-new", "50"])
        first = json.loads(TmpLiveLaw.out_path.read_text())
        ns.run(TmpLiveLaw(), ["--pages", "1", "--max-new", "50"])
        second = json.loads(TmpLiveLaw.out_path.read_text())
        check("run stores one record per article", len(first) == len(art) and len({d["url"] for d in first}) == len(first),
              (len(first), len(art)))
        check("second run adds nothing (seen/stored URLs are skipped)", len(second) == len(first))
    finally:
        ns.Fetcher, ns.known_urls_everywhere = orig_fetcher, orig_known

# ------------------------------------------------- cross-source de-duplication
print("cross-source de-duplication against Bar & Bench records")
bb = json.loads((FX / "bb_counterparts.json").read_text())
new = [parse(LE, f"le_{i}") for i in (1, 2, 3, 4)] + [parse(LL, f"ll_{i}") for i in range(1, 9)]
merged, report = dedup.dedupe(bb + new, is_personnel_move, best_role)
groups = report["merged_groups"]


def grouped(substr_a, substr_b):
    for g in groups:
        heads = [g["kept"]["headline"]] + [d["headline"] for d in g["duplicates"]]
        if any(substr_a.lower() in h.lower() for h in heads) and any(substr_b.lower() in h.lower() for h in heads):
            return len(heads)
    return 0


# each pair: the Bar & Bench headline fragment and its LiveLaw/Legal Era twin
pairs = [
    ("RPG Life Sciences", "RPG Life Sciences On API"),
    ("Happiest Minds", "Happiest Minds Technologies"),
    ("Odyssey Energy", "Procure VI"),
    ("PwC India - PwC US", "PwC US On Joint Venture"),
    ("NIIF on setting up", "NIIF on First Close"),
    ("Samara Capital's", "Samara Capital To Invest"),
    ("JSW One ₹3,054", "JSW Steel And JSW Cement"),
]
for a, b in pairs:
    check(f"merged: {a} / {b}", grouped(a, b) == 2, grouped(a, b))

distinct = ["Multiples Private Equity", "Dilip Buildcon", "Inox Green", "Godrej Finance", "India Resurgence"]
for key in distinct:
    n = sum(1 for m in merged if key.lower() in m["headline"].lower()
            or any(key.lower() in o["headline"].lower() for o in m.get("also_reported_by", [])))
    check(f"genuinely new, still one deal: {key}", n == 1, n)

expected_total = len(bb) + len(new) - len(pairs)
check("no other merges happened", len(merged) == expected_total, (len(merged), expected_total))

m = [x for x in merged if "RPG Life Sciences" in x["headline"]][0]
check("merged record keeps the richest copy and lists the other outlet",
      len(m["people"]) >= 15 and {o["source"] for o in m["also_reported_by"]} <= {"Bar & Bench", "LiveLaw"},
      (len(m["people"]), m.get("also_reported_by")))
names = [x["name"].lower() for x in m["people"]]
check("merged record has no duplicate people", len(names) == len(set(names)))

again, rep2 = dedup.dedupe(merged, is_personnel_move, best_role)
check("deduping the merged set removes nothing more", rep2["removed_duplicates"] == 0, rep2["merged_groups"])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all source tests pass")
