"""
Publication dates for records that were scraped before dates were recorded.

New articles get `published_at` from the scraper that fetches them. Two sets of
older records have none:

  * barandbench_deals.json (script-owned): dates are written back into the file.
  * structured_deals.json (a legacy export no script here produces): dates go
    to a sidecar, deal_dates.json {url: iso | null}, and are applied at build
    time, so the export itself is never rewritten.

The backfill is a one-off, resumable and polite: one request at a time with a
delay, an honest User-Agent, a hard stop on 403/429, progress saved every few
fetches so an interrupted run loses nothing, and a URL that is gone (404/410)
is recorded as null so it is never asked for again.

Run:  python3 scraper/dates.py [--limit N]
"""
import json
import sys
from pathlib import Path

SCRAPER_DIR = Path(__file__).parent
BASE = SCRAPER_DIR.parent
sys.path.insert(0, str(SCRAPER_DIR))
sys.path.insert(0, str(BASE))

SIDECAR_PATH = BASE / "deal_dates.json"
SAVE_EVERY = 20


def load_sidecar(path=SIDECAR_PATH):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else {}


def save_sidecar(data, path=SIDECAR_PATH):
    path = Path(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(sorted(data.items())), indent=1, ensure_ascii=False))
    tmp.replace(path)


def apply_sidecar(records, sidecar):
    """Fill `published_at` on records that lack one. Returns how many it set.
    A record's own date always wins over the sidecar."""
    n = 0
    for r in records:
        if r.get("published_at"):
            continue
        got = sidecar.get(r.get("url"))
        if got:
            r["published_at"] = got
            n += 1
    return n


def date_only(value):
    """YYYY-MM-DD from any ISO-ish timestamp, or None."""
    return str(value)[:10] if value and len(str(value)) >= 10 else None


def _is_gone(exc):
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) in (404, 410)


def backfill(fetcher, bb_path, legacy_path, sidecar_path=SIDECAR_PATH, limit=None, log=print):
    """Returns a stats dict. `fetcher.get(url)` must return page HTML and raise
    news_source.Blocked on 403/429."""
    from extraction import published_at_from_html
    from news_source import Blocked

    bb = json.loads(Path(bb_path).read_text()) if Path(bb_path).exists() else []
    legacy = json.loads(Path(legacy_path).read_text()) if Path(legacy_path).exists() else []
    sidecar = load_sidecar(sidecar_path)

    # script-owned B&B records first (they matter most), then the legacy URLs
    todo = [("bb", r) for r in bb if not r.get("published_at")]
    seen = {r["url"] for r in bb}
    todo += [("legacy", r) for r in legacy
             if r.get("url") and r["url"] not in seen and r["url"] not in sidecar
             and not r.get("published_at")]
    if limit:
        todo = todo[:limit]

    stats = {"todo": len(todo), "dated": 0, "gone": 0, "no_date_on_page": 0, "errors": 0, "blocked": False}
    dirty = 0

    def flush():
        save_sidecar(sidecar, sidecar_path)
        Path(bb_path).write_text(json.dumps(bb, indent=2, ensure_ascii=False))

    try:
        for i, (kind, r) in enumerate(todo, 1):
            url = r["url"]
            try:
                when = published_at_from_html(fetcher.get(url))
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                if _is_gone(e):
                    stats["gone"] += 1
                    if kind == "legacy":
                        sidecar[url] = None
                    dirty += 1
                else:
                    stats["errors"] += 1
                    log(f"  error {url}: {e}")
                continue
            if when:
                stats["dated"] += 1
                if kind == "bb":
                    r["published_at"] = when
                else:
                    sidecar[url] = when
            else:
                stats["no_date_on_page"] += 1
                if kind == "legacy":
                    sidecar[url] = None
            dirty += 1
            if dirty >= SAVE_EVERY:
                flush()
                dirty = 0
                log(f"  {i}/{len(todo)} (dated {stats['dated']}, gone {stats['gone']})")
    except Blocked as e:
        stats["blocked"] = True
        log(f"BLOCKED: {e}; progress saved, rerun later to continue")
    finally:
        flush()
    return stats


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    import news_source as ns
    fetcher = ns.Fetcher(1.5)
    stats = backfill(fetcher, BASE / "barandbench_deals.json", BASE / "structured_deals.json", limit=limit)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
