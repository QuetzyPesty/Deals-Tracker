# STATE: multi-source deals (Legal Era + LiveLaw) with strict cross-source dedup

Branch: `claude/data-sources-brainstorm-test-0e41d2`. CCI is deliberately deferred.
Baseline before this work (main @116ed41): 612 deals, 4002 people, 141 firms.

| id | goal | status | verify | commit |
|----|------|--------|--------|--------|
| T0 | repo CLAUDE.md + this ledger | in-progress | `test -f CLAUDE.md && test -f STATE.md` | |
| T1 | move source-agnostic extraction out of scrape_barandbench.py into scraper/extraction.py, B&B output unchanged | todo | `python3 tests/test_firm_extraction.py` | |
| T2 | dedup.py: layered strict cross-source dedup + merge, unit tests incl. hard negatives | todo | `python3 tests/test_dedup.py` | |
| T3 | wire dedup into build_directory (report, provenance table), baseline deal count unchanged | todo | `python3 build_directory.py` + counts == baseline | |
| T4 | Legal Era scraper + fixtures + tests | todo | `python3 tests/test_sources.py` | |
| T5 | LiveLaw scraper + fixtures + tests | todo | `python3 tests/test_sources.py` | |
| T6 | workflow update, live run, full rebuild, Playwright smoke | todo | `npm test` | |
