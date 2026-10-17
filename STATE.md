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
- 5 deals stay undated (see D1 below): shown as "unknown", never guessed.

- F2 (2026-10-16): the first manual workflow run after #3 showed `Khaitan & Co regularly` on main: F1's enumerated verb list missed an adverb. Replaced by a structural rule (a firm name is capitalised words plus connectors; the first other lowercase word ends it), with exemptions for "Pte./Ltd./Pvt." abbreviations and for names starting with a known firm. Re-parsed the 3 stale records (`DSK Legal teams`, `Khaitan & Co regularly`, a BRLM bank); firms 154 -> 151. Verify: `python3 tests/test_sources.py`.
- F3 (2026-10-16): the three Bar & Bench name problems recorded as open after F2 are fixed, plus two more of the same kind found by scanning every firm for "X and Y". (1) `Luthra and Luthra` was split on "and" by both headline parsers: they now share `split_firm_names`, which rejoins a pair when the joined text is a known firm in `FIRM_ALIASES`. (2) `Kanga & Company, Advocates and Solicitors` / `Kanga and Co` and the Luthra long forms are aliased. (3) A body sentence "A and B advised ..." read as one firm (Dentons/Hogan Lovells, Sidley Austin/FBT Gibbons, Fenwick/Allen & Gledhill, Trilegal/A&O Shearman, AZB/Duane Morris): `split_fused_firms` and `_attributed_names` split it only when a piece is vouched for by a page topic tag or the known-firm list and no piece is a bare law word ("Saraf and Partners" stays whole). 8 B&B records re-parsed; firms 151 -> 145, people 4224 -> 4216 (Luthra's lawyers merged under one firm). Verify: `python3 tests/test_sources.py`.

- D1 (2026-10-17): the three deferred items. **Laser Power twin**: `$1,200 crore` is a typo (crore is a rupee unit); `parse_money` now returns no figure for a non-rupee symbol with an Indian unit, which had been read as a US$12 billion IPO. With no value to veto on, the pair merges by `same-core-headline`. **Draft-then-final IPOs**: the same dig found two more (Rentomojo "proposed ₹150 crore+" vs ₹1,255 crore; INDO-MIM ₹1,000 crore+ vs ₹3,812 crore) that the value veto kept apart. New dedup rule `ipo-draft-then-final`: both IPOs, exactly one headline says proposed/DRHP, shared counsel, one headline's identity words nested in the other's, draft not after final and within 400 days. **Legacy deal sizes**: 208 of the 376 legacy headlines state a size the export never carried; the build now reads it from the headline (deals with a size 205 -> 413). **LiveLaw headlines**: `lead_keyword_firms` takes the firm from the outlet's own keyword list when the headline opens with it and it looks like a law firm (no verb list); `--retry-dropped` re-fetches URLs seen but not kept (AMS Legal now kept; litigation and the "Expands Its Mumbai Presence" firm news stay out, the office-expansion filter now allows a word in between). **Undated**: 10 -> 5. Two were Legally India forum threads (`/convos/`), now excluded from deals; three were the draft IPO stories above, which now merge into their dated final. The other five (`jsa-advises-veh-green-energy`, `jsa-assists-serentica`, `knm-partners-jsa ... aipl`, `fox-mandal ... jana-nayagan`, `khaitan ... tvs-group`) are deleted from Bar & Bench: 404 or a redirect to the listing, confirmed from two clients; the Wayback Machine refuses Bar & Bench. They stay unknown. Serentica was re-published under a new slug on 2026-07-23 (not in our data), but that is the update's date, not the original's. Verify: `python3 tests/test_dedup.py && python3 tests/test_sources.py`.

## Deferred
- LiveLaw feature stories with no firm in the headline and no standard attribution sentence (Solicis Lex / Kalpataru, a flat purchase) are still dropped.
- The Bar & Bench scraper reads only the first listing page per run: fine at ~4 articles a day, but a gap of more than ~20 articles between runs would be missed silently.
