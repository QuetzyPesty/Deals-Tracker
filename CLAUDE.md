# Brag-Brag (Indian M&A legal directory)

Directory of lawyers and firms on Indian deals, built from scraped legal-news
articles (Bar & Bench mainly) plus an older mbox-derived dataset. Published as
a single static page on GitHub Pages (QuetzyPesty/Deals-Tracker).

## Stack
Python 3 (requests, bs4/lxml, optional spaCy), SQLite + JSON output, one
vanilla-JS HTML template, Playwright for browser tests. No JS build step.

## Where to look
- `scraper/scrape_barandbench.py` — main scraper and article parsing; other
  sources in `scraper/scrape_livelaw.py`, `scrape_legalera.py`, `scrape_cci.py`.
  `seen_*.json` are the already-scraped checkpoints.
- `build_directory.py` — merges `structured_deals.json` + `barandbench_deals.json`
  into `legal_directory.db` / `legal_directory.json`; firm aliases, role buckets.
- `legal_directory_template.html` — the whole UI (state, filters, views).
- `render_html.py` — inlines the JSON into the template -> `index.html` (generated).
- `.github/workflows/scrape.yml` — scheduled scrape, rebuild, commit.
- `tests/` — `*.spec.js` Playwright; `test_*.py` offline Python tests with saved
  article fixtures in `tests/fixtures/`.
- `local_tools/` — gitignored local NER experiments; never commit.
- `STATE.md` — task ledger.

## Commands
- Rebuild: `python3 build_directory.py && python3 render_html.py`
- Browser tests: `npx playwright test` (serves the repo on port 4174)
- Python tests: `python3 tests/test_firm_extraction.py` (likewise the other `tests/test_*.py`)
- Re-parse every scraped article: `python3 scraper/scrape_barandbench.py --reparse-all` (slow, ~1 s/article)

## Gotchas
- Edit the template, never `index.html`; regenerate after any template or data change.
- Precision over recall: a missed name is acceptable, a wrong attribution is not.
- Deal size is displayed in its reported currency; the INR value exists only for ranking/filtering.
- Use a heredoc for commit messages (backticks get shell-expanded).
