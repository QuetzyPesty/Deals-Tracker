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

## Phase 2: recency + CCI enrichment (started 2026-10-15)
Decisions (user, 2026-10-15): exact dates via refetch; CCI matches high-confidence only; recency UI = sort + filter + date chips.
Dedup uses earliest published_at as the deal's "date reported".

| id | goal | status | verify | commit |
|----|------|--------|--------|--------|
| R1 | every record carries published_at: B&B scraper records it; backfill script + `deal_dates.json` sidecar for existing/legacy URLs; applied in build | done (664/674 deals dated) | `python3 tests/test_dates.py` | f405daa |
| R2 | re-audit dedup now that more records are dated: 14-day window split 5 real duplicates; measured gaps (max 72d) -> 90/120-day windows, merges back to 33 | done | `python3 tests/test_dedup.py` | see git log |
| R3 | build + UI: deals[].published_at, sort "Most recent deal", "Date reported" filter, date chips, latest-deal chip | done | `npm test` -> 21 passed | see git log |
| C1 | CCI scraper -> `cci_filings.json` (1,466 filings; incremental, polite, keeps file on any failure) | done | `python3 tests/test_cci.py` | see git log |
| C2 | CCI matcher: type gate, party-name coverage, ambiguity + shared-filing rules, window -130/+60d from hand-labelled review; 9 matches, 0 known wrong | done | `python3 tests/test_cci.py` | see git log |
| C3 | build integration (`deal_cci` table, JSON, UI badge), workflow step | done | `python3 build_directory.py` + `npm test` | see git log |

## Phase 2 notes
- CCI window (-130/+60 days between filing notification and the date the deal was reported) comes from a hand review of 39 in-band candidates; small sample, so it is deliberately not wider than the evidence. Re-check when `cci_report.json` has more matches.
- CCI coverage is low by design (9 of 674 deals): most deals are not merger-control notifiable, and a name join is only trusted on a full party name.
- Roughly 10 deals stay undated (6 legacy URLs are gone, 6 pages carry no date): shown as "unknown", never guessed.

## Deferred
- Possible missed twin: Laser Power & Infra IPO ("$1,200 crore" typo in one B&B headline vs ₹742 crore); kept apart by the value veto.
- LiveLaw headlines using verbs outside the loose-verb list ("Concludes", "Completes") yield no firm and are dropped when the body has no credits.
