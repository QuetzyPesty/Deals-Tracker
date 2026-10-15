"""Tests for the CCI scraper (and, below, the deal matcher).
Run:  python3 tests/test_cci.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scraper"))

import news_source as ns  # noqa: E402
import scrape_cci as cci  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "cci"
FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


ROWS = json.loads((FX / "rows.json").read_text())

print("scraper")
n = cci.normalize(ROWS[0])
check("row is normalised: ISO dates, absolute links, clean parties",
      n["combination_no"] and n["notified"] == "2026-10-06" and n["decided"] is None
      and n["summary_url"].startswith("https://www.cci.gov.in/") and "&amp;" not in n["parties"], n)
check("order link is captured when present", any(cci.normalize(r)["order_url"] for r in ROWS))
check("unparseable date becomes None, not a crash",
      cci.normalize({"combination_no": "C-1", "notification_date": "n/a"})["notified"] is None)
u = cci.build_url(100)
check("request carries the full DataTables column set (a minimal query 500s)",
      "columns%5B8%5D%5Bdata%5D=order_files" in u and "start=100" in u and "length=100" in u)


class Stub:
    def __init__(self, pages, fail_at=None, blocked_at=None):
        self.pages, self.fail_at, self.blocked_at, self.calls = pages, fail_at, blocked_at, []

    def get(self, url):
        start = int(url.split("start=")[1].split("&")[0])
        self.calls.append(start)
        if self.blocked_at is not None and start >= self.blocked_at:
            raise ns.Blocked("403 from www.cci.gov.in")
        if self.fail_at is not None and start >= self.fail_at:
            raise RuntimeError("HTTP 500")
        chunk = self.pages[start // cci.PAGE_SIZE]
        return json.dumps({"draw": 1, "recordsTotal": 450, "recordsFiltered": 450, "data": chunk})


def pages_of(n_pages):
    out = []
    for p in range(n_pages):
        out.append([{**ROWS[i % len(ROWS)], "combination_no": f"C-2026/01/{p * 100 + i}", "id": str(p * 100 + i)}
                    for i in range(3)])
    return out


with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / "cci.json"
    f = Stub(pages_of(5))
    st = cci.run(f, out, log=lambda *_: None, sleep=lambda _: None)
    check("first run is a full walk of the register", f.calls == [0, 100, 200, 300, 400] and st["stored"] == 15, (f.calls, st))
    f = Stub(pages_of(5))
    cci.run(f, out, log=lambda *_: None, sleep=lambda _: None)
    check("later runs only refetch the newest pages", f.calls == [0, 100, 200], f.calls)
    before = json.loads(out.read_text())
    ch = pages_of(5)
    ch[0][0] = {**ch[0][0], "order_status": "Approved", "decision_date": "10/10/2026"}
    cci.run(Stub(ch), out, log=lambda *_: None, sleep=lambda _: None)
    after = {r["combination_no"]: r for r in json.loads(out.read_text())}
    check("a filing's status/decision update replaces the stored row (no duplicates)",
          len(after) == len(before) and after["C-2026/01/0"]["status"] == "Approved"
          and after["C-2026/01/0"]["decided"] == "2026-10-10")
    snapshot = out.read_text()
    st = cci.run(Stub(pages_of(5), blocked_at=100), out, log=lambda *_: None, sleep=lambda _: None)
    check("403 keeps the stored file untouched", st.get("blocked") and out.read_text() == snapshot)
    st = cci.run(Stub(pages_of(5), fail_at=0), out, log=lambda *_: None, sleep=lambda _: None)
    check("endpoint failure keeps the stored file untouched", "failed" in st and out.read_text() == snapshot)

a = {"combination_no": "C-2020/03/735", "parties": "1. Outotec Oyj & 2. Metso Oyj", "status": "", "decided": "2020-06-18",
     "summary_url": None, "order_url": "u", "kind": "Under Section 31", "form": "I", "cci_id": "253", "notified": "2020-03-12"}
b = {**a, "parties": "Outotec Oyj and Metso Oyj and Metso Outotec Finland", "status": "Approved with modification", "order_url": None, "cci_id": "732"}
m, _ = cci.merge([], [a, b])
check("duplicate register rows for one filing are combined, keeping every party and any non-empty field",
      len(m) == 1 and "Metso Outotec Finland" in m[0]["parties"] and m[0]["status"] == "Approved with modification"
      and m[0]["order_url"] == "u", m)
m2, _ = cci.merge([], [{**a, "parties": "Outotec Oyj and Metso Oyj"}, {**a, "parties": "1. Outotec Oyj and Metso Oyj and X"}])
check("a row whose parties contain the other's does not get concatenated", m2[0]["parties"].count("Outotec") == 1, m2[0]["parties"])

# ------------------------------------------------------------------ matcher
import cci_match as cm  # noqa: E402
from datetime import date, timedelta  # noqa: E402

print("matcher")


def filing(no, parties, notified, status="Approved", decided=None):
    return {"combination_no": no, "parties": parties, "notified": notified, "decided": decided or notified,
            "status": status, "summary_url": f"https://www.cci.gov.in/s/{no}", "order_url": None, "kind": "", "form": "", "cci_id": no}


def deal(headline, reported, **kw):
    return {"headline": headline, "client": kw.get("client"), "law_firms": kw.get("firms", []),
            "published_at": reported, "url": "u:" + headline[:20], **{k: v for k, v in kw.items() if k not in ("client", "firms")}}


def iso(d): return d.isoformat()


R = date(2026, 6, 1)
FILINGS = [
    filing("C-2026/04/1408", "Cube Highways Trust", iso(R + timedelta(days=20))),
    filing("C-2024/05/1151", "1. Coforge Limited 2. Cigniti Technologies Limited", iso(R + timedelta(days=-10))),
    filing("C-2018/11/614", "DENSO Corporation, Japan", "2018-11-06"),
    filing("C-2021/04/829", "1. Unifeeder ISC FZCO; 2. Transworld Holdings Limited; 3. Shreyas Shipping and Logistics Limited",
           iso(R + timedelta(days=5))),
    filing("C-2025/03/1254", "1. Lloyds Metals and Energy Limited 2. Thriveni Earthmovers and Infra Private Limited", iso(R - timedelta(days=30))),
    filing("C-2026/05/1500", "Brookfield Asset Management Ltd.", iso(R + timedelta(days=5))),
    filing("C-2026/05/1501", "Brookfield Asset Management Ltd.", iso(R + timedelta(days=12))),
]
IDX = cm.FilingIndex(FILINGS)


def m(h, reported=iso(R), **kw):
    return cm.match_deal(deal(h, reported, **kw), IDX)


r, info = m("SAM advises Kotak Special Situations Fund on sale of HKR Roadways to Cube Highways")
check("one full rare party name, in the window, unique -> matched", r and r["combination_no"] == "C-2026/04/1408", info)
r, info = m("JSA leads Coforge to close Cigniti acquisition")
check("acquirer AND target both named -> matched", r and r["combination_no"] == "C-2024/05/1151", info)
r, info = m("Trilegal advises Sona Comstar on acquisition of stake in a DENSO Corp unit", reported=iso(R))
check("a 2018 filing is not attached to a 2026 deal (date window)", r is None, r)
r, info = m("Trilegal advises Cube Highways on QIP", reported=iso(R))
check("an IPO/QIP/loan deal is never matched (type gate)", r is None and info["reason"] == "type-gate", info)
r, info = m("Trilegal advises Cube Highways on acquisition of road assets", reported=None)
check("one name and no date is not enough", r is None, info)
r, info = m("ELP advises Maersk on forming JV with Bainbridge to operate UAE shipping pool")
check("a sector word ('shipping') alone is never evidence", r is None, info)
r, info = m("ELP advises Transworld on forming JV with Bainbridge to operate UAE shipping pool")
check("a full party name is evidence (Transworld Holdings), on its own, in the window",
      r and r["combination_no"] == "C-2021/04/829" or r is None and info["reason"] != "type-gate", (r, info))
r, info = m("Tata Steel acquires stake in Thriveni Pellets", reported=iso(R))
check("sharing one word of a longer party name is a group name, not the party (Thriveni)", r is None, (r, info))
r, info = m("Khaitan advised Brookfield Asset Management on acquisition of warehousing assets")
check("two equally good filings -> neither is attached, both listed as candidates",
      r is None and info["reason"] == "ambiguous" and len(info["candidates"]) == 2, info)
r, info = m("Cyril Amarchand Mangaldas cements Ambuja merger", reported=None)
check("a verb next to a name is not a second entity", r is None, info)

late = deal("SAM advises Cube Highways on acquisition of road assets", iso(R + timedelta(days=cm.DAYS_BEFORE + 400)))
check("outside the window on the far side -> no match", cm.match_deal(late, IDX)[0] is None)
early = deal("SAM advises Cube Highways on acquisition of road assets", iso(R - timedelta(days=cm.DAYS_AFTER + 400)))
check("outside the window on the near side -> no match", cm.match_deal(early, IDX)[0] is None)

# one filing, two different deals -> neither gets it (SMBC buying YES Bank vs selling Kotak shares)
smbc = [filing("C-2025/06/1294", "Sumitomo Mitsui Banking Corporation", iso(R - timedelta(days=90)))]
two = [deal("SMBC acquires further stake in YES Bank from Carlyle; JSA, SAM advise Sumitomo Mitsui Banking", iso(R)),
       deal("JSA, TT&A, Ashurst act on Sumitomo Mitsui Banking sale of shares in Kotak Mahindra Bank", iso(R + timedelta(days=3)))]
two[0]["url"], two[1]["url"] = "u:yes", "u:kotak"
rep2 = cm.attach(two, smbc)
check("a filing claimed by two different deals is attached to neither, and reported",
      "cci" not in two[0] and "cci" not in two[1] and "C-2025/06/1294" in rep2["filing_claimed_by_several_deals"], rep2)

ds = [deal("JSA leads Coforge to close Cigniti acquisition", iso(R)), deal("Trilegal advises X on QIP", iso(R))]
rep = cm.attach(ds, FILINGS)
check("attach() annotates matched deals only and reports why the rest were skipped",
      "cci" in ds[0] and "cci" not in ds[1] and rep["reasons"].get("type-gate") == 1 and len(rep["matched"]) == 1, rep)
check("a match carries the status, dates and links a reader needs",
      all(k in ds[0]["cci"] for k in ("combination_no", "status", "notified", "decided", "summary_url", "parties", "confidence")))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all cci tests pass")
