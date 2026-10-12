# CLAUDE.md

Orientation file for LLMs working in this repo. Verified against the repo contents; keep it current when structure or commands change.

## What this is
"Brag-Brag": a searchable directory of Indian corporate-law deals, the law firms that advised on them and the lawyers named in each deal. News articles are scraped, firms/people/roles/deal value are extracted, and everything is rendered into a single static `index.html`.

## Stack
Python 3.11 (requests, beautifulsoup4, lxml, optional spaCy), SQLite, a static HTML template, Playwright for browser tests. GitHub Actions runs the scrape on a schedule.

## Where to look
- `scraper/scrape_barandbench.py` — Bar & Bench Dealstreet scraper and the extraction logic (firms, "Name (Role)" people, deal value). Writes `barandbench_deals.json`, tracks `scraper/seen_urls.json`.
- `scraper/ner_supplement.py` — optional spaCy pass that only fills gaps; silently off when spaCy is missing.
- `build_directory.py` — merges source JSON files, canonicalises firms (`FIRM_ALIASES`, `canonical_firm`), builds `legal_directory.db` and `legal_directory.json`.
- `render_html.py` + `legal_directory_template.html` — inject the JSON into the template, producing `index.html`.
- `structured_deals.json` — legacy deals (mbox / Google Alerts export), not produced by any script here.
- `firm_history_verification.json` — hand-maintained firm-move verification used by the build.
- `tests/test_firm_extraction.py` — offline regression tests on gzipped real articles in `tests/fixtures/`.
- `tests/smoke_brag_brag.spec.js` + `playwright.config.js` — browser tests against `index.html`.
- `.github/workflows/scrape.yml` — scheduled scrape (every 2 days) that scrapes, rebuilds, renders and commits.

## Commands
- Scrape: `python3 scraper/scrape_barandbench.py` (`--reparse-all` re-fetches every stored URL)
- Build: `python3 build_directory.py && python3 render_html.py`
- Python tests: `python3 tests/test_firm_extraction.py`
- Browser tests: `npm test` (serves the repo root on :4174; needs `index.html` built first)

## Gotchas
- `legal_directory.db`, `legal_directory.json` and `index.html` are generated and committed; regenerate rather than hand-edit. The build is deterministic, so a diff after a rebuild means the inputs changed.
- The CI bot commits `Scrape: <date>` to main every two days, so branches fall behind quickly.
- Scraped sites are third-party publishers: keep the per-request delay, and identify honestly in the User-Agent (some sites return 403 for UAs containing "bot" or "crawler").
- Firm attribution is deliberately conservative: a wrong firm is treated as worse than a missing one.
- CI does not run the Python or Playwright tests.

## Other docs
`STATE.md` (task ledger for the multi-source work), `PROGRESS.md` (phase checkpoints), if present.
