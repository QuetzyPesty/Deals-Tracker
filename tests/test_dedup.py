"""Tests for scraper/dedup.py: cross-source de-duplication.

Two failure directions matter, and the suite pins both:

  * a duplicate that survives is counted as two deals and credits every lawyer
    twice (the "positives" below);
  * a false merge silently deletes a real deal (the "negatives" below, which
    are built from near-misses found in the real corpus, not invented).

Run:  python3 tests/test_dedup.py
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scraper"))

from build_directory import is_personnel_move, best_role  # noqa: E402
import dedup  # noqa: E402

FAILS = []


def rec(headline, url, firms, source="", snippet="", people=(), client="X",
        value=None, published_at=None, types=()):
    r = {
        "headline": headline, "url": url, "source": source, "client": client,
        "snippet": snippet, "law_firms": list(firms),
        "transaction_types": list(types),
        "people": [{"name": n, "role": "Partner", "firm": f} for n, f in people],
    }
    if value:
        r["deal_value"] = value
    if published_at:
        r["published_at"] = published_at
    return r


def run(records):
    merged, report = dedup.dedupe(records, is_personnel_move, best_role)
    return merged, report


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def n_after(records):
    return len(run(records)[0])


# ---------------------------------------------------------------- positives
BB = "https://www.barandbench.com/law-firms/dealstreet/"
LE = "https://www.legaleraonline.com/deal-street/"
LL = "https://www.livelaw.in/law-firms/deals/"

print("positives: must collapse to one deal")

check("same URL with tracking param and trailing slash",
      n_after([rec("A advises B on C", BB + "x-1", ["Khaitan & Co"]),
               rec("Something else entirely", BB + "x-1/?utm_source=tw", ["Khaitan & Co"])]) == 1)

check("same host + slug under the old and new Bar & Bench path",
      n_after([rec("JSA advises Foo on bar", "https://www.barandbench.com/dealstreet/jsa-advises-foo", ["JSA"]),
               rec("JSA advises Foo on bar", BB + "jsa-advises-foo", ["JSA"])]) == 1)

check("same headline across outlets, case and punctuation differ",
      n_after([rec("Khaitan & Co advises Godrej Finance on gold loan acquisition", BB + "a", ["Khaitan & Co"]),
               rec("Khaitan & Co Advises Godrej Finance On Gold Loan Acquisition!", LL + "b-1", ["Khaitan & Co"])]) == 1)

check("same deal reworded, abbreviations and currency spelled differently",
      n_after([
          rec("Khaitan & Co advises Multiples PE on $100 million investment in Brahma AI",
              BB + "k-multiples", ["Khaitan & Co"], value={"amount_inr": 8_800_000_000, "source": "headline"}),
          rec("Khaitan & Co Advised Multiples Private Equity On USD 100 Million Investment In Brahma AI",
              LE + "khaitan-multiples-990534", ["Khaitan & Co"],
              value={"amount_inr": 8_800_000_000, "source": "headline"}),
      ]) == 1)

check("single-entity deal, verb tense differs (core headline)",
      n_after([rec("Khaitan & Co advises Zomato on QIP", BB + "z", ["Khaitan & Co"]),
               rec("Khaitan & Co Advised Zomato On Its QIP", LE + "z-1", ["Khaitan & Co"])]) == 1)

check("same fund number across outlets, money in different units",
      n_after([rec("Trilegal advises Alpha Partners on ₹3,000 crore Alpha Fund II final close", BB + "af", ["Trilegal"]),
               rec("Trilegal Advised Alpha Partners On INR 30 Billion Alpha Fund II Final Close", LL + "af-2", ["Trilegal"])]) == 1)

check("three outlets, one deal -> one record, two also_reported_by",
      (lambda m: len(m) == 1 and len(m[0].get("also_reported_by", [])) == 2)(run([
          rec("Trilegal advises CPPIB on INR 30 billion investment in Prestige Hospitality Ventures",
              BB + "t", ["Trilegal"], source="Bar & Bench"),
          rec("Trilegal Advised CPPIB On INR 30 Billion Investment In Prestige Hospitality Ventures",
              LE + "t-990329", ["Trilegal"]),
          rec("Trilegal advises CPPIB on investment in Prestige Hospitality Ventures",
              LL + "t-55", ["Trilegal"]),
      ])[0]))

LEDE = ("Khaitan & Co advised the National Investment and Infrastructure Fund on the first closing "
        "of NIIF Infrastructure Fund II, its second flagship fund, which raised commitments from "
        "domestic and international investors")
check("verb and legal-entity expansion (real Odyssey Energy ECB pair)",
      n_after([rec("Dua Associates advises Odyssey Energy Solutions on availing €7 million ECB from BIO", BB + "od", ["Dua Associates"]),
               rec("Dua Associates advises Odyssey Energy Solutions Procure VI Pvt. Ltd. on EUR 7 million ECB from BIO",
                   LL + "od-550953", ["Dua Associates"])]) == 1)

check("two sides of one JV reported by different counsel (real PwC pair)",
      n_after([rec("JSA acts on PwC India - PwC US joint venture", BB + "pwc", ["JSA Advocates & Solicitors"]),
               rec("Khaitan & Co Advises PwC US On Joint Venture With PwC India", LL + "pwc-552801", ["Khaitan & Co"])]) == 1)

check("firm list empty on one side, seven shared entity names (real Samara pair)",
      n_after([rec("Trilegal, JSA, Solicis Lex act on Samara Capital's stake acquisition in Associated Road Carriers, Calyx Container Terminals",
                   BB + "sam", ["Trilegal", "JSA Advocates & Solicitors", "Solicis Lex"]),
               rec("Samara Capital To Invest In Associated Road Carriers, Calyx Container Terminals To Build Integrated B2B Logistics Platform",
                   LL + "sam-552634", [])]) == 1)

check("IPO: headline total vs a body-stated fresh-issue tranche is not a conflict",
      n_after([rec("Cyril Amarchand Mangaldas, Trilegal, Khaitan & Co act on JSW One ₹3,054 crore proposed IPO", BB + "jsw",
                   ["Cyril Amarchand Mangaldas", "Trilegal", "Khaitan & Co"],
                   value={"amount_inr": 30_540_000_000, "source": "headline"}),
               rec("Khaitan & Co Advised JSW Steel And JSW Cement On The Proposed IPO Of JSW One Platforms Limited",
                   LE + "jsw-990301", ["Khaitan & Co"],
                   value={"amount_inr": 13_000_000_000, "source": "body"})]) == 1)

TRILEGAL_TEAM = [("Sai Krishna Bharathan", "Trilegal"), ("Raina Mitra", "Trilegal"),
                 ("Archi Jain", "Trilegal"), ("Annika Bohra", "Trilegal")]
check("one deal, buyer's and target's counsel each published separately, a hub lists both (real Prestige case)",
      n_after([
          rec("CPP Investments to invest ₹3,000 crore in Prestige Hospitality Ventures; Trilegal, CAM advise", BB + "hub",
              ["Trilegal", "Cyril Amarchand Mangaldas"], people=TRILEGAL_TEAM,
              value={"amount_inr": 30_000_000_000, "source": "headline"}),
          rec("Trilegal Advised CPPIB On INR 30 Billion Investment In Prestige Hospitality Ventures", LE + "t-1", ["Trilegal"],
              people=TRILEGAL_TEAM, value={"amount_inr": 30_000_000_000, "source": "headline"}),
          rec("Cyril Amarchand Mangaldas Advised Prestige Group On CPP Investments’ Proposed INR 3,000 Crore Investment...",
              LE + "c-2", ["Cyril Amarchand Mangaldas"], value={"amount_inr": 30_000_000_000, "source": "headline"}),
      ]) == 1)

check("copied press-release text under unrelated headlines",
      n_after([rec("NIIF closes first round of second flagship fund", BB + "n1", ["Khaitan & Co"], snippet=LEDE),
               rec("Khaitan & Co Advises NIIF on First Close of Its Second Flagship Infrastructure Fund",
                   LL + "n2-1", ["Khaitan & Co"], snippet=LEDE)]) == 1)

# ---------------------------------------------------------------- negatives
print("negatives: must stay separate (a false merge deletes a real deal)")

check("same firm + client, different target",
      n_after([rec("Khaitan & Co advised Godrej Finance on acquisition of gold loan business of Kanakadurga Finance",
                   LL + "g1-1", ["Khaitan & Co"]),
               rec("Khaitan & Co advised Godrej Finance on issuance of non-convertible debentures",
                   LL + "g2-2", ["Khaitan & Co"])]) == 2)

check("different amounts to the same trust (the real Indus Infra Trust pair)",
      n_after([rec("JSA advises UBI, HDFC Bank on sanctioning ₹3,495 crore to Indus Infra Trust",
                   BB + "i1", ["JSA Advocates & Solicitors"]),
               rec("JSA advises HDFC Bank on sanctioning 1,940 crores to Indus Infra Trust",
                   BB + "i2", ["JSA Advocates & Solicitors"])]) == 2)

check("Series A vs Series B of the same company",
      n_after([rec("JSA advises Acme on Series A fundraise", BB + "a", ["JSA Advocates & Solicitors"]),
               rec("JSA advises Acme on Series B fundraise", BB + "b", ["JSA Advocates & Solicitors"])]) == 2)

check("Series A vs Series B with two shared entities (round mark veto)",
      n_after([rec("JSA advises Acme Robotics and Beta Ventures on Series A fundraise", BB + "ra", ["JSA Advocates & Solicitors"]),
               rec("JSA advises Acme Robotics and Beta Ventures on Series B fundraise", LL + "rb-1", ["JSA Advocates & Solicitors"])]) == 2)

check("Fund II vs Fund III of the same manager",
      n_after([rec("Khaitan & Co advises Everstone Capital on Everstone Fund II first close", BB + "f2", ["Khaitan & Co"]),
               rec("Khaitan & Co Advised Everstone Capital On Everstone Fund III First Close", LE + "f3-1", ["Khaitan & Co"])]) == 2)

check("same project, different capacity",
      n_after([rec("JSA advises Renew on financing for 320 MW solar project in Rajasthan", BB + "mw1", ["JSA Advocates & Solicitors"]),
               rec("JSA advises Renew on financing for 500 MW solar project in Rajasthan", BB + "mw2", ["JSA Advocates & Solicitors"])]) == 2)

check("two IPOs by the same firm pair",
      n_after([rec("Trilegal, JSA act on Shivalaya Construction ₹450 crore IPO", BB + "s", ["Trilegal", "JSA Advocates & Solicitors"]),
               rec("Trilegal, JSA act on Atlanta Electricals ₹687 crore IPO", BB + "a", ["Trilegal", "JSA Advocates & Solicitors"])]) == 2)

check("same wording template, different company (Series A pattern)",
      n_after([rec("JSA acts on Deccan AI $25 million Series A fundraise", BB + "d", ["JSA Advocates & Solicitors"]),
               rec("JSA acts on Bacancy Systems Series A fundraise", BB + "b", ["JSA Advocates & Solicitors"])]) == 2)

check("same deal wording but dates months apart (recurring issuer)",
      n_after([rec("Trilegal advises Foo Infra on bond issuance", LE + "x-1", ["Trilegal"], published_at="2026-01-05"),
               rec("Trilegal advises Foo Infra on bond issuance programme", LL + "y-2", ["Trilegal"], published_at="2026-09-20")]) == 2)

check("same team on two different deals does not merge on people alone",
      n_after([rec("Khaitan advises Alpha on acquisition of Beta", BB + "p1", ["Khaitan & Co"],
                   people=[("Haigreve Khaitan", "Khaitan & Co"), ("Aditi Singhvi", "Khaitan & Co"), ("Bhaskar Vishwajeet", "Khaitan & Co")]),
               rec("Khaitan advises Gamma on divestment to Delta", LL + "p2-1", ["Khaitan & Co"],
                   people=[("Haigreve Khaitan", "Khaitan & Co"), ("Aditi Singhvi", "Khaitan & Co"), ("Bhaskar Vishwajeet", "Khaitan & Co")])]) == 2)

check("two different people-move articles at one firm",
      n_after([rec("Anil Kumar joins Khaitan & Co as Partner", BB + "m1", ["Khaitan & Co"], client=None),
               rec("Sunita Rao joins Khaitan & Co as Partner", LE + "m2-9", ["Khaitan & Co"], client=None)]) == 2)

check("different counsel, same company, different transactions",
      n_after([rec("AZB advises Tata Power on acquisition of Alpha Solar", BB + "t1", ["AZB & Partners"]),
               rec("Trilegal advises Tata Power on sale of Beta Hydro stake", LE + "t2-1", ["Trilegal"])]) == 2)

check("different counsel, Series A vs Series B of one company",
      n_after([rec("Trilegal advises Acme Robotics on Series A fundraise", BB + "r1", ["Trilegal"]),
               rec("JSA advises Acme Robotics on Series B fundraise", LE + "r2-1", ["JSA Advocates & Solicitors"])]) == 2)

check("same issuer and name words, QIP vs bond issue (type conflict)",
      n_after([rec("Trilegal advises Reliance Retail Ventures on ₹2,000 crore QIP", BB + "q", ["Trilegal"]),
               rec("Trilegal Advised Reliance Retail Ventures On Its Bond Issue", LE + "b-1", ["Trilegal"])]) == 2)

check("same issuer, IPO vs an acquisition by it",
      n_after([rec("Khaitan & Co advises Zenith Foods Limited on its IPO", BB + "i", ["Khaitan & Co"]),
               rec("Khaitan & Co Advised Zenith Foods Limited On Acquisition Of Snackco", LE + "a-1", ["Khaitan & Co"])]) == 2)

check("a hub record must not bridge two spokes that contradict each other",
      n_after([
          rec("Trilegal, JSA act on Acme acquisition of Beta Industries", BB + "h", ["Trilegal", "JSA Advocates & Solicitors"]),
          rec("Trilegal advises Acme on ₹100 crore acquisition of Beta Industries", LE + "s1-1", ["Trilegal"],
              value={"amount_inr": 1_000_000_000, "source": "headline"}),
          rec("JSA advises Acme on ₹900 crore acquisition of Beta Industries", LL + "s2-2", ["JSA Advocates & Solicitors"],
              value={"amount_inr": 9_000_000_000, "source": "headline"}),
      ]) == 2)

check("no shared firm and different clients",
      n_after([rec("AZB advises Alpha on acquisition of Beta", BB + "z1", ["AZB & Partners"]),
               rec("Trilegal advises Gamma on acquisition of Delta", LE + "z2-1", ["Trilegal"])]) == 2)

# --------------------------------------------------------------- properties
print("properties")

a = rec("Khaitan advises Alpha Corp on acquisition of Beta Ltd", BB + "c1", ["Khaitan & Co"])
b = rec("Khaitan advises Alpha Corp on acquisition of Beta Ltd stake", LL + "c2-1", ["Khaitan & Co"])
c = rec("Khaitan advises Alpha Corp on acquisition of Gamma Ltd stake", LE + "c3-1", ["Khaitan & Co"])
merged, _ = run([a, b, c])
check("no chaining: B~A and B~C must not pull A and C together", len(merged) == 2,
      f"got {len(merged)} records: {[m['headline'] for m in merged]}")

pool = [
    rec("Khaitan & Co advises Multiples PE on $100 million investment in Brahma AI", BB + "m", ["Khaitan & Co"]),
    rec("Khaitan & Co Advised Multiples Private Equity On USD 100 Million Investment In Brahma AI", LE + "m-1", ["Khaitan & Co"]),
    rec("Trilegal advises CPPIB on INR 30 billion investment in Prestige", BB + "t", ["Trilegal"]),
    rec("Trilegal Advised CPPIB On INR 30 Billion Investment In Prestige", LL + "t-2", ["Trilegal"]),
    rec("AZB advises Foo on Bar", BB + "f", ["AZB & Partners"]),
    rec("JSA acts on Deccan AI Series A", BB + "d", ["JSA Advocates & Solicitors"]),
    rec("JSA acts on Bacancy Series A", BB + "e", ["JSA Advocates & Solicitors"]),
]
sig = lambda ms: sorted(m["url"] for m in ms)
base = sig(run(pool)[0])
ok = True
for seed in range(8):
    shuffled = pool[:]
    random.Random(seed).shuffle(shuffled)
    ok &= sig(run(shuffled)[0]) == base
check("order independent (8 shuffles)", ok)

once, _ = run(pool)
twice, _ = run(once)
check("idempotent: deduping the output removes nothing more", len(twice) == len(once))

m = run([
    rec("Khaitan & Co advises Multiples PE on $100 million investment in Brahma AI", BB + "m", ["Khaitan & Co"],
        people=[("Haigreve Khaitan", "Khaitan & Co")], source="Bar and Bench"),
    rec("Khaitan & Co Advised Multiples Private Equity On USD 100 Million Investment In Brahma AI", LE + "m-1",
        ["Khaitan & Co"], people=[("Haigreve Khaitan", None), ("Aditi Singhvi", "Khaitan & Co")],
        published_at="2026-10-12"),
])[0]
check("merge unions people, fills a missing firm, keeps one row per person",
      len(m) == 1 and len(m[0]["people"]) == 2
      and {p["name"]: p["firm"] for p in m[0]["people"]}["Haigreve Khaitan"] == "Khaitan & Co", m)
check("source labels are normalised (richer record is primary, both labelled)",
      {m[0]["source"], m[0]["also_reported_by"][0]["source"]} == {"Bar & Bench", "Legal Era"})

# ---------------------------------------------------------- real-corpus gate
print("real corpus")
recs = json.loads((ROOT / "structured_deals.json").read_text()) \
    + json.loads((ROOT / "barandbench_deals.json").read_text())
merged, report = run(recs)
check("real corpus: merges stay a small fraction (guards against a runaway rule)",
      report["removed_duplicates"] <= 0.05 * report["input_records"],
      (report["removed_duplicates"], report["input_records"]))
heads = " ".join(r["headline"] for r in merged)
check("real corpus: both Indus Infra Trust deals survive",
      sum("Indus Infra Trust" in r["headline"] for r in merged) >= 2)
again, rep2 = run(merged)
check("real corpus: output is a fixed point", rep2["removed_duplicates"] == 0, rep2["merged_groups"])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all dedup tests pass")
