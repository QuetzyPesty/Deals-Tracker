# STATE: multi-source deals (Legal Era + LiveLaw) with strict cross-source dedup

Branch: `claude/data-sources-brainstorm-test-0e41d2`. CCI is deliberately deferred.
Baseline before this work (main @116ed41): 612 deals, 4002 people, 141 firms.

| id | goal | status | verify | commit |
|----|------|--------|--------|--------|
| T0 | repo CLAUDE.md + this ledger | done | `test -f CLAUDE.md && test -f STATE.md` | e4cb493 |
| T1 | move source-agnostic extraction out of scrape_barandbench.py into scraper/extraction.py, B&B output unchanged | done | `python3 tests/test_firm_extraction.py` | 1ec1396 |
| T2 | dedup.py: layered strict cross-source dedup + merge, unit tests incl. hard negatives | done | `python3 tests/test_dedup.py` | d9dae2e |
| T3 | wire dedup into build_directory (report, provenance table, UI label) | done | `python3 build_directory.py` (deterministic) | 54e1913 |
| T4 | Legal Era scraper + fixtures + tests | done | `python3 tests/test_sources.py` | 8ac1d24, aa15577 |
| T5 | LiveLaw scraper + fixtures + tests | done | `python3 tests/test_sources.py` | 8ac1d24, aa15577 |
| T6 | workflow gate, live backfill, rebuild, Playwright (fixed a pre-existing flawed sort test, +2 UI tests) | done | `npm test` -> 15 passed | see git log |

## Post-merge fixes
- F1 (2026-10-15): firm-attribution candidates could run across a sentence ("Dua Associates is a full-service ... The firm"), pick up trailing verbs ("Khaitan & Co acted"), or be the bankers a firm advised ("ICICI Securities Limited and Jefferies ..."). Fixed in `extraction.clean_firm_candidate`; 5 records re-parsed; firms 153 -> 148. Verify: `python3 tests/test_sources.py` (the "every firm ... is a plausible name" check scans all committed source files).

## Deferred
- CCI combination orders as an enrichment layer (JSON endpoint verified: GET /combination/orders-section31 with full DataTables params).
- Possible missed twin: Laser Power & Infra IPO ("$1,200 crore" typo in one B&B headline vs ₹742 crore); kept apart by the value veto.
- LiveLaw headlines using verbs outside the loose-verb list ("Concludes", "Completes") yield no firm and are dropped when the body has no credits.
