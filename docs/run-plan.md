# Run plan: week 2, tasks 2–7 (autonomous run)

Written 14 Nov 2026 in the project chat, for Claude Code. Decision D58. Integration branch: `run/week-2`. Don't edit this file during the run, except to fix a command that doesn't work (say so in the run log).

## 1. What this run is

You build the rest of week 2 on your own: tasks 2–7 of the week 2 plan (D49), in order, ending at gate 2, then week 1 hardening if you still can. Nobody approves plans or answers questions during the run. Yazan reviews everything at the end, in one pull request into main, and he must be able to explain every line, so the record you leave matters as much as the code:

- Every decision and every assumption is written down (section 5).
- Every PR explains what isn't obvious, at the level of detail of PR #9's description.
- The code stays plain: clear names, small functions, comments that say why (above all in ledger, locking, FEFO, alert and order code), no cleverness, no dead code.

Gate 2 (CLAUDE.md, D53): the simulator raises alerts and draft orders on its own, every venue still reconciles, and CI is green. The CI test runs each of the three venues for 14 days; the full 3 × 90 days runs through reset, and its output goes in the gate PR.

**Precedence:** CLAUDE.md's hard rules and decisions D1–D58, then this plan, then CLAUDE.md's Workflow section. Read CLAUDE.md, `docs/spec.md`, `docs/schema.md` and `docs/decisions.md` before step 0, as CLAUDE.md asks.

**Keep going.** Work in one go: don't end your turn to report progress or to ask whether to continue; the run log is the report. The run ends only when the final PR is open and green (section 9), or when a usage limit or an error you can't get past stops you (section 8).

