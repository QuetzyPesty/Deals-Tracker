"""
Competition Commission of India: combination filings (merger-control notices).

This is an enrichment source, not a deal source. A filing says which parties
notified a combination, when, and what the CCI decided; it names no law firms.
cci_match.py joins filings onto the deals we already hold.

The CCI site loads its table from a server-side DataTables endpoint on the
same URL as the page; it answers plain GETs when the full column parameters are
present (a minimal query returns HTTP 500). Order and decision links on each row
are public PDFs / detail pages.

Incremental by default: the newest NEWEST_PAGES pages are refetched every run
(only recent filings change status), merged by combination number into
cci_filings.json. `--full` walks the whole register (about 15 requests).

Run:  python3 scraper/scrape_cci.py [--full]
"""
import html
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

SCRAPER_DIR = Path(__file__).parent
BASE = SCRAPER_DIR.parent
sys.path.insert(0, str(SCRAPER_DIR))
sys.path.insert(0, str(BASE))

ENDPOINT = "https://www.cci.gov.in/combination/orders-section31"
OUT_PATH = BASE / "cci_filings.json"
PAGE_SIZE = 100
NEWEST_PAGES = 3
COLUMNS = ["DT_RowIndex", "combination_no", "party_name", "form_type",
           "notification_date", "order_status", "decision_date", "summary_files", "order_files"]


def build_url(start, length=PAGE_SIZE):
    params = [("draw", "1")]
    for i, c in enumerate(COLUMNS):
        params += [
            (f"columns[{i}][data]", c), (f"columns[{i}][name]", c),
            (f"columns[{i}][searchable]", "true"), (f"columns[{i}][orderable]", "true"),
            (f"columns[{i}][search][value]", ""), (f"columns[{i}][search][regex]", "false"),
        ]
    params += [
        ("order[0][column]", "4"), ("order[0][dir]", "desc"),
        ("start", str(start)), ("length", str(length)),
        ("search[value]", ""), ("search[regex]", "false"),
    ]
    return ENDPOINT + "?" + urllib.parse.urlencode(params)


def _date(value):
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def _links(fragment):
    return re.findall(r'href="([^"]+)"', fragment or "")


def clean_parties(text):
    t = html.unescape(text or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(t.split())


def normalize(row):
    """One CCI row -> the record we store. Dates are ISO; links are absolute."""
    summary = _links(row.get("summary_files"))
    order = _links(row.get("order_files"))
    rid = str(row.get("id") or "")
    return {
        "combination_no": (row.get("combination_no") or "").strip(),
        "cci_id": rid,
        "kind": row.get("order_type") or "",
        "form": row.get("form_type") or "",
        "parties": clean_parties(row.get("party_name")),
        "notified": _date(row.get("notification_date")),
        "decided": _date(row.get("decision_date")),
        "status": (row.get("order_status") or "").strip(),
        "summary_url": summary[0] if summary else None,
        "order_url": order[0] if order else None,
    }


def fetch_rows(fetcher, start, length=PAGE_SIZE):
    payload = json.loads(fetcher.get(build_url(start, length)))
    if "data" not in payload:
        raise RuntimeError(f"unexpected CCI response: {str(payload)[:120]}")
    return payload["data"], int(payload.get("recordsTotal") or 0)


def _combine(a, b):
    """Two register rows for one filing (it can be listed under both "Under
    Section 31" and "Cases Approved with Modification", with differently
    worded party text). Keep every party name either row has."""
    pa, pb = a["parties"], b["parties"]
    if pa.lower() in pb.lower():
        parties = pb
    elif pb.lower() in pa.lower():
        parties = pa
    else:
        parties = f"{pa}; {pb}"
    out = dict(a)
    out["parties"] = parties
    for k in ("status", "decided", "summary_url", "order_url", "kind", "form", "cci_id"):
        out[k] = a.get(k) or b.get(k)
    return out


def merge(existing, fresh):
    """One record per combination number. Rows for the same filing inside one
    fetch are combined; a freshly fetched filing then replaces the stored one
    (the fetch is the complete, current view of it)."""
    batch = {}
    for r in fresh:
        no = r["combination_no"]
        if not no:
            continue
        batch[no] = _combine(batch[no], r) if no in batch else r
    by_no = {r["combination_no"]: r for r in existing}
    changed = 0
    for no, r in batch.items():
        if by_no.get(no) != r:
            changed += 1
        by_no[no] = r
    out = sorted(by_no.values(), key=lambda r: (r["notified"] or "", r["combination_no"]), reverse=True)
    return out, changed


def run(fetcher, out_path=OUT_PATH, full=False, log=print, sleep=time.sleep, gap=1.2):
    from news_source import Blocked
    existing = json.loads(Path(out_path).read_text()) if Path(out_path).exists() else []
    full = full or not existing
    fresh, start, total = [], 0, None
    try:
        while True:
            rows, total = fetch_rows(fetcher, start)
            fresh += [normalize(r) for r in rows]
            start += PAGE_SIZE
            if not rows or start >= total or (not full and start >= PAGE_SIZE * NEWEST_PAGES):
                break
            sleep(gap)
    except Blocked as e:
        log(f"BLOCKED: {e}; keeping stored filings")
        return {"blocked": True, "fetched": len(fresh), "stored": len(existing)}
    except Exception as e:  # noqa: BLE001  endpoint/layout change must not break the build
        log(f"CCI fetch failed: {e}; keeping stored filings")
        return {"failed": str(e), "fetched": len(fresh), "stored": len(existing)}
    merged, changed = merge(existing, fresh)
    tmp = Path(out_path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=1, ensure_ascii=False))
    tmp.replace(out_path)
    log(f"CCI: fetched {len(fresh)} rows ({'full' if full else 'newest'}), "
        f"{changed} new/updated, {len(merged)} stored of {total} on the register")
    return {"fetched": len(fresh), "changed": changed, "stored": len(merged), "register_total": total}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    import news_source as ns
    f = ns.Fetcher(1.2)
    f.session.headers["X-Requested-With"] = "XMLHttpRequest"
    run(f, full="--full" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
