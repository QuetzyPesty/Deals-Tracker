"""Tests for publication dates: extraction from HTML, the resumable backfill and
the legacy sidecar.   Run:  python3 tests/test_dates.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scraper"))

import dates  # noqa: E402
import news_source as ns  # noqa: E402
from extraction import published_at_from_html  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


print("extraction")
ld = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-09-09T16:49:23Z"}</script>'
meta = '<meta property="article:published_time" content="2026-08-01T10:00:00+05:30">'
check("JSON-LD datePublished", published_at_from_html(f"<html>{ld}</html>") == "2026-09-09T16:49:23Z")
check("article:published_time meta", published_at_from_html(f"<head>{meta}</head>") == "2026-08-01T10:00:00+05:30")
check("JSON-LD wins over meta", published_at_from_html(meta + ld) == "2026-09-09T16:49:23Z")
check("no date -> None", published_at_from_html("<html></html>") is None and published_at_from_html(None) is None)
check("date_only", dates.date_only("2026-09-09T16:49:23Z") == "2026-09-09" and dates.date_only(None) is None)

print("backfill")
RealFetcher = ns.Fetcher


class Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text, self.encoding = code, text, "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            e = requests.HTTPError(f"HTTP {self.status_code}")
            e.response = self
            raise e


class Session:
    def __init__(self, pages, blocked=()):
        self.pages, self.blocked, self.headers, self.calls = pages, set(blocked), {}, []

    def get(self, url, timeout=30):
        self.calls.append(url)
        if url in self.blocked:
            return Resp(403)
        return Resp(200, self.pages[url]) if url in self.pages else Resp(404)


def fetcher(pages, blocked=()):
    f = RealFetcher(0)
    f.session = Session(pages, blocked)
    return f


def page(day):
    return f'<script type="application/ld+json">{{"datePublished":"{day}T08:00:00Z"}}</script>'


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    bb = [{"url": "https://b/1", "headline": "a"}, {"url": "https://b/2", "headline": "b", "published_at": "2026-01-01T00:00:00Z"}]
    legacy = [{"url": "https://l/1", "headline": "c"}, {"url": "https://l/gone", "headline": "d"},
              {"url": "https://l/nodate", "headline": "e"}, {"url": "https://b/1", "headline": "dup of bb"}]
    (tmp / "bb.json").write_text(json.dumps(bb))
    (tmp / "legacy.json").write_text(json.dumps(legacy))
    side = tmp / "side.json"
    f = fetcher({"https://b/1": page("2026-02-02"), "https://l/1": page("2025-05-05"), "https://l/nodate": "<html></html>"})
    st = dates.backfill(f, tmp / "bb.json", tmp / "legacy.json", side, log=lambda *_: None)
    got_bb = json.loads((tmp / "bb.json").read_text())
    got_side = json.loads(side.read_text())
    check("B&B record is dated in place", got_bb[0]["published_at"] == "2026-02-02T08:00:00Z", got_bb[0])
    check("a record's existing date is untouched and not refetched",
          got_bb[1]["published_at"] == "2026-01-01T00:00:00Z" and "https://b/2" not in f.session.calls)
    check("legacy URL goes to the sidecar, not into the legacy file",
          got_side["https://l/1"] == "2025-05-05T08:00:00Z"
          and "published_at" not in json.loads((tmp / "legacy.json").read_text())[0])
    check("a gone URL (404) is recorded as null so it is never asked again",
          "https://l/gone" in got_side and got_side["https://l/gone"] is None)
    check("a URL already in B&B is not fetched a second time via legacy", f.session.calls.count("https://b/1") == 1)
    check("stats add up", st["dated"] == 2 and st["gone"] == 1 and st["no_date_on_page"] == 1, st)

    f2 = fetcher({})
    st2 = dates.backfill(f2, tmp / "bb.json", tmp / "legacy.json", side, log=lambda *_: None)
    check("second run is a no-op (resumable)", f2.session.calls == [] and st2["todo"] == 0, (f2.session.calls, st2))

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    legacy = [{"url": f"https://l/{i}", "headline": str(i)} for i in range(6)]
    (tmp / "bb.json").write_text("[]")
    (tmp / "legacy.json").write_text(json.dumps(legacy))
    pages = {f"https://l/{i}": page("2025-03-0" + str(i + 1)) for i in range(6)}
    f = fetcher(pages, blocked=["https://l/3"])
    side = tmp / "side.json"
    st = dates.backfill(f, tmp / "bb.json", tmp / "legacy.json", side, log=lambda *_: None)
    saved = json.loads(side.read_text())
    check("a 403 stops the run and keeps progress made so far",
          st["blocked"] and len(saved) == 3 and f.session.calls[-1] == "https://l/3", (st, saved))

print("sidecar")
recs = [{"url": "u1"}, {"url": "u2", "published_at": "own"}, {"url": "u3"}]
n = dates.apply_sidecar(recs, {"u1": "2025-01-01T00:00:00Z", "u2": "sidecar", "u3": None})
check("sidecar fills gaps, never overrides, ignores null", n == 1 and recs[0]["published_at"] == "2025-01-01T00:00:00Z"
      and recs[1]["published_at"] == "own" and "published_at" not in recs[2])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all date tests pass")