**State at the start:** main is at 0e75a8c (PR #9, week 2 task 1, stock counts). 366 tests; on Windows the suite takes 100–250 s and gate 1's test about 52 s. The dev database holds 30 café days ending 2026-10-31 on migration 232cc07be728. `reset --end-date 2026-10-31` gives 89 receipts, 2,790 usage movements, 9 shortfalls, 8 settle pairs and 44 reversals, and reconciles.

## 2. What changes in CLAUDE.md's workflow for this run

| CLAUDE.md says | During the run |
| --- | --- |
| Plan first, and wait for approval | Write the plan in the run log's task entry and the PR description, then build. Don't wait. |
| If a task conflicts with the docs, stop and ask | Don't stop. Keep the existing rule or decision, take the route that respects it (or skip that part), and put the conflict and a proposed fix under For Yazan. |
| Don't edit `docs/` unless asked | You're asked to: append to `docs/decisions.md`, keep `docs/schema.md` in step with the code, and keep `docs/run-log.md`. Don't edit `docs/spec.md`; note any problem with it under For Yazan. |
| One small task per branch, about 15 minutes to review | One branch and one PR per task. They'll be bigger, so make each commit one logical step with its tests, and the review can go commit by commit. |
| If a task needs a new decision, say so; Yazan records it | Make it and record it yourself (section 5). |
| Yazan reviews every change | Still true, at main: task PRs merge into `run/week-2` after green CI and your own fresh review, and main changes only through the final PR, which Yazan reviews and merges (D18, D58). |
| Ask before adding a dependency or a service | Don't add any. If one is truly needed, log it as a decision and put it under For Yazan. |

Everything else in CLAUDE.md stands: the stack, environment notes, commands, hard rules 1–10, the testing rules and the commit style.

What counts as changing a decision: anything already built, and anything a logged decision says, is fixed. The tables `docs/schema.md` plans but nobody has built yet (par_levels, purchase_orders, purchase_order_lines, alerts, po_id on receipts) are designs, not decisions: change them where a run decision says so, and update the doc in the same PR. Text that describes the state of the code, like hard rule 6's note that the negative-stock alert arrives in week 2, is updated when the code changes, without changing the rule. Don't add new hard rules; propose them under For Yazan.

## 3. Never

- Push to main, merge anything into main, merge or mark ready the final PR, or change repository settings, rulesets or branch protection.
- Force-push or rewrite history on `run/week-2`. Rebasing your own unmerged feature branch is fine.
- Change, weaken or reinterpret an existing decision (D1–D58) or hard rule. If one looks wrong, keep it and propose the change under For Yazan.
- Edit a migration that is already merged (on main or `run/week-2`). Add a new one.
- Start week 3: no `web/`, `mobile/`, CORS or login work.
- Run two test suites at once, or tests from two checkouts: every checkout shares the `stock_test` database.
- Run `docker compose down -v`, or anything else that deletes the Postgres volume. reset on the dev database is fine.
- Commit `.env` or any secret.
- Run what auto mode blocks by default: force pushes, `git reset --hard`, `git checkout -- .`, `git restore .`, `git clean -fd`, `git stash drop` or `git stash clear`, `git commit --amend` on a pushed commit, deleting remote branches. If the classifier blocks a command, don't retry it or rephrase it to get round the block: three blocks in a row, or twenty in a session, pause auto mode until Yazan answers, which stalls the run. Find a way that doesn't need it, or log it under For Yazan and move on.

Yazan has pre-approved, in his settings and his first message, the two things auto mode would otherwise block: squash-merging your own task PRs into `run/week-2` once CI is green, and resetting the dev database (reset, and Alembic downgrades on it). Nothing else.

## 4. Step 0: set up (once)

From the repo root, on main with a clean working tree (this file may be the only untracked file; if anything else is uncommitted, leave it alone and note it in the run log):

1. `git switch main`, `git pull`, `git switch -c run/week-2`.
2. Make sure this file is at `docs/run-plan.md`.
3. In `docs/decisions.md`:
   - Append the rows D49–D58 from Appendix A after D48, verbatim.
   - Bring four statuses up to date, as the project log has them: D31 and D33 "Active (extended by D50)", D40 "Superseded by D52 (from week 2 task 4)", D46 "Active (amended by D51)".
   - Replace the Open section's backdated-writes item with "None." (D50 and D51 closed it).
   - Header: "Last updated: 14 Nov 2026 (week 2 run, D58)." Replace "this file is updated from it at each gate, so decisions made since the last gate may not be here yet." with "this file is updated from it at each gate. During the week 2 run (D58), decisions are logged here first, from D59, and copied to the working copy at review."
4. Create `docs/run-log.md` from Appendix B.
5. In CLAUDE.md, add this section just before "## Workflow":

   ```
   ## Autonomous run (temporary, D58)

   Week 2's tasks 2–7 run autonomously under `docs/run-plan.md`, which overrides the Workflow section below and the "stop and ask" and "don't edit `docs/`" lines under Project, until the run's final pull request into main is open. In a new session or after compaction, read `docs/run-plan.md` and `docs/run-log.md` before doing anything else. When compacting, keep the current task, branch and step, open PR numbers, the next decision ID and the For Yazan items. Claude Code squash-merges its own task pull requests into `run/week-2` once CI is green; it never pushes to main, merges into main, or marks the final pull request ready.
   ```

6. Record the baseline in the run log: run the checks from section 6, step 5 (test count, suite time, gate 1's test time, the reset summary and reconcile).
7. Commit ("Start the week 2 run: decisions D49–D58, run plan and run log") straight to `run/week-2`, then `git push -u origin run/week-2`.

## 5. Decisions, assumptions and the For Yazan list

You own every open decision listed in section 7 and any new one you meet. A decision is anything that sets the schema, an API contract, a domain rule or how the simulator behaves, or that chooses between options Yazan would want to weigh. Internal names and refactors that change no behaviour aren't decisions.

**How to choose**, in this order:

1. It keeps every hard rule and existing decision.
2. It serves the user stories (U6, U7, U10, U12) and gate 2.
3. It is the simplest thing that works for v1 and is easy to change later. Between two close options, take the one that's easier to reverse, and if later tasks will build on it, put it under For Yazan as "review first".
4. It follows existing patterns: D23's schema conventions, D36's locking, D42's transactions, D47–D48's API style, D41's simulator style.
5. It keeps writes and CI fast.

**How to log** each decision, as you make it:

- A row in `docs/decisions.md`: the next ID (D59, D60, …), the date, one line in the log's style (what was decided and the main reason, plain text) and the status "Active (run, task N, to review)".
- In the PR description's Decisions section: the options you weighed and why you chose this one, a few lines each.
- In the run log's task entry: the IDs, a few words each.
- End each task with a "Week 2 task N details" row, like D57: the plan-level choices worth knowing (keys and constraint names, the order of checks, what is computed where, gaps you accepted).

**Assumptions:** Yazan's standing rule is never to assume. You can't ask him during the run, so whenever you assume something the docs don't say (how a business behaves, a number, the meaning of an unclear line), add it to the run log's Assumptions list, with where it's used.

**For Yazan:** anything he should look at first goes in the run log's For Yazan list: hard-to-reverse choices, proposals to change an existing decision or doc, conflicts you worked around, review findings you declined, anything skipped.

If a decision can't be made without breaking a rule, skip that part, put it under For Yazan, and carry on with whatever doesn't depend on it.

## 6. The loop, for each task

1. **Orient.** Re-read `docs/run-log.md`, the task's section below, and the code and docs it touches.
2. **Branch.** `git switch run/week-2`, `git pull`, `git switch -c <branch>` (names in section 7).
3. **Decide and plan.** Settle the task's decisions (section 5). Write the plan into the run log's task entry (files, approach, tests, decision IDs), update the resume point, and make that the branch's first commit.
4. **Build** in logical commits, each with its tests. In the same PR:
   - Migrations: autogenerate, then review; write triggers, checks and partial indexes by hand (CLAUDE.md). The drift test must pass. On the dev database, run the round trip: `uv run alembic upgrade head`, `uv run alembic downgrade -1`, `uv run alembic upgrade head`.
   - Tests: worked examples for every rule. Rules on the ledger side (alerts, draft lines) also get their invariants in the Hypothesis state machine in `tests/test_stock_properties.py` (CLAUDE.md's Testing section).
   - Docs: `docs/schema.md` (tables, rules, constraints, indexes, and its Last updated date), CLAUDE.md (commands; hard-rule text that describes the code; the Current milestone only in task 7) and the README if a command changes.
   - Push after every commit and keep the resume point current, so an interruption loses little.
5. **Check**, from `api/`. The suite and reset can take several minutes: give them a long timeout (up to 10 minutes) or run them in the background and poll.
   - `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .`
   - Record the test count, the suite's time and gate 1's test time (pytest's `--durations`). If gate 1's test slows by more than about 20% in one task, find out why and note it.
   - Reset the dev database, `echo yes | uv run python -m app.cli reset --end-date 2026-10-31` (plus any options the task adds), then `uv run python -m app.cli reconcile`. Tasks 2, 3 and 6 must not change the simulator's movements: its receipts, usage movements, shortfalls, settle pairs and reversals must match the baseline exactly. Tasks 4 and 5 change them by design: record the new summary.
   - `git diff --check run/week-2...HEAD` prints nothing.
6. **PR.** Finish the run-log entry and push, then `gh pr create --base run/week-2`, with a title like "Add par levels and alerts (week 2 task 2)" and the body from Appendix C, written to a file outside the repo and passed with `--body-file`.
7. **Review with fresh eyes.** Run `/code-review` on the PR if it's available in this session. If not, start a subagent that hasn't seen the build, give it the diff (`git diff run/week-2...HEAD`), CLAUDE.md, `docs/schema.md`, `docs/decisions.md` and this plan, and ask for correctness bugs, breaches of hard rules or decisions, missing tests and docs that no longer match the code, not style. Fix every correctness finding, run the checks again, and push, with the PR number added to the run log. Then add the findings and what you did about each to the PR description (`gh pr edit <number> --body-file`); for any you decline, say why there and under For Yazan.
8. **CI and merge.** `gh pr checks <number> --watch`. If CI fails, fix, push and watch again; if a failure looks flaky, re-run it once (`gh run rerun --failed`), and if it fails again, treat it as real. When it's green, `gh pr merge <number> --squash`. GitHub deletes the head branch itself; leave the local one.
9. **Next.** Start the next task straight away. Its first commit (step 3) records the merge in the run log's resume point and timeline.

Only the main session edits files, commits and pushes. Subagents are for reading wide areas of code and for the review, to keep your context lean; they share the same usage limits.

## 7. The tasks

Scope comes from CLAUDE.md's Current milestone, `docs/spec.md` (U6, U7, U10, U12), `docs/schema.md` (the planned tables and rule 8) and D49–D53. Each task's open decisions come from the project's status doc; the ones marked (new) were found while writing this plan. They're yours to make: the notes are things to weigh, not answers.

### Task 2: par levels and alerts (branch `feat/alerts`)

Scope: the par_levels and alerts tables; the four alert types (low_stock, expiring, expired, negative_stock) raised and resolved by every stock write (receive, use, waste, reverse and count), plus a sweep for the date-based ones; GET /alerts. Hard rule 6's negative-stock alert lands here.

- **The open-alert key.** The schema's one-open-alert key (type, item_id, batch_id) leaves out location_id, so an open low-stock alert (which has no batch) at one location would block the same item's alert at another. Key it on location too.
- **Expiry window.** expiry_warn_days is one number per business (default 3), but the café's ka'ak keeps 1 day and its oat milk 90, so with 3 days every pastry counts as expiring on arrival. Decide per business, per item or from shelf life, without an alert on every delivery.
- **When alerts are evaluated:** inside every stock write (in its transaction, after its movements, under the default-batch lock it already holds, D36), plus a sweep for expiring and expired. raised_at and resolved_at are business time (the write's occurred_at, or the time the sweep runs as of), so simulated days get simulated times. A sweep and a write mustn't race into duplicate or stale alerts.
- **Low stock's usable on-hand:** whether a negative default batch counts against it. Expired stock is excluded (rule 8's "usable"). Task 3's draft lines use the same definition.
- (new) **What runs the sweep outside the simulator:** a CLI command, the API's start-up or a read of GET /alerts. No scheduler and no new service.
- (new) **Granularity and lifecycle:** which types carry a batch (expiring and expired per batch, low stock per item and location, negative stock per default batch or per item); when each resolves; whether a recurrence reopens the row or raises a new one. History matters: week 3's expiry log (U11) shows what expired, when, and what was done with it. Also items without par levels, and inactive items.
- (new) **GET /alerts:** filters (status, type, location, item), the order, and the item and batch details it returns, in D47–D48's style.
- (new) **Proof:** a reconcile check that the open alerts match a fresh evaluation from the balances (the date-based ones as of the latest sweep), so gate 2 shows the alerts are right and not just present, and the same as a Hypothesis invariant.
- (new) **Cost per write:** evaluate only what the write touched, in as few statements as you can.

### Task 3: draft orders (branch `feat/draft-orders`)

Scope: purchase_orders and purchase_order_lines; po_id on receipts (D30); one open draft per supplier and location (the partial unique index), filled from par levels and rounded up to whole packs (rule 8, U7); endpoints to list and read orders, edit a draft's lines, send it, and receive against it (POST /receipts with a po_id).

- **Statuses:** draft, sent, received, and maybe cancelled; what each one allows.
- **Which supplier:** the preferred one (supplier_items.is_preferred); items with no preferred supplier, or more than one.
- **On order:** open drafts plus sent orders not yet received.
- **Line growth:** whether a line grows when stock falls further, and what happens to it if stock recovers before the draft is sent.
- **Receiving against an order:** matching receipt lines to order lines, when an order counts as received, short and over deliveries, items that weren't on the order.
- (new) **When lines are added:** inside the stock writes, like alerts, or by the sweep. Rule 8 says when on-hand falls to reorder_point, so at or below it, while U7 says "below": follow rule 8 and say so in the PR.
- (new) **Packs:** the supplier's pack unit (supplier_items.item_unit_id); whether packs are whole numbers; what the owner's edit changes (qty_packs) and what keeps the suggestion (suggested_packs).
- (new) **origin** (the schema lists it on purchase_orders): its values and what sets them.

### Task 4: the simulator on week 2 rules, for the café (branch `feat/sim-week2`)

Scope: par levels in the venue data; deliveries from draft orders (D52, superseding D40): acting as the owner, the simulator sends each supplier's open draft on that supplier's order days and receives it on the first delivery day after the lead time, with a few deliveries late or short; counts that find theft and over-portioning; expired stock wasted because of expired alerts; the sweep run each simulated day. Opening stock stays a day-0 delivery from each supplier. The simulator still writes only through the domain functions with source simulator (D41, D56), and the same seed and end date still give an identical ledger.

- **Par levels** per item, and each supplier's order days from its delivery weekdays and lead time.
- **The count schedule,** and theft and over-portioning as physical stock that differs from what's logged (the simulator keeps its own physical quantities), so counts find variances.
- **Expired waste** driven by open expired alerts instead of the simulator's own morning check.
- **The last day and now():** once the simulator counts at night, a reset during the day writes a count later than the clock, and the barrier (D51) then refuses API writes dated before it. Decide whether the last day stops at now() (brought forward from week 3), or how else to avoid it.
- (new) **Reversals across counts:** a reversal copies its original's occurred_at (D36), so once an item is counted, no earlier request for it can be reversed (D51). Plan reversals so they never cross a count.
- (new) **The summary and gate 1's test:** what the summary adds (alerts raised and resolved, drafts sent, orders received, counts and variances) and what gate 1's test now checks. It must keep passing; task 5 decides its future.

### Task 5: the sweets shop and the cloud kitchen (branch `feat/venues`)

Scope: two more venues as plain data, like `app/sim/cafe.py` (made up, like Dar Zaatar Café); reset and simulate seeding and running all three (simulate seeds only the café today, and refuses when any business exists); 90-day runs; the gate 2 CI test (D53): each venue for 14 days, each reconciles, raises alerts and draft orders, and exercises the main paths.

- **The venues' data:** items, units and packs, shelf lives, suppliers, par levels, counts and usage patterns. Use them to exercise what the café doesn't (U10: one system fits a café and a sweets shop).
- **The CLI:** options, the default number of days, a random seed per venue, and how reset reports three venues.
- **Gate 1's test:** whether it stays or folds into gate 2's.
- **CI time:** record it before and after, and keep the suite as quick as D53 intends.
- **Evidence in the PR:** `reset --days 90 --end-date 2026-10-31`, then reconcile: each venue's summary and how long it took.

### Task 6: the item setup API (branch `feat/item-setup`)

Scope (U10): create and update items, their units and packs, tracking flags, supplier prices (supplier_items) and par levels, built on GET /catalogue (D45), in D47's style and scoped by business (hard rule 5).

- **Changing tracks_batches, tracks_expiry or a unit's factor_to_base** once stock or orders exist.
- **Deactivating an item** that still has stock, open alerts or draft lines.
- **Changing the preferred supplier,** and what happens to open drafts.
- (new) **What else is fixed** after an item's first movement (base_unit at least), SKU uniqueness, and whether setup writes need a client_id like stock writes.
- (new) **Whether a par-level change** re-evaluates low stock and draft lines straight away.

### Task 7: gate 2 housekeeping (branch `feat/gate-2`)

- `docs/decisions.md`: every run decision present, statuses right, the Open section current.
- CLAUDE.md: Current milestone for week 3 (dashboard), from the spec's milestone row and user stories, with gate 3 ("Demo script runs in the browser"); commands and hard-rule text current. Keep the temporary run section until the final step.
- `docs/schema.md`: the Last updated date, the rules intro (which rules now have property tests), anything stale.
- README: the Status section describes week 2.
- Gate 2 evidence, in the PR and the run log: the 3 × 90 reset output and reconcile, alerts and draft orders per venue, the test count and suite time on this machine, and CI's time.

### Hardening (only after task 7, one PR each, if you still can)

- **`feat/receipt-line-location`:** receipt lines reference their batch by item only, so the database doesn't tie the batch to the receipt's location. Count lines carry location_id for this reason (D57). This one is a decision.
- **`feat/write-lookups`:** since PR #9, every new write runs two more small queries (the client_id lookup in stock_counts and the latest-count lookup). Merge them if it stays clear, and measure gate 1's test before and after.
- After either, run the 3 × 90 reset again and confirm the gate 2 evidence still holds.

Then do the final step.

## 8. Interruptions and resuming

- Push after every commit and keep the resume point current: anything not pushed may be lost.
- If Postgres stops (it has once, with exit code 255), run `docker compose up -d db` from the repo root and carry on; note it in the timeline.
- If you hit a usage limit, or an error you can't get past, stop at a clean point: commit and push what's sound, write the resume point (what's done, what's next, anything half-built), and stop.
- After a compaction, re-read sections 1–6 of this plan and the run log before the next step.

**Resuming,** in a new session (maybe on another model):

1. `git fetch --all`, then `gh pr list --state all --base run/week-2` and the branch list, to find the newest run branch: an unmerged feature branch if a task was in progress, otherwise `run/week-2`.
2. Check it out and read `docs/run-plan.md` and `docs/run-log.md` there. If the working tree has uncommitted changes from the interrupted session, inspect them: commit what's sound, stash the rest (`git stash push -m "interrupted run"`) rather than discarding it, and say which in the run log.
3. Add a timeline line (the time, and the model if it changed), then carry on from the resume point.

The resume prompt Yazan pastes: "Resume the week 2 run: find the newest run branch, read section 8 of docs/run-plan.md and docs/run-log.md there, and carry on from the resume point without asking me anything. The standing approvals still apply: you may squash-merge your own pull requests into run/week-2 once CI is green, and reset the local dev database and run Alembic downgrades on it."

## 9. The final step

1. Remove the temporary run section from CLAUDE.md, and finish the run log: status "Complete", the For Yazan list deduplicated with the most important first, and a suggested review order. Commit straight to `run/week-2` and push.
2. Open the final PR as a draft: `gh pr create --draft --base main --head run/week-2`, titled "Week 2: rules and simulator (tasks 2–7, gate 2)", with the body from Appendix D.
3. Wait for CI to go green; if it fails, fix it through a PR into `run/week-2` as usual. Don't merge the final PR and don't mark it ready.
4. Stop and report: the PR link, what's done, what was skipped, and the top For Yazan items.

## Appendix A: decision rows to add in step 0

Append these rows after D48, in this order, verbatim (the header is here only so they render as a table; don't add it again):

| ID | Date | Decision | Status |
| --- | --- | --- | --- |
| D49 | 2026-11-11 | Week 2 is split into seven tasks, one PR each, in this order: (1) stock counts and the backdated-writes rule, (2) par levels and alerts, (3) draft orders, (4) the simulator on week 2 rules for the café, (5) the sweets shop and cloud kitchen with 90-day runs and the gate 2 test, (6) the item setup API, (7) gate 2 housekeeping. Each feature brings its own migration, domain code, endpoints and tests, rather than one migration task first. Item setup comes after the simulator to protect the gate, and can move into week 3 next to its screen. | Active |
| D50 | 2026-11-11 | Backdated writes, part 1 (closes the open item): FEFO draws only from batches received at or before the usage's occurred_at, and waste can't name a batch received after its occurred_at. The default batch is always available and still takes whatever remains. Extends domain rule 2, D31 and D33. | Active |
| D51 | 2026-11-11 | Backdated writes, part 2: a count is a barrier per item and location. It must be dated at or after every movement already logged for the item there, and afterwards no write for the item there can be dated at or before it, reversals of earlier requests included. Amends D46: past times are accepted unless at or before the item's latest count at that location. | Active |
| D52 | 2026-11-11 | From week 2's task 4, the simulator's deliveries come from draft orders: acting as the owner, it sends each supplier's open draft on that supplier's order days and receives it on the first delivery day after the lead time, with a few deliveries late or short. Opening stock stays a day-0 delivery from each supplier. Supersedes D40 from then. | Active |
| D53 | 2026-11-11 | The gate 2 CI test runs each of the three venues for 14 days and checks that each one reconciles, raises alerts and draft orders, and exercises the main paths. The full 3 × 90 days runs through reset at the gate, and its output goes in the gate PR. | Active |
| D54 | 2026-11-11 | A count line names a batch, and counting an item covers all its batches at the location. An item without batch tracking counts its default batch. For a batch-tracked item, named batches take their counted quantity, any other batch with stock counts as zero, and the default batch is set to zero; stock without a batch comes in through a receipt. Each line keeps expected_qty and counted_qty, and each difference posts one count_adjust movement at the batch's unit_cost. | Active |
| D55 | 2026-11-11 | A count is submitted in one request (POST /counts, with counted_at and every line) and replays on its client_id. There are no server-side draft counts, so stock_counts drops the status and submitted_at columns the schema listed; week 4's app keeps an unfinished count on the phone. | Active |
| D56 | 2026-11-11 | count_adjust movements take their channel as source: count from the API and simulator from the simulator, so D41's rule that every simulator movement has source simulator still holds. | Active |
| D57 | 2026-11-11 | Week 2 task 1 details. count_stock lives in app/domain/counts.py and imports stock.py's private locking, ledger and check helpers instead of moving them; the barrier lookup and the client_id check that knows about counts live in stock.py. stock_counts has unique (business_id, client_id) and unique (business_id, id, location_id). Count lines have counted_qty >= 0, one line per batch per count, hand-named keys to their count's location (fk_stock_count_lines_count) and to a batch there (fk_stock_count_lines_batch), and an index on (location_id, item_id) for the barrier; movements reference their line on (business_id, stock_count_line_id, batch_id). Every counted item gets a default-batch line, so an item's latest count is the latest counted_at over its lines at the location. Writes check the barrier after locking their default batches and after their replay check (reverse also after its already-reversed check, as built in PR #9); a count checks it after both lock statements, against every batch of the item at the location, empty ones included, because the usage that empties a batch can be the item's latest movement. Named batches are checked before any lock, so each refusal gets its own message. Lines are read back in one order (SKU, then the default batch last, then FEFO), shared with the replay, and count_adjusts post in that order. allocate_usage takes occurred_at; check_counted_quantity allows zero. A client_id used by a count is refused for other requests and the reverse; a count that changes nothing writes no movements, so a concurrent request reusing its client_id isn't caught (accepted). reconcile gains count_line_unposted and count_line_mismatch. POST /counts reuses the stock endpoints' helpers, and MovementOut gains stock_count_line_id. | Active |
| D58 | 2026-11-14 | The rest of week 2 (tasks 2–7, then week 1 hardening if time allows) runs as one autonomous Claude Code run on Fable 5.2 (Opus 5.5 if the Fable allowance runs out), following docs/run-plan.md, which overrides CLAUDE.md's workflow for the run. Claude Code takes the open decisions itself and logs each in docs/decisions.md from D59 with status "Active (run, task N, to review)", and keeps its progress, assumptions and questions for Yazan in docs/run-log.md. Each task is a pull request into the integration branch run/week-2, squash-merged there after green CI and a fresh-context code review. main changes only through one final draft pull request from run/week-2, which Yazan reviews in full and merges himself, so D18 still holds for main. The run can't change existing decisions or hard rules, only propose changes. | Active |

## Appendix B: the run log

```
# Run log: week 2 run (D58)

Plan: docs/run-plan.md. Integration branch: run/week-2. Final PR: not yet.

## Resume point

- Status: in progress
- Task and step: step 0
- Branch and PR: run/week-2
- Done so far:
- Next:
- Next decision ID: D59
- Updated:

## For Yazan (look at these first)

- None yet.

## Assumptions

- None yet.

## Baseline (main at 0e75a8c)

- Tests, suite time, gate 1's test time:
- reset --end-date 2026-10-31:
- reconcile:

## Tasks

### Task 2: par levels and alerts

- Branch, PR, merge commit:
- Plan:
- Decisions:
- Checks: test count, suite time, gate 1's test time; migration round trip; reset summary; reconcile; CI time
- Review: findings and what was done
- Notes:

(The same block for tasks 3–7, and one for each hardening PR.)

## Timeline

- YYYY-MM-DD HH:MM: step 0 started (model: ...)
```

## Appendix C: task PR description

```
Week 2 task N, from the autonomous run (D58). Merges into run/week-2; Yazan reviews it in the final PR into main.

## Summary
## Plan as built
## Decisions (Dxx–Dyy)
For each: the decision, the options weighed, and why this one.
## Assumptions
## Tests
What's new, the test count before and after, the suite's time and gate 1's test time.
## Checks run
pytest, ruff check, ruff format --check, the migration round trip, the reset summary against the baseline, reconcile.
## Review
Each finding from /code-review (or the review subagent) and what was done; declined findings with the reason.
## For Yazan
## Follow-ups
```

## Appendix D: final PR description

```
Week 2 tasks 2–7 and gate 2, built in one autonomous run (D58). A draft until Yazan has reviewed it: don't merge.

## Gate 2
The 3 × 90 reset output and reconcile, alerts and draft orders per venue, the test count and times, CI.
## Tasks
One line each with its PR link, then any hardening PRs.
## Decisions made in the run
A table of D59 onwards: ID, task, the one-line decision.
## For Yazan
The run log's list, most important first.
## Assumptions
## Suggested review order
1. The For Yazan list and decisions D59 onwards
2. Migrations and docs/schema.md
3. Domain code (alerts, orders) and its tests
4. API
5. Simulator and venues
6. CLAUDE.md and the README
## Skipped or not done
```
