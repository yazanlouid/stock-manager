# Run log: week 2 run (D58)

Plan: docs/run-plan.md. Integration branch: run/week-2. Final PR: not yet.

## Resume point

- Status: in progress
- Task and step: task 2, step 6 (PR), then 7 (review) and 8 (CI and merge)
- Branch and PR: feat/alerts, PR to be opened
- Done so far: step 0 (30a3a2c on run/week-2); task 2 built and checked on feat/alerts (all seven plan steps, D59–D66)
- Next: open task 2's PR into run/week-2, review it with fresh eyes, fix findings, merge when CI is green, then start task 3 on `feat/draft-orders`
- Next decision ID: D67
- Updated: 2026-11-14 18:31

## For Yazan (look at these first)

- **Review first: D61, the alerts date.** Expiring, expired and low stock's "not expired" are judged against `businesses.alerts_swept_on`, which only the sweep moves, and the business row is the lock between a sweep and the writes. Tasks 3 and 4 build on it. The other option was to judge each write by its own date, which leaves the open alerts as a mix of dates that no single evaluation can check.
- **D62: GET /alerts writes.** It runs the sweep when the local date has moved on, so a GET can raise and resolve alerts. The alternatives were a CLI command (nothing runs it outside the simulator) or the API's start-up (stale from the next day).
- **D63: the expiry window's formula** (min of expiry_warn_days and half the shelf life) is my number; see Assumptions.
- **Proposed hard rule (not added, the plan says to propose):** "`alerts` is a projection, written only by `app/domain/alerts.py`. Anything that changes what an alert depends on (a balance, a par level, an item's is_active or shelf_life_days, expiry_warn_days) re-evaluates the item in the same transaction." Task 6's item setup has to follow it either way (D59).
- **A timing to know about:** every stock write now runs two more statements (the business-row lock and the evaluation). Gate 1's test went from 43.7 s to 48–51 s on this machine, about 11% by profile; one run read 80.6 s with nothing else changed, so single timings here are noisy.

## Assumptions

