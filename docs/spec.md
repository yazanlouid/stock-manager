# Perishable Stock Manager — v1 Spec

Author: Yazan Louid · Last updated: 7 Nov 2026 · Related: [schema.md](schema.md), [decisions.md](decisions.md)

## Goal and definition of done

v1 proves that an append-only ledger with batch expiry and FEFO can run a perishable storeroom from delivery to waste, on generated data, at zero cost.

Done means a reviewer sees all of this on a fresh clone:

- `docker compose up` starts Postgres, the API, the dashboard and seeded demo data, with no paid services or keys.
- The simulator replays 90 days for three demo venues (café, sweets shop and cloud kitchen, the verticals that desk research on the Jordan market rated strongest), and every balance matches the ledger.
- A five-minute demo walks one loop: receive a delivery with expiry dates → usage draws the earliest-expiring stock first → an expiring-soon alert → waste logged and valued → low stock drafts an order.
- Core stock rules are covered by property-based tests, and CI is green.

## In scope

One mechanism per concern, configured per item rather than coded per business type.

- **Catalogue:** items with a base unit (g, ml or piece), pack units per item (crate = 12 kg), per-item batch and expiry tracking, and suppliers with pack prices.
- **Ledger:** append-only stock movements in base units, corrections by reversal only, and on-hand per batch kept as a projection that can be rebuilt.
- **Receiving:** manual goods receipt; each line creates a batch with expiry, lot and unit cost.
- **Usage:** logged per item in base units, each movement tagged with its source (manual, count or simulator). A partial unit is just a base-unit quantity (30 ml from a 700 ml bottle). Allocation is FEFO across batches; recipes arrive later as one more source.
- **Waste:** logged per item or batch with a reason code, valued at batch cost.
- **Counts:** stocktake per location; variances post as adjustment movements.
- **Alerts:** low stock, expiring within N days, expired on hand, negative stock.
- **Draft orders:** one draft per supplier, filled from par levels and rounded up to whole packs. The owner edits it and marks it sent or received; nothing is actually sent.
- **Tenancy and access:** business → locations, `business_id` on every row, demo login with owner and staff roles.
- **Data:** a seeded generator plus a day-by-day simulator that generates usage, deliveries and waste through the domain services, never raw SQL.
- **Surfaces:** a React dashboard (stock, batches, expiry log, alerts, waste, orders, item setup) and an Expo app with three capture screens (receive, waste, count).

## User stories

Twelve stories cover v1; the week column maps each one to its milestone.

| ID | Role | Story | Week |
| --- | --- | --- | --- |
| U1 | Staff | Record a delivery with an expiry date per line in under a minute, so stock is right from arrival. | 1 (API), 4 (app) |
| U2 | Staff | Log waste with a reason in two taps, so waste is captured instead of guessed. | 1 (API), 4 (app) |
| U3 | Staff | Count a shelf and submit it, so on-hand is corrected without me doing the maths. | 2 (API), 4 (app) |
| U4 | System | Deduct every usage from the earliest-expiring batch first, so old stock leaves first. | 1 |
| U5 | Owner | See on-hand by item, location and batch with expiry, so I know my stock without walking the storeroom. | 3 |
| U6 | Owner | Get alerted to batches expiring within N days, so I use or discount them before they become waste. | 2 (rule), 3 (screen) |
| U7 | Owner | Get a draft order per supplier when items fall below their reorder point, so ordering takes minutes. | 2 (rule), 3 (screen) |
| U8 | Owner | See waste by reason and value for any period, so I know what waste costs and where it starts. | 3 |
| U9 | Owner | See count variance by item, so I can spot theft or over-portioning. | 3 |
| U10 | Owner | Set an item's units, packs, tracking and par levels, so one system fits a café and a sweets shop. | 2 (API), 3 (screen) |
| U11 | Owner | See an expiry log of what expired, when and what was done with it, so I can show it at an inspection. | 3 |
| U12 | Developer | Seed and simulate N days for a venue type with one command, so the demo always has realistic data. | 1 (v0), 2 (full) |

## Out of scope

Everything below is deferred on purpose. The schema already leaves room for recipes, transfers, offline sync, invoice scanning and POS feeds, so none of them will need a migration that rewrites data.

| Item | Why not in v1 | Lands |
| --- | --- | --- |
| Transfers between locations | Needs a second live location; stock rows already carry `location_id` | v1.1 |
| Recipes, sub-recipes and prep batches | Deferred by decision (D9 in [decisions.md](decisions.md)); usage movements already carry a source, so recipes arrive as one more | v1.1 or later |
| Arabic UI and right-to-left layout | Names store any script and UI strings live in i18n files from day 1; translation waits | v1.1 |
| Offline sync in the app | Hard to do well; the app is online-only, but movements already carry client IDs and `occurred_at` | v2 |
| E-invoice and JoFotara QR scanning | What the QR holds is still unverified | v2, after the invoice test |
| POS and delivery-app feeds (Foodics, Talabat, Careem) | The simulator stands in for usage; movements already record their source | v2 |
| Sending orders to suppliers (WhatsApp, email) | How venues order is still an open research question | v2 |
| Forecasting, ML, Ramadan-aware par levels | No ML in v1, by decision | v2 |
| Supplier payments, payables, temperature logs | Separate domains from stock | Later |
| Real auth, invites, hosting, billing | Local demo only | Before any pilot |

## Milestones

Screens wait until the ledger reconciles: four one-week milestones, each closed by a gate that must pass before the next week starts.

| Week | Milestone | Scope | Gate |
| --- | --- | --- | --- |
| 1 | Ledger core | Schema + migrations; receive, use, waste; FEFO + reversals; seed v0, tests, CI | 30 café days reconcile exactly |
| 2 | Rules + simulator | Stock counts; par levels, alerts; draft orders; 3 venues × 90 days | Simulator raises alerts and orders |
| 3 | Dashboard | Stock + batches; alerts, waste; orders, item setup; expiry log | Demo script runs in the browser |
| 4 | Capture app | Expo: receive, waste, count; README, demo video; one-command setup | Fresh clone to demo in 10 minutes |

Week 1 builds no UI on purpose. If gate 1 slips, week 4's app falls back to responsive web screens instead of eating into ledger work.

## Risks and open decisions

The schedule, not the design, is the main risk: four weeks covers an API, a dashboard and a mobile app for one person.

- **Mobile slips:** if gate 1 lands more than two days late, the three capture screens ship as responsive web pages and Expo moves to v1.1.
- **Ledger edge cases:** negative stock, expired batches and usage recorded before deliveries are where stock systems break. They get property-based tests in week 1, before any screen exists.
- **Simulator realism:** the demo is only as convincing as its data. The simulator stays rule-based with seeded noise (weekday peaks, over-portioning, occasional theft).
- **Decided:** negative stock is allowed and flagged, and FEFO skips expired batches (approved 7 Nov 2026). See domain rules 3 and 4 in [schema.md](schema.md) and D12–D13 in [decisions.md](decisions.md).
