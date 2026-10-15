# CLAUDE.md

Orientation file for LLMs working in this repo. Verified against the repo contents; keep it current when structure or commands change.

## What this is
"Brag-Brag": a searchable directory of Indian corporate-law deals, the law firms that advised on them and the lawyers named in each deal. News articles from several outlets (Bar & Bench, Legal Era, LiveLaw, plus a legacy export) are scraped, firms/people/roles/deal value are extracted, the same deal reported by several outlets is merged into one, and everything is rendered into a single static `index.html`.

## Stack
Python 3.11 (requests, beautifulsoup4, lxml, optional spaCy), SQLite, a static HTML template, Playwright for browser tests. GitHub Actions runs the scrape on a schedule.

## Where to look
- `scraper/extraction.py` — the shared extraction engine (firm attribution, "Name (Role)" / "Role Name" / plural-list credits, deal value, headline parsers). Every source uses it.
- `scraper/scrape_barandbench.py` — Bar & Bench Dealstreet scraper. Writes `barandbench_deals.json`, tracks `scraper/seen_urls.json`.
- `scraper/news_source.py` — shared machinery for NewsArticle-JSON-LD outlets: polite fetch, 403/429 hard stop, discovery, article parsing, run loop.
- `scraper/scrape_legalera.py`, `scraper/scrape_livelaw.py` — thin source configs on top of `news_source.py`. Write `legalera_deals.json` / `livelaw_deals.json`, track `scraper/seen_legalera.json` / `seen_livelaw.json`.
- `scraper/dates.py` — one-off, resumable backfill of publication dates for records scraped before dates were recorded (B&B written in place; legacy URLs go to the `deal_dates.json` sidecar, applied at build time).
- `scraper/scrape_cci.py` — CCI merger-control filings register -> `cci_filings.json` (incremental; `--full` walks everything). Enrichment source, not a deal source.
- `scraper/cci_match.py` — attaches a filing to a deal by NAME (type gate, distinct-party evidence, date window, ambiguity rule). Annotation only; every decision is written to `cci_report.json`.
- `scraper/dedup.py` — cross-source de-duplication, run at build time over every source. Rules, vetoes and the reasoning are in its docstring.
- `scraper/ner_supplement.py` — optional spaCy pass that only fills gaps; silently off when spaCy is missing.
- `build_directory.py` — merges all source JSON files (`SOURCE_FILES`), runs dedup, canonicalises firms (`FIRM_ALIASES`, `canonical_firm`), builds `legal_directory.db`, `legal_directory.json` and `dedup_report.json`.
- `render_html.py` + `legal_directory_template.html` — inject the JSON into the template, producing `index.html`.
- `structured_deals.json` — legacy deals (mbox / Google Alerts export), not produced by any script here.
- `firm_history_verification.json` — hand-maintained firm-move verification used by the build.
- `dedup_report.json`, `cci_report.json` — generated audit trails: every dedup merge with its rule (plus near-misses kept apart), and every CCI match / ambiguity / skip reason.
- `tests/` — `test_firm_extraction.py` (Bar & Bench fixtures), `test_dedup.py`, `test_sources.py` (Legal Era / LiveLaw fixtures in `tests/fixtures/sources/`), `test_dates.py`, `test_cci.py` (fixtures in `tests/fixtures/cci/`), `smoke_brag_brag.spec.js` (Playwright against `index.html`).
- `.github/workflows/scrape.yml` — scheduled scrape (every 2 days): scrape each source, run the offline tests, rebuild, render, commit.

## Commands
- Scrape: `python3 scraper/scrape_barandbench.py`, `python3 scraper/scrape_legalera.py [--pages N] [--max-new N] [--reparse-all]`, `python3 scraper/scrape_livelaw.py` (same flags), `python3 scraper/scrape_cci.py [--full]`, `python3 scraper/dates.py [--limit N]`
- Build: `python3 build_directory.py && python3 render_html.py`
- Python tests: `python3 tests/test_firm_extraction.py && python3 tests/test_dedup.py && python3 tests/test_sources.py && python3 tests/test_dates.py && python3 tests/test_cci.py`
- Browser tests: `npm ci && npm test` (serves the repo root on :4174; needs `index.html` built first)

## Gotchas
- `legal_directory.db`, `legal_directory.json`, `index.html` and `dedup_report.json`, `cci_report.json` are generated and committed; regenerate rather than hand-edit. The build is deterministic, so a diff after a rebuild means the inputs changed.
- Source files are kept RAW and un-merged on disk; de-duplication happens only in `build_directory.py`. A scraper's per-run URL check is just a cheap pre-filter.
- A false merge silently deletes a real deal, so `dedup.py` prefers keeping two records apart: value, date, round, deal-type and entity vetoes beat similarity. Add a test in `tests/test_dedup.py` (positive AND negative) with any rule change, and read `dedup_report.json` near-misses.
- "Date reported" = `published_at`, the earliest publication date among the outlets that carried the deal (dedup keeps the minimum). It is when the market was told, not when the deal closed. Undated deals are unknown, not old: they never pass the recency filter and sort last.
- A CCI match is a name join with no shared id, so `cci_match.py` prefers a miss to a wrong match; any loosening needs a positive AND a negative test in `tests/test_cci.py`.
- The CI bot commits `Scrape: <date>` to main every two days, so branches fall behind quickly.
- Scraped sites are third-party publishers: keep the per-request delay and the honest User-Agent (Legal Era returns 403 for UAs containing "bot" or "crawler"; never impersonate a browser). Use path-suffix pagination, never `?page=` (disallowed by robots.txt).
- Firm attribution is deliberately conservative: a wrong firm is treated as worse than a missing one.
- The CI workflow runs the five Python test files before committing; the Playwright suite is not run in CI.

## Other docs
`STATE.md` (task ledger for the multi-source work).