- Half the shelf life is the cap on an item's expiry window (D63). The docs give only expiry_warn_days per business. Used in `app/domain/alerts.py`.
- A negative default batch counts against usable on-hand (D64): the shortfall is stock the ledger says was used, so the best estimate of what is left is the sum. Used by low stock, and by draft lines in task 3.
- An alert the sweep raises takes the time the sweep runs as of (the plan's wording), not the midnight the batch actually crossed its date. On GET /alerts that is the time of the read (D62).
- An inactive item still raises expiring, expired and negative-stock alerts while it holds stock, because that stock still has to be used, wasted or explained; only low stock looks at is_active (D64).

## Baseline (main at 0e75a8c)

- Tests, suite time, gate 1's test time: 366 passed in 103.8 s; gate 1's test 43.7 s, the property machine 20.5 s, the determinism test 17.0 s (`uv run pytest -q --durations=5`)
- reset --end-date 2026-10-31: 33.5 s. Receipts 89 (5 late); movements receive 555, reversal 72, settle 16, usage 2790, waste 64; waste by reason damaged 5, expired 30, spoiled 29; shortfalls 9; settle pairs 8; reversals 44. Matches the plan's numbers
- reconcile: Dar Zaatar Café: OK

## Tasks

### Task 2: par levels and alerts

- Branch, PR, merge commit: feat/alerts
- Plan: one commit per step, each with its tests.
  1. Tables: `par_levels`, `alerts` and `businesses.alerts_swept_on` (models in `app/models/rules.py`, one migration, builders, schema tests in `tests/test_rules_schema.py`).
  2. `app/domain/alerts.py`: `evaluate_alerts(session, business_id, pairs, at)` (lock the business row FOR SHARE, then one statement: what is due for the touched items, resolve what isn't, raise what's missing) and `sweep_alerts(session, business_id, as_of)`. Worked examples in `tests/test_alerts.py`, calling evaluate directly on rows from the builders.
  3. Call `evaluate_alerts` at the end of receive, use, waste, reverse (`stock.py`) and `count_stock` (`counts.py`), with the default-batch keys each one locked and its own time. Worked examples through the domain functions.
  4. reconcile: `alert_missing` and `alert_not_due`, from the same "what is due" query. Tests in `tests/test_reconcile.py`.
  5. Property machine: par levels for milk and flour, a sweep rule, and an invariant that the open alerts equal a Python evaluation of the snapshots.
  6. GET /alerts (`app/api/alerts.py`, schemas), with the sweep when the date has moved on. Tests in `tests/test_api_alerts.py`.
  7. Docs: `docs/schema.md` (tables, rule 10, constraints, indexes), CLAUDE.md hard rule 6's text, README's endpoint list.
- Decisions: D59 alerts are a projection kept by every write, proved by reconcile; D60 granularity, the open key with location, lifecycle; D61 the alerts date and its lock; D62 what runs the sweep; D63 the expiry window; D64 usable on-hand and low stock; D65 GET /alerts.
- Details row: D66.
- Checks:
  - `uv run pytest`: 455 passed in 124.5 s (366 at the baseline). Gate 1's test 51.3 s in the full run and 48.4 s alone, against 43.7 s at the baseline; the property machine 30.2 s (20.5 s).
  - `uv run ruff check .` and `uv run ruff format --check .`: clean, 58 files.
  - Migration round trip on the dev database (upgrade, downgrade -1, upgrade): `232cc07be728 -> af5e5eafee1d` each way, and reconcile OK afterwards.
  - `reset --end-date 2026-10-31`: 38.0 s; receipts 89 (5 late), receive 555, reversal 72, settle 16, usage 2790, waste 64, shortfalls 9, settle pairs 8, reversals 44: identical to the baseline. It left 17 negative_stock alerts, all resolved, and no alerts date, because the simulator doesn't sweep or set par levels until task 4.
  - reconcile: Dar Zaatar Café OK (now including the alerts check).
  - `git diff --check run/week-2...HEAD`: nothing.
- Review: findings and what was done
- Notes:
  - Gate 1's time: the first measurement after wiring the evaluation into the writes read 80.6 s. A rerun read 48.4 s, and a profile of the same 30 days put the evaluation at 4.3 s of 37.6 s (lock 1.8 s, statement 2.5 s). The 80.6 s was the machine, not the code.
  - A mutation check on the property machine (expiring's `<=` changed to `<` in the domain query) survived one run and was caught by the next: the machine reaches the window's exact boundary in most runs, not all. The parametrized worked examples in `tests/test_alerts.py` pin the boundary. In one counted run of 1,213 steps, low_stock was due 1,399 times, expired 188, negative_stock 145 and expiring 120.
  - The work-in-progress commit cb1f6bf (from the interruption) is followed by f584479, which finishes the tables step.

### Task 3: draft orders

- Branch, PR, merge commit:
- Plan:
- Decisions:
- Checks: test count, suite time, gate 1's test time; migration round trip; reset summary; reconcile; CI time
- Review: findings and what was done
- Notes:

### Task 4: the simulator on week 2 rules, for the café

- Branch, PR, merge commit:
- Plan:
- Decisions:
- Checks: test count, suite time, gate 1's test time; migration round trip; reset summary; reconcile; CI time
- Review: findings and what was done
- Notes:

### Task 5: the sweets shop and the cloud kitchen

- Branch, PR, merge commit:
- Plan:
- Decisions:
- Checks: test count, suite time, gate 1's test time; migration round trip; reset summary; reconcile; CI time
- Review: findings and what was done
- Notes:

### Task 6: the item setup API

- Branch, PR, merge commit:
- Plan:
- Decisions:
- Checks: test count, suite time, gate 1's test time; migration round trip; reset summary; reconcile; CI time
- Review: findings and what was done
- Notes:

### Task 7: gate 2 housekeeping

- Branch, PR, merge commit:
- Plan:
- Decisions:
- Checks: test count, suite time, gate 1's test time; migration round trip; reset summary; reconcile; CI time
- Review: findings and what was done
- Notes:

## Timeline

- 2026-11-14 17:28: step 0 started (model: Claude Fable 5.2). Times in this log are this machine's clock, which reads UTC.
- 2026-11-14 17:35: step 0 committed and pushed to run/week-2 (30a3a2c); the baseline matches the plan's numbers.
- 2026-11-14 17:36: task 2 started on feat/alerts; plan and D59–D65 committed (0ce9104).
- 2026-11-14 17:37: stopped by a usage limit during task 2's plan step 1; work in progress committed and pushed (cb1f6bf), resume point written. (These three entries first carried estimated times that ran ahead of the clock; corrected here from the commits' times. From here on every time is read from the clock.)
- 2026-11-14 18:04: resumed in the same session (model unchanged: Claude Fable 5.2). The working tree was clean and feat/alerts matched origin.
