# Run log: week 2 run (D58)

Plan: docs/run-plan.md. Integration branch: run/week-2. Final PR: not yet.

## Resume point

- Status: in progress
- Task and step: task 3, step 6 (PR), then 7 (review) and 8 (CI and merge)
- Branch and PR: feat/draft-orders, PR to be opened
- Done so far: step 0 (30a3a2c); task 2 merged as 464444a (PR #10); task 3 built and checked on feat/draft-orders (D67–D75)
- Next: open task 3's PR into run/week-2, review it with fresh eyes, fix findings, merge when CI is green, then start task 4 on `feat/sim-week2`
- Next decision ID: D76
- Updated: 2026-11-15 14:26

## For Yazan (look at these first)

- **Why test times swung so much (D75).** Timings on this machine varied by tens of seconds between identical runs, and PR #9 reported 100 to 250 s. The cause is the planner's statistics for `stock_test`: autovacuum analyzes it between runs, when every test's rows have been rolled back, so the statistics say the tables are empty, and a 30-day simulation inside one transaction then runs about 40% slower (reproduced both ways). The simulator now runs ANALYZE after day 0 and weekly, which also helps a real `reset`. This touches the simulator in task 3, though its movements are unchanged.
- **Look at the lock order in D61.** PR #10's review found that a stock write and the alerts sweep could deadlock (reproduced on the dev database), because writes took the business-row lock after their batch locks. Writes now take it first, inside `_lock_defaults`, and a test pins the order. It was caught before merge, but it is the kind of bug worth a second pair of eyes.
- **Review first: D61, the alerts date.** Expiring, expired and low stock's "not expired" are judged against `businesses.alerts_swept_on`, which only the sweep moves, and the business row is the lock between a sweep and the writes. Tasks 3 and 4 build on it. The other option was to judge each write by its own date, which leaves the open alerts as a mix of dates that no single evaluation can check.
- **D62: GET /alerts writes.** It runs the sweep when the local date has moved on, so a GET can raise and resolve alerts. The alternatives were a CLI command (nothing runs it outside the simulator) or the API's start-up (stale from the next day).
- **Task 3, things the owner can't do in v1 (D67, D69):** add a line for an item that isn't low, create an order by hand, or cancel a draft; and an owner's quantity on a draft line is lost if the item recovers before the draft is sent. All four follow from keeping a draft as a pure projection of the par-level rule. If you want hand-made lines, the smallest change is to keep lines that have a qty_packs and add an `origin` column.
- **D63: the expiry window's formula** (min of expiry_warn_days and half the shelf life) is my number; see Assumptions.
- **Proposed hard rule (not added, the plan says to propose):** "`alerts` is a projection, written only by `app/domain/alerts.py`. Anything that changes what an alert depends on (a balance, a par level, an item's is_active or shelf_life_days, expiry_warn_days) re-evaluates the item in the same transaction." Task 6's item setup has to follow it either way (D59).
- **A timing to know about:** every stock write now runs two more statements (the business-row lock and the evaluation). Gate 1's test went from 43.7 s to 48–51 s on this machine, about 11% by profile; one run read 80.6 s with nothing else changed, so single timings here are noisy.

## Assumptions

- (Task 3) A supplier delivers an order in one go: one receipt closes it, and whatever was short goes on the next draft (D71). The docs don't say whether deliveries can be split.
- (Task 3) Orders are placed in whole packs, the owner's edits included (D70). Receipts still accept part packs, as before.
- (Task 3) A draft's quantity on order doesn't count as "already on order" (D68): only sent orders do. The plan's note reads "open drafts plus sent orders"; with one line per item on the one open draft, counting the draft would subtract the line from itself.
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

- Branch, PR, merge commit: feat/alerts, PR #10, merged into run/week-2 as 464444a (CI 1m27s)
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
- Review: `/code-review` at high effort on PR #10, seven findings.
  1. Fixed: a stock write and the sweep could deadlock. The write held its batch locks and then asked for the business row; the sweep held the business row and then needed a batch row, because an alert's foreign key locks the batch it points at. Reproduced with two connections on the dev database (DeadlockDetected). Writes now lock the business row first, inside `_lock_defaults`, before any batch; with that order the same two-connection script shows the sweep waiting and then finishing.
  2. Fixed: two reads of GET /alerts either side of local midnight could turn the earlier one into a 400, because its sweep would move the date back. `sweep_alerts_if_due` now does nothing in that case; `sweep_alerts` still refuses when called directly.
  3. Fixed: no test covered the lock order. `test_every_write_locks_the_alerts_date_before_any_batch` records each write's statements and checks the order.
  4. Fixed: `docs/schema.md` said the simulator sweeps each simulated day; it doesn't until task 4. The sentence is back in task 4's hands.
  5. Fixed: `ALERT_TYPES` was unused; removed.
  6. Fixed: a pointless last assertion in the API waste test, with two imports only it used; removed.
  7. Declined: the business-row lock statement runs once per write even when the transaction already holds the lock (the simulator's day-long transactions), about 1.8 s of a 37.6 s profile. Skipping it would need per-transaction state in the domain functions, which costs more clarity than the 5% is worth.
  - After the fixes: 457 passed in 143.5 s, ruff clean, reset identical to the baseline, reconcile OK.
- Notes:
  - Gate 1's time: the first measurement after wiring the evaluation into the writes read 80.6 s. A rerun read 48.4 s, and a profile of the same 30 days put the evaluation at 4.3 s of 37.6 s (lock 1.8 s, statement 2.5 s). The 80.6 s was the machine, not the code.
  - A mutation check on the property machine (expiring's `<=` changed to `<` in the domain query) survived one run and was caught by the next: the machine reaches the window's exact boundary in most runs, not all. The parametrized worked examples in `tests/test_alerts.py` pin the boundary. In one counted run of 1,213 steps, low_stock was due 1,399 times, expired 188, negative_stock 145 and expiring 120.
  - The work-in-progress commit cb1f6bf (from the interruption) is followed by f584479, which finishes the tables step.

### Task 3: draft orders

- Branch, PR, merge commit: feat/draft-orders
- Plan: one commit per step, each with its tests.
  1. Tables: `purchase_orders`, `purchase_order_lines` and `receipts.po_id` (models, one migration, builders, schema tests).
  2. `app/domain/draft_lines.py`: `evaluate_draft_lines(session, business_id, pairs)`: one query finds how the touched items' draft lines differ from what is due, then plain statements add, update or remove lines (creating the draft with INSERT … ON CONFLICT). `check_draft_lines` is the same query for reconcile. It shares the "batches with stock" CTEs with alerts, so usable on-hand has one definition (D64).
  3. `app/domain/rules.py`: `evaluate_rules` (alerts, then draft lines), which the five stock writes call in place of `evaluate_alerts`, and `sweep` / `sweep_if_due`, which move here from `alerts.py` and re-evaluate draft lines too.
  4. `app/domain/orders.py`: `send_order`, `cancel_order`, `set_line_quantity`, each locking the business row exclusively (D72). `receive()` takes `po_id` (D71).
  5. reconcile: draft-line problems. Property machine: a preferred supplier for flour, rules to send, cancel and receive against an order, and the draft-lines invariant in Python.
  6. API: GET /orders, GET /orders/{id}, PUT a line, POST send and cancel, `po_id` on POST /receipts.
  7. Docs: `docs/schema.md`, CLAUDE.md, README.
- Decisions: D67 draft lines are a projection; D68 when a line is due and for how much; D69 statuses, no origin; D70 whole packs, qty_packs and suggested_packs; D71 receiving against an order; D72 order actions lock the business row; D73 endpoints; D75 the simulator analyzes as it fills the database (found while measuring this task).
- Details row: D74.
- Checks:
  - `uv run pytest`: 543 passed in 171.7 s (457 after task 2). Gate 1's test 51.4 s, the property machine 43.8 s, the determinism test 24.1 s. The machine ran about a third slower on 15 Nov than on 14 Nov (the simulation without any rules evaluation took about 39 s in a profile, against 33 s), so compare these with each other, not with task 2's.
  - `uv run ruff check .` and `uv run ruff format --check .`: clean, 66 files.
  - Migration round trip on the dev database (upgrade, downgrade -1, upgrade): `af5e5eafee1d -> 6f33477b5349` each way, and reconcile OK afterwards.
  - `reset --end-date 2026-10-31`: 49.7 s; receipts 89 (5 late), receive 555, reversal 72, settle 16, usage 2790, waste 64, shortfalls 9, settle pairs 8, reversals 44: identical to the baseline. No draft orders yet: the simulator sets no par levels until task 4.
  - reconcile: Dar Zaatar Café OK (now including the draft-line and order-line checks).
  - `git diff --check run/week-2...HEAD`: nothing.
- Review: findings and what was done
- Notes:
  - **Gate 1's time, looked into (the plan's 20% rule).** After the draft-line evaluation went into the writes, gate 1's test read 73.5 s and 60.5 s. A profile of the 30 days gave 64.6 s, with 8.95 s in the alerts statement (2.5 s in task 2) and 8.89 s in the draft-line statement. The cause was not the new code: `stock_test` had statistics from when it was empty, so the planner read whole tables. From a schema with no statistics the same code took 44.7 s (alerts 2.76 s, draft lines 2.83 s); after an ANALYZE of the empty database, 64.5 s; with the simulator analyzing as it goes (D75), 48.3 s from that same bad state. So the draft-line evaluation costs about 2.8 s of a 45 s run, about 6%, and task 2's alerts about the same.
  - I first tried passing the item pairs as VALUES rows instead of two arrays, so a cached plan would know how many pairs there are. With real statistics it made no difference, so it is not in the PR. The experiment is in a git stash on this machine ("values rows experiment"); the plan rules out dropping stashes, so it stays there and can be dropped by hand.
  - To reproduce the bad state I recreated `stock_test`'s schema (Alembic downgrade to base and upgrade, on the test database only) and ran ANALYZE on it while empty. The test database holds no data between tests, so nothing was lost.
  - The property machine failed once in the full run, in the new `send_draft` rule. It was the test's own expectation, not the domain: a line left alone keeps the owner's number from an earlier refused send, and the rule assumed it ordered the suggestion. Fixed in the test; it then passed three runs in a row. A mutation check (CEIL changed to FLOOR in the draft-line query) was caught.
  - A refused receipt against an order first raised its error after writing the receipt's movements (the API would have rolled them back, but a domain function refuses before it writes). The order is now closed as the last check, before anything is written, and a test checks nothing is left behind.

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
- 2026-11-14 18:33: PR #10 opened for task 2.
- 2026-11-14 18:40: PR #10 reviewed (seven findings, six fixed, one declined) and the fixes pushed.
- 2026-11-14 18:44: PR #10 green (CI 1m27s) and squash-merged into run/week-2 as 464444a.
- 2026-11-14 18:47: task 3 started on feat/draft-orders.
- 2026-11-14 about 19:05: the session stopped mid-task 3 (uncommitted: order actions and po_id on receive); nothing was lost.
- 2026-11-15 13:38: resumed in the same session after Yazan's "continue the run" (model unchanged: Claude Fable 5.2). The working tree held the uncommitted order actions, which were finished, tested and committed (c9c3699).
- 2026-11-15 14:26: task 3 built and checked.
