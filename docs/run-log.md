# Run log: week 2 run (D58)

Plan: docs/run-plan.md. Integration branch: run/week-2. Final PR: not yet.

## Resume point

- Status: in progress
- Task and step: task 7, step 6 (PR)
- Branch and PR: feat/gate-2, no PR yet
- Done so far: step 0 (30a3a2c); task 2 merged as 464444a (PR #10); task 3 merged as 66e388c (PR #11); D83 merged as c22a57d (PR #12); task 4 merged as 6af1d9a (PR #13); task 5 merged as 102b96c (PR #14); task 6 merged as 9b13074 (PR #15); task 7's plan
- Next: build task 7 in the commit order of its plan below, then the final step
- Next decision ID: D97
- Updated: 2026-11-18 13:07

## For Yazan (look at these first)

- **Answered by Yazan (D82): the last day stops at the current time.** Built in task 4. What I wrote: **The last day and now() (D78): I kept D41 and propose changing it.** D41 says the last simulated day runs in full, even past the current time. With night counts that would stop API writes for the counted items until tonight, so the simulator doesn't count on its last day. The older problem remains: after a reset during the day, the API can't count an item until tomorrow, because movements dated later today already exist (D51). The fix is to stop the last day at the current time, which changes D41, so it is yours to decide; the plan mentions it as brought forward from week 3.
- **Answered by Yazan (D83): low stock has its own level.** Built in PR #12 (D84), and the café's levels come in task 4. What I wrote: **Low stock is open every evening for items delivered daily (D76).** The reorder point is what triggers an order, and a daily-delivered pastry is below it every night, so its low_stock alert is raised and resolved every day. If the dashboard should only show items that are really running out, low stock needs its own lower level, separate from the reorder point.
- **Review: an end date in the future is now refused (D85).** D82 says a reset leaves nothing dated in the future; a future end date would do exactly that, so simulate() refuses it before writing anything. It is a small addition to D82, easy to drop.
- **Review: the closing check (D85).** At closing the simulated owner wastes, with an 80% chance each, the batches that expire that day. Without it, tonight's drafts counted stock that can't be sold tomorrow and ordered short. It makes expired alerts rare in the café (4 in 30 days), though every kind still shows.
- **The CLI's default is 30 days, not 90 (D89).** The spec's demo is 3 × 90 days, which takes about six minutes here, so every reset would; `--days 90` gives it. Changing the default is one line.
- **Gate 1's test is gone, folded into gate 2's (D88).** Every path it checked is still checked, across the three venues for 14 days each; the 30- and 90-day evidence lives in the reset output.
- **Review: setup takes the business row exclusively (D92).** Every item setup write waits for, and holds up, stock writes in the whole business, like an order action. Setup is rare, so I chose the simplest lock that keeps the rules right; a busy business that edits items all day would want per-item locks instead.
- **Gaps in item setup (D95):** no deleting items or units, no setting up suppliers or locations, and no history of setup changes (who changed a par level, and when).
- **An option I declined (PR #11's review, finding 4):** the draft-line query runs on every stock write, about 6% of a simulated month, although a line can only be due while the item's low_stock alert is due. The alerts evaluation could tell the caller when to skip it. I left it out because it ties two modules together for a saving that is small now; it is the first thing to try if writes need to be faster.
- **Why test times swung so much (D75).** Timings on this machine varied by tens of seconds between identical runs, and PR #9 reported 100 to 250 s. The cause is the planner's statistics for `stock_test`: autovacuum analyzes it between runs, when every test's rows have been rolled back, so the statistics say the tables are empty, and a 30-day simulation inside one transaction then runs about 40% slower (reproduced both ways). The simulator now runs ANALYZE after day 0 and weekly, which also helps a real `reset`. This touches the simulator in task 3, though its movements are unchanged.
- **Look at the lock order in D61.** PR #10's review found that a stock write and the alerts sweep could deadlock (reproduced on the dev database), because writes took the business-row lock after their batch locks. Writes now take it first, inside `_lock_defaults`, and a test pins the order. It was caught before merge, but it is the kind of bug worth a second pair of eyes.
- **Review first: D61, the alerts date.** Expiring, expired and low stock's "not expired" are judged against `businesses.alerts_swept_on`, which only the sweep moves, and the business row is the lock between a sweep and the writes. Tasks 3 and 4 build on it. The other option was to judge each write by its own date, which leaves the open alerts as a mix of dates that no single evaluation can check.
- **D62: GET /alerts writes.** It runs the sweep when the local date has moved on, so a GET can raise and resolve alerts. The alternatives were a CLI command (nothing runs it outside the simulator) or the API's start-up (stale from the next day).
- **Task 3, things the owner can't do in v1 (D67, D69):** add a line for an item that isn't low, create an order by hand, or cancel a draft; and an owner's quantity on a draft line is lost if the item recovers before the draft is sent. All four follow from keeping a draft as a pure projection of the par-level rule. If you want hand-made lines, the smallest change is to keep lines that have a qty_packs and add an `origin` column.
- **D63: the expiry window's formula** (min of expiry_warn_days and half the shelf life) is my number; see Assumptions.
- **Proposed hard rule (not added, the plan says to propose):** "`alerts` is a projection, written only by `app/domain/alerts.py`. Anything that changes what an alert depends on (a balance, a par level, an item's is_active or shelf_life_days, expiry_warn_days) re-evaluates the item in the same transaction." Task 6's item setup has to follow it either way (D59).
- **A timing to know about:** every stock write now runs two more statements (the business-row lock and the evaluation). Gate 1's test went from 43.7 s to 48–51 s on this machine, about 11% by profile; one run read 80.6 s with nothing else changed, so single timings here are noisy.

## Assumptions

- (Task 7) Week 3's scope in CLAUDE.md includes demo login with owner and staff roles, and CORS, because the dashboard needs them; the spec's milestone row names only the screens, and D43 says the header lasts "until demo login". It also names the read endpoints that waste-by-reason and count-variance screens will need.
- (Task 4) The owner places orders after closing, and deliveries come the morning of the delivery day, so a lead time of one day costs no day of usage (D76). The docs give lead times in days only.
- (Task 4) The par levels in days per kind of item, the over-portioning shares, the theft chances, the short-delivery chances and the count weekdays are my numbers, chosen so that a month of café data shows every kind of alert and a few shortfalls without looking broken (D76 to D78).
- (Task 5) Everything about the sweets shop and the cloud kitchen is my invention (D86): what they buy, from whom, pack sizes, prices (the kitchen's in AED), usage, opening hours, busy days, shelf lives, par and low-stock levels, shrinkage and supplier reliability. The docs only name the three verticals.
- (Task 4) The low-stock levels in days per kind of item (D85: dairy 1, pastry 0.33, produce 1.5, beans 2.5, dry goods and packaging 4) are my numbers: below what is normally left just before a delivery, so the alert means a late or short delivery or a busy day (D83).
- (Task 4) A short delivery brings 40 to 80% of each line; a late one keeps the expiry date of its planned day (D77).
- (Task 4) Demand isn't capped by what is in stock: usage is logged as demanded, as in week 1, and runs short when a delivery is late or short. Unlogged loss only shows at a count.
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

- Branch, PR, merge commit: feat/draft-orders, PR #11, merged into run/week-2 as 66e388c (CI 1m51s)
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
- Review: `/code-review` at high effort on PR #11, five findings.
  1. Fixed: pack counts could overflow. `suggested_packs` was `CEIL(...)::integer` into an int4 column, and `order_up_to` over a small `factor_to_base` can pass two billion, which would have failed every stock write for that item. Both pack columns are now bigint (the migration wasn't merged yet, so it was edited in place and both local databases re-migrated), and the owner's `qty_packs` is capped at a billion with a 400.
  2. Fixed: `receive()` read the order's lines before taking the alerts-date lock. If the order was still a draft then, it could gain a line and be sent before the lock, and that item would not be re-evaluated when the order closed. It now locks first; a test checks the order of the statements.
  3. Fixed: the simulator ran ANALYZE after the last day when its index was a multiple of 7, in a transaction nobody commits. It now skips the last day.
  4. Declined: the draft-line query runs on every write (see For Yazan).
  5. Fixed: tests for findings 1 and 2.
  - After the fixes: 546 passed in 148.6 s, gate 1's test 43.0 s, ruff clean, reset identical to the baseline, reconcile OK.
- Notes:
  - **Gate 1's time, looked into (the plan's 20% rule).** After the draft-line evaluation went into the writes, gate 1's test read 73.5 s and 60.5 s. A profile of the 30 days gave 64.6 s, with 8.95 s in the alerts statement (2.5 s in task 2) and 8.89 s in the draft-line statement. The cause was not the new code: `stock_test` had statistics from when it was empty, so the planner read whole tables. From a schema with no statistics the same code took 44.7 s (alerts 2.76 s, draft lines 2.83 s); after an ANALYZE of the empty database, 64.5 s; with the simulator analyzing as it goes (D75), 48.3 s from that same bad state. So the draft-line evaluation costs about 2.8 s of a 45 s run, about 6%, and task 2's alerts about the same.
  - I first tried passing the item pairs as VALUES rows instead of two arrays, so a cached plan would know how many pairs there are. With real statistics it made no difference, so it is not in the PR. The experiment is in a git stash on this machine ("values rows experiment"); the plan rules out dropping stashes, so it stays there and can be dropped by hand.
  - To reproduce the bad state I recreated `stock_test`'s schema (Alembic downgrade to base and upgrade, on the test database only) and ran ANALYZE on it while empty. The test database holds no data between tests, so nothing was lost.
  - The property machine failed once in the full run, in the new `send_draft` rule. It was the test's own expectation, not the domain: a line left alone keeps the owner's number from an earlier refused send, and the rule assumed it ordered the suggestion. Fixed in the test; it then passed three runs in a row. A mutation check (CEIL changed to FLOOR in the draft-line query) was caught.
  - A refused receipt against an order first raised its error after writing the receipt's movements (the API would have rolled them back, but a domain function refuses before it writes). The order is now closed as the last check, before anything is written, and a test checks nothing is left behind.

### D83: the low-stock level (Yazan's decision, before task 4)

- Branch, PR, merge commit: feat/low-stock-level, PR #12, merged into run/week-2 as c22a57d (CI 1m47s)
- Plan: one commit per step, each with its tests.
  1. Table: `par_levels.low_stock_level`, numeric(14,3), required, with checks 0 ≤ `low_stock_level` ≤ `reorder_point`. A new migration adds it, fills existing rows with their reorder_point (the one value that leaves their alerts as they were), then makes it NOT NULL. The builder's default is half the reorder point. Schema tests for the checks.
  2. Alerts: low_stock is due when usable on-hand is at or below `low_stock_level` (`app/domain/alerts.py`'s due query, which reconcile shares, so reconcile follows). Draft lines keep `reorder_point`. Worked examples: due at the level, not between the level and the reorder point (where a draft line is due), resolved above it.
  3. Property machine: each par level gets a low-stock level below its reorder point, and the Python evaluation of low stock uses it, so the machine covers the band where a line is due and no alert is.
  4. Docs: `docs/schema.md` (par_levels, rules 8 and 10, checks), the D84 details row.
- Decisions: D84 (details): the column's name, required, the backfill, and what an item without one does.
- Checks:
  - `uv run pytest`: 548 passed in 187.3 s (546 after task 3). Gate 1's test 56.0 s in the full run and 49.4 s alone (43.0 s after task 3's fixes); the property machine 52.1 s (43.8 s). This branch can't change gate 1's work: the simulator sets no par levels until task 4, so the low-stock condition never matches there, and the reset's time (48 s against 49.7 s) agrees. The machine is slower today, as on 15 Nov; the property machine also walks a new band now.
  - `uv run ruff check .` and `uv run ruff format --check .`: clean, 67 files.
  - Migration round trip on the dev database (upgrade, downgrade -1, upgrade): `6f33477b5349 -> 7a9b465f4743` each way; the 40 par-level rows task 4's work in progress had seeded took their reorder points.
  - `reset --end-date 2026-10-31`: 48 s; receipts 89 (5 late), receive 555, reversal 72, settle 16, usage 2790, waste 64, shortfalls 9, settle pairs 8, reversals 44: identical to the baseline.
  - reconcile: Dar Zaatar Café OK.
  - `git diff --check run/week-2...HEAD`: nothing.
- Review: `/code-review` at high effort on PR #12, two findings.
  1. Fixed: no worked example covered the band between the two levels through the stock writes, where a draft line is due and no alert is. `test_between_the_low_stock_level_and_the_reorder_point_milk_is_on_the_draft_but_not_low` in `tests/test_draft_lines.py` walks milk down through it and back.
  2. Declined: `ck_par_levels_reorder_point_non_negative` is now implied by the two new checks. Dropping it would be a migration that gains nothing; D84 says it stays.
  - After the fix: `tests/test_draft_lines.py` 27 passed, ruff clean.
- Notes:
  - A mutation check on the property machine (the alerts query back on reorder_point) was caught, after Hypothesis shrank it for five minutes.

### Task 4: the simulator on week 2 rules, for the café

- Branch, PR, merge commit: feat/sim-week2, PR #13, merged into run/week-2 as 6af1d9a (CI 1m40s)
- Plan:
  1. Venue data (`app/sim/cafe.py`): par levels per kind of item in days of usage, low-stock levels included (D83), each supplier's short-delivery chance and derived order weekdays, each kind's count weekday, over-portioning and theft, and the venue's order and count times. The standing-order fields (buffer, safety days) go. `seed.py` writes the par_levels rows, with their low_stock_level.
  2. Simulator (`app/sim/simulator.py`): a sweep at the start of each day; the morning check driven by open expired alerts; after closing, send the drafts of the suppliers whose order day it is and plan each delivery (late, short or cancelled); deliveries received against their order; unlogged loss per item, and a weekly count per kind after closing. Standing orders and their carry go. A run that ends today stops at the current time (D82): the last day's events dated after it are left out, counts included, so D78's last-day rule goes.
  3. Summary and CLI: the new counters, and the tallies of alerts and orders read at the end.
  4. Tests: seed (par levels), gate 1's test with the new path checks (D81), determinism extended to alerts, orders and counts, the existing failure test, and the default-end-date test now checking that nothing is dated after the current time (D82).
  5. Docs: CLAUDE.md (commands, milestone text stays for task 7), README, `docs/schema.md` rule 10's sweep sentence, D85.
  - Updated on 17 Nov after Yazan's D82 and D83, before the build was finished; the work in progress (adfc8bf) had kept D41's full last day and D78's rule.
- Decisions: D76 par levels as venue data and opening stock; D77 how the owner orders, late, short and cancelled deliveries; D78 shrinkage, weekly counts (its last-day rule replaced by Yazan's D82); D79 the daily sweep and expired waste from alerts; D80 reversals never cross a count; D81 the summary and gate 1's test. Yazan's D82 (the last day stops at the current time) and D83 (low-stock levels, here as venue data). Details row: D85.
- Checks:
  - `uv run pytest`: 552 passed in 160.1 s (549 after D83). Gate 1's test 45.2 s (43.0 s after task 3's fixes, 49.4 s alone on D83's branch this morning), the property machine 46.9 s, the determinism test 21.6 s (24.1 s; 7 days, now also comparing alerts, orders and counts), the D82 cutoff test 6.0 s.
  - `uv run ruff check .` and `uv run ruff format --check .`: clean, 67 files.
  - No migration in this task.
  - `reset --end-date 2026-10-31`: 46 s. Orders sent 83, received 78, cancelled 0 (now: 3 drafts, 5 sent); receipts 84 (2 late, 8 short); movements count_adjust 157, receive 417, reversal 76, settle 46, usage 2776, waste 42; waste by reason damaged 3, expired 16, spoiled 23; shortfalls 26; settle pairs 23; reversals 53; counts 25 (157 lines with a variance); alerts raised expired 4, expiring 144, low_stock 142, negative_stock 29; open at the end expiring 3, low_stock 5. Changed from the baseline by design (D52).
  - `reset` with no end date (D82): the 30 days up to 17 Nov, stopped at 12:43 UTC; nothing in movements, counts, alerts or orders is dated after the current time.
  - reconcile: Dar Zaatar Café OK after both.
  - `git diff --check run/week-2...HEAD`: nothing.
- Review: `/code-review` at high effort on PR #13, five findings, all fixed.
  1. `reset --end-date` in the future recreated the schema (erasing the demo data) and only then had simulate() refuse it. The CLI now checks the end date against today in the café's timezone before it asks or touches anything; a test makes recreate_schema and the prompt fail if reached.
  2. A count took the unlogged loss from expired batches first, though usage (and so over-portioning) skips them. Loss now comes from the batches usage draws, expired ones last. The 30-day summary is unchanged: no count in it met an expired batch with loss to take.
  3. The CLI caught every ValueError from simulate() and said nothing was committed; with the end date checked first, that catch is gone.
  4. `summary.expired_by_sku` was written and never read (since week 1); removed.
  5. Gate 1's path check hardcoded 2 Oct as the first day; it takes it from the summary.
  - After the fixes: 553 passed in 150.5 s, gate 1's test 46.4 s, ruff clean, reset summary identical, reconcile OK.
- Notes:
  - Low-stock alerts in the 30 days went from 440 (at the reorder point, the work in progress before D83) to 142. About 40 are day 0's: the first sweep finds every item empty before its opening delivery. Most of the rest open in the evening after a busy day, or after a late or short delivery, and close with the next morning's delivery.
  - Pastries run short most often (shortfalls on busy Fridays and Saturdays, and when a daily delivery is late). Demand isn't capped by stock, so a late delivery shows as a shortfall rather than lost sales (Assumptions).
  - The interrupted session left the simulator rewrite uncommitted; it was committed as work in progress (adfc8bf), and the D82 and D83 changes follow it as their own commits.

### Task 5: the sweets shop and the cloud kitchen

- Branch, PR, merge commit: feat/venues, PR #14, merged into run/week-2 as 102b96c (CI 2m0s)
- Plan:
  1. Move the venue dataclasses, weekday constants and the item constructor from `app/sim/cafe.py` to `app/sim/venue.py` (no behaviour change; the café's data stays in `cafe.py`).
  2. `app/sim/sweets.py` (SWEETS) and `app/sim/kitchen.py` (KITCHEN), and `app/sim/venues.py` with VENUES in run order. Tune each with a 30- and a 90-day run until it raises every kind of alert, sends and receives orders and has a few shortfalls without looking broken.
  3. CLI: reset and simulate run all three (D87); the default number of days is settled by timing 3 × 30 and 3 × 90 days.
  4. Tests: the seed test for each venue; gate 2's test (D88) replacing gate 1's; the CLI tests for three venues.
  5. Docs: CLAUDE.md commands, README (demo data, commands), D89.
- Decisions: D86 the two venues and what each exercises; D87 the CLI runs all three, seeds per venue; D88 gate 2's test, gate 1's folded into it. Details row: D89.
- Checks:
  - `uv run pytest`: 555 passed in 176.3 s (553 after task 4). Gate 2's test 56.4 s in the full run and 48.8 s alone, replacing gate 1's (46.4 s after task 4); the property machine 39.8 s; the determinism test 23.6 s.
  - `uv run ruff check .` and `uv run ruff format --check .`: clean, 71 files.
  - No migration in this task.
  - `reset --end-date 2026-10-31` (3 × 30 days): 115 s. The café's summary is identical to task 4's (its seed is unchanged, D87). Sukkar & Semsem Sweets: orders sent 70, received 68; receipts 73 (2 late, 0 short); shortfalls 1; counts 20 (107 lines with a variance); alerts raised expiring 45, low_stock 58, negative_stock 5. Sahtein Cloud Kitchen: orders sent 83, received 80; receipts 86 (3 late, 3 short); shortfalls 5; counts 25 (126 lines with a variance); alerts raised expired 1, expiring 165, low_stock 96, negative_stock 12.
  - `reset --days 90 --end-date 2026-10-31` (3 × 90 days): 349 s. Café: orders sent 254, received 248, cancelled 1; receipts 254 (14 late, 6 short); shortfalls 116; counts 77 (466 lines with a variance); alerts raised expired 14, expiring 449, low_stock 378, negative_stock 81. Sweets: orders sent 216, received 214; receipts 219 (12 late, 11 short); shortfalls 32; counts 64 (340); alerts raised expiring 130, low_stock 180, negative_stock 33. Kitchen: orders sent 263, received 259, cancelled 1; receipts 265 (18 late, 14 short); shortfalls 55; counts 77 (368); alerts raised expired 5, expiring 529, low_stock 268, negative_stock 63.
  - reconcile: all three OK after both resets.
  - `git diff --check run/week-2...HEAD`: nothing.
- Review: `/code-review` at high effort on PR #14, three findings, all fixed.
  1. Without --end-date, each venue ends today in its own timezone, so for an hour each evening the Dubai kitchen ends a day after the Amman venues, unlike D87's "same end date". That follows D82, so the behaviour stays; D87 and the CLI now say so.
  2. Determinism was tested only for the café. The test now runs the café and the kitchen (another timezone), 5 days each, in the same total time (24.4 s against 23.6 s).
  3. `ItemSpec.pack_price`'s comment said JOD; it now says the venue's currency.
- Notes:
  - The sweets shop raises no expired alerts, even in 90 days: the closing check wastes its cheese and dough on their expiry day, and its other goods keep for weeks or have no expiry. The café and the kitchen cover the expired path, which gate 2 checks across the three venues.
  - The CLI's default stays at 30 days (D89): 3 × 90 days take about six minutes here.

### Task 6: the item setup API

- Branch, PR, merge commit: feat/item-setup, PR #15, merged into run/week-2 as 9b13074 (CI 1m49s)
- Plan:
  1. `app/domain/items.py`: create_item, update_item, add_unit, rename_unit, set_supplier_price, remove_supplier_price, set_par_level, remove_par_level. Each locks the business row (orders.py's lock, made public as lock_business), checks, writes, and re-evaluates through evaluate_rules where D92 says. Worked examples in `tests/test_items.py`, each ending with reconcile's alert and draft-line checks.
  2. Property machine: a rule that changes a par level, so the alerts and draft-lines invariants cover re-evaluation by setup.
  3. API: `app/api/items.py` and its schemas (D90). Tests in `tests/test_api_items.py`.
  4. Docs: `docs/schema.md` (rule 10's list of what re-evaluates; the setup rules), README endpoint list, D95.
- Decisions: D90 endpoints; D91 what can't change once used; D92 rules follow setup, with the business lock; D93 deactivation; D94 supplier prices, preferred, no client_id. Details row: D95.
- Checks:
  - `uv run pytest`: 593 passed in 153.4 s (555 after task 5). Gate 2's test 45.9 s, the property machine 35.1 s (with its new par-level rule), the determinism tests 14.7 s and 9.9 s.
  - `uv run ruff check .` and `uv run ruff format --check .`: clean, 75 files.
  - No migration in this task.
  - `reset --end-date 2026-10-31` (3 × 30 days): 101 s. The movements of all three venues are identical to task 5's (café: receive 417, usage 2776, count_adjust 157, …; sweets: usage 1755; kitchen: usage 1895), as the plan requires for task 6.
  - reconcile: all three OK.
  - `git diff --check run/week-2...HEAD`: nothing.
- Review: `/code-review` at high effort on PR #15, five findings, four fixed, one declined.
  1. Fixed: numbers too large for their column (a factor, a price or a par level of 10^11 or more, a shelf life past int4) reached Postgres and came back as a 500. The shared checks now refuse a quantity or an amount above 99,999,999,999.999 with a 400, which covers the stock writes too, where the same 500 was possible; shelf lives must be 1 to 36,500 days.
  2. Fixed: update_item compared is_active before checking its type, so a direct caller's 1 or 0 could be ignored; the type is checked first.
  3. Fixed: changing the base unit before the first movement kept the item's units and par levels, whose numbers were in the old unit (a 1000 ml bottle would become 1000 g). The base unit now changes only on an item with no units and no par levels; otherwise the owner creates a new item (units can't be resized or removed, D91). Tracking still changes until the first movement.
  4. Fixed: the lock-order test covered five of the eight setup writes; it now covers all eight.
  5. Declined: each write endpoint looks the item up for its 404 and the domain looks it up again. The 404/400 split needs the first lookup, and it is one indexed query per setup write.
  - After the fixes: 599 passed in 157.1 s, ruff clean.
- Notes:
  - A mutation check on the property machine (set_par_level without its re-evaluation) was caught.
  - The domain worked examples check the rules with check_alerts and check_draft_lines rather than the full reconcile, because they build stock with test_alerts.py's batch_with (balances without movements), as the alerts examples do. The API tests and the property machine cover real movements.

### Task 7: gate 2 housekeeping

- Branch, PR, merge commit: feat/gate-2
- Plan:
  1. `docs/decisions.md`: Last updated; D81's status (its gate 1 checks moved to gate 2's test by D88); the Open section (still none); D96, the gate.
  2. CLAUDE.md: the Current milestone for week 3 (dashboard), from the spec's milestone row and user stories U5–U11, with gate 3 ("Demo script runs in the browser"); commands and hard rules checked against the code. The temporary run section stays until the final step.
  3. `docs/schema.md`: Last updated, and a check that the rules intro and every section match the code.
  4. README: the Status section describes week 2.
  5. Gate 2 evidence, in the PR and here: `reset --days 90 --end-date 2026-10-31` and reconcile, alerts and draft orders per venue, the test count and suite time, and CI's time.
- Decisions: none new beyond D96, the gate and its evidence.
- Checks:
  - `uv run pytest`: 599 passed in 168.0 s (366 at the baseline, 593 before task 6's review fixes). Gate 2's test 48.4 s, the property machine 37.7 s, the determinism tests 19.2 s (café) and about 10 s (kitchen). CI took 1m49s on PR #15.
  - `uv run ruff check .` and `uv run ruff format --check .`: clean, 75 files.
  - No migration in this task.
  - `git diff --check run/week-2...HEAD`: nothing.
- Gate 2 evidence: `reset --days 90 --end-date 2026-10-31`, 307 s, identical to the run in task 5.

  | Venue | Orders sent / received / cancelled | Receipts (late, short) | Movements | Count lines with a variance | Alerts raised (expired / expiring / low_stock / negative_stock) | Open at the end | Drafts waiting |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | Dar Zaatar Café | 254 / 248 / 1 | 254 (14, 6) | 10,534 | 466 | 922 (14 / 449 / 378 / 81) | 9 | 3 |
  | Sukkar & Semsem Sweets | 216 / 214 / 0 | 219 (12, 11) | 6,646 | 340 | 343 (0 / 130 / 180 / 33) | 2 | 3 |
  | Sahtein Cloud Kitchen | 263 / 259 / 1 | 265 (18, 14) | 7,272 | 368 | 865 (5 / 529 / 268 / 63) | 12 | 4 |

  reconcile: Dar Zaatar Café OK, Sahtein Cloud Kitchen OK, Sukkar & Semsem Sweets OK.
- Review: findings and what was done
- Notes:
  - The 3 × 90 run uses the CLI's seeds (20261110, +1, +2) and leaves the dev database with the full demo.

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
- 2026-11-15 about 16:40: the session stopped after task 3's last commit (059ffc5), before its PR.
- 2026-11-16 19:44: resumed in the same session after Yazan's "Try again" (model unchanged). Postgres had exited (code 0) about 20 hours earlier; `docker compose up -d db` brought it back with the dev data intact.
- 2026-11-16 19:47: PR #11 opened for task 3.
- 2026-11-16 19:53: PR #11 reviewed (five findings, four fixed, one declined) and the fixes pushed.
- 2026-11-16 19:55: PR #11 green (CI 1m51s) and squash-merged into run/week-2 as 66e388c.
- 2026-11-16 20:00: task 4 started on feat/sim-week2.
- 2026-11-17 12:07: resumed in a new session on Claude Opus 5.5 (Fable 5.2 no longer available), after Yazan answered two For Yazan items with D82 and D83. The working tree held task 4's simulator rewrite, uncommitted; it lints clean but its tests aren't updated, so it was committed as work in progress on feat/sim-week2 (adfc8bf). D83 is built first, on feat/low-stock-level.
- 2026-11-17 12:20: PR #12 opened for D83, reviewed (two findings, one fixed, one declined).
- 2026-11-17 12:27: PR #12 green (CI 1m47s) and squash-merged into run/week-2 as c22a57d.
- 2026-11-17 12:28: run/week-2 merged into feat/sim-week2 (conflicts in decisions.md and the run log only); task 4 resumes.
- 2026-11-17 about 12:50: the session stopped after PR #13's review fixes were pushed, with CI green.
- 2026-11-18 12:11: resumed in the same session after Yazan's "Try again" (model unchanged: Claude Opus 5.5). Postgres had exited (code 0) about 12 hours earlier; `docker compose up -d db` brought it back.
- 2026-11-18 12:13: PR #13 squash-merged into run/week-2 as 6af1d9a (CI 1m40s).
- 2026-11-18 12:14: task 5 started on feat/venues.
- 2026-11-18 12:38: PR #14 green (CI 2m0s) and squash-merged into run/week-2 as 102b96c.
- 2026-11-18 12:41: task 6 started on feat/item-setup.
- 2026-11-18 13:06: PR #15 green (CI 1m49s) and squash-merged into run/week-2 as 9b13074.
- 2026-11-18 13:07: task 7 started on feat/gate-2.
