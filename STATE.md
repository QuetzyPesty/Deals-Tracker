# STATE: multi-source deals (Legal Era + LiveLaw) with strict cross-source dedup

Branch: `claude/data-sources-brainstorm-test-0e41d2`. CCI is deliberately deferred.
Baseline before this work (main @116ed41): 612 deals, 4002 people, 141 firms.

| id | goal | status | verify | commit |
|----|------|--------|--------|--------|
| T0 | repo CLAUDE.md + this ledger | done | `test -f CLAUDE.md && test -f STATE.md` | e4cb493 |
| T1 | move source-agnostic extraction out of scrape_barandbench.py into scraper/extraction.py, B&B output unchanged | done | `python3 tests/test_firm_extraction.py` | 1ec1396 |
| T2 | dedup.py: layered strict cross-source dedup + merge, unit tests incl. hard negatives | done | `python3 tests/test_dedup.py` | d9dae2e |
| T3 | wire dedup into build_directory (report, provenance table); 612 -> 607 deals = the 6 real duplicates | done | `python3 build_directory.py` (deterministic) | 54e1913 |
| T4 | Legal Era scraper + fixtures + tests | done | `python3 tests/test_sources.py` | 8ac1d24 |
| T5 | LiveLaw scraper + fixtures + tests | done | `python3 tests/test_sources.py` | 8ac1d24 |
| T6 | workflow update, live run, full rebuild, Playwright smoke | todo | `npm test` | |
