# Perishable Stock Manager — Core Schema

Author: Yazan Louid · Last updated: 11 Nov 2026 · Related: [spec.md](spec.md), [decisions.md](decisions.md)

## Design principles

The ledger is the single source of truth: every other stock number is derived from it and can be rebuilt from it.

- **Tenancy:** every table has `business_id`. Child rows use composite foreign keys on (`business_id`, `id`), so no row can point at another tenant's data.
- **Locations from day 1:** stock rows carry `location_id` even though each demo business has one location. Adding it to a ledger later means rewriting history.
- **Append-only ledger:** `stock_movements` accepts inserts only. A mistake is fixed with a reversal row, never an edit.
- **Units:** every quantity is stored in the item's base unit (g, ml or piece) as numeric(14,3). Pack units convert at entry. No floats.
- **Money:** numeric(14,3) with a currency per business; three decimals fit Jordanian fils. The one exception is `unit_cost`, which is per base unit and numeric(18,6) on batches, movements and receipt lines, because a pack price spread over thousands of grams needs more decimals. Receipt lines also keep `pack_price` as invoiced, in numeric(14,3). Unit cost lives on the batch, so waste and usage are valued at what was actually paid.
- **Time and IDs:** timestamptz throughout. A movement's `occurred_at` is when the event happened, set by the caller, and `created_at` is when the server wrote it. Each request sends a client-generated `client_id`, shared by every movement it writes, and numbers those movements with `client_seq` (0, 1, 2…). Keys are UUIDv7 (built into Postgres 18). Together these make offline sync additive later.

## Tables

18 tables in five groups. Every table also has `id` (UUIDv7), `business_id` and `created_at`, so those are not repeated below.

| Group | Table | Holds | Key columns |
| --- | --- | --- | --- |
| Tenancy | businesses | The tenant | name, currency, timezone, expiry_warn_days |
| Tenancy | locations | A branch or storeroom | name, kind |
| Tenancy | users | Demo login | email, name, role (owner, staff), password_hash |
| Catalogue | items | Anything stocked | sku, name, category, base_unit, tracks_batches, tracks_expiry, shelf_life_days, is_active |
| Catalogue | item_units | Pack conversions | item_id, name (crate, bottle), factor_to_base |
| Catalogue | suppliers | Who you buy from | name, phone, lead_time_days |
| Catalogue | supplier_items | What each supplier sells | supplier_id, item_id, item_unit_id, pack_price, is_preferred |
| Stock | batches | A received lot | location_id, item_id, expires_on, lot_code, unit_cost, received_at, is_default |
| Stock | stock_movements | The ledger | location_id, item_id, batch_id, qty_delta, type, source, reason, unit_cost, receipt_line_id, reverses_id, occurred_at, user_id, client_id, client_seq |
| Stock | stock_balances | On-hand per batch (projection) | batch_id, location_id, item_id, qty_on_hand, last_movement_at |
| Documents | receipts, receipt_lines | Goods in | location_id, supplier_id, received_at, invoice_ref (po_id in week 2) · item_id, item_unit_id, qty_packs, qty_base, pack_price, unit_cost, expires_on, lot_code, batch_id |
| Documents | stock_counts, stock_count_lines | Stocktakes | location_id, counted_at, client_id, user_id · location_id, item_id, batch_id, expected_qty, counted_qty |
| Rules | par_levels | Reorder rule per item and location | location_id, item_id, reorder_point, order_up_to |
| Rules | purchase_orders, purchase_order_lines | Draft orders | location_id, supplier_id, status, origin · item_id, item_unit_id, qty_packs, suggested_packs |
| Rules | alerts | Open issues | location_id, item_id, batch_id, type, status, raised_at, resolved_at |

Enums: movement type is receive, usage, waste, count_adjust, settle or reversal. Movement source is manual, count or simulator; recipe, pos and invoice come later. Waste reason is expired, spoiled, damaged, prep_error, overproduction or other. Alert type is low_stock, expiring, expired or negative_stock. Waste needs no table of its own: it is a movement with type waste and a reason. Waste names the batch it comes from (an item without batch tracking uses its default batch), can't name a batch received after its `occurred_at`, and can't exceed that batch's on-hand.

A movement links to the document that caused it through a nullable foreign key per document type: `receipt_line_id` for receives and `stock_count_line_id` for count adjustments. Every batch has a `received_at`; a default batch takes the time it was created. `occurred_at`, `counted_at` and both `received_at` columns have no default, because the simulator replays past days.

## Domain rules

Nine rules keep the ledger honest; rules 1 to 7 have had property-based tests since week 1, and rule 9 since week 2. In [decisions.md](decisions.md), rule 3 is D13 (refined by D31) and rule 4 is D12.

1. **Balance invariant:** a batch's `qty_on_hand` always equals the sum of its movements. Both are written in one transaction, and a full rebuild from the ledger must match exactly.
2. **FEFO:** usage draws from the location's batches received at or before its `occurred_at`, ordered by `expires_on` (no date last), then `received_at`, then `id`, locked with SELECT … FOR UPDATE. The default batch is always available: it comes last and takes whatever remains. One usage can split into one movement per batch.
3. **Expired stock is skipped:** FEFO never draws from a batch past its expiry date: a batch is usable on its expiry date and skipped from the next day. The date is the usage's `occurred_at` in the business's timezone, converted in Postgres. The batch stays on hand with an expired alert until someone wastes it.
4. **Negative stock is allowed, and flagged:** a shortfall posts to the item's default batch, which goes negative and raises an alert. The next receipt settles it first with a settle pair (out of the new batch, into the default batch). Blocking usage because a delivery wasn't logged is worse than a visible gap.
5. **One code path:** every item has exactly one default batch per location, with no expiry. Items without batch tracking only ever use it.
6. **Corrections:** a reversal posts the negated quantity with `reverses_id` set. Each movement can be reversed once. v1 reverses whole usage and waste requests: every movement of the request, each copying its original's `unit_cost` and `occurred_at`, so the pair nets to zero in quantity and value. Receipts (with their settle movements) and reversals can't be reversed.
7. **Valuation:** each movement copies its batch's `unit_cost`, so waste and usage reports never join back to receipts. A default batch's cost is the weighted average of what it holds: a receive sets it to (on_hand × cost + qty × line cost) / (on_hand + qty), and when on_hand ≤ 0 or the cost is unknown, the line's cost replaces it (D35).
8. **Draft orders:** when usable on-hand (not expired) falls to `reorder_point`, add a line to that supplier's one open draft: `order_up_to` minus on-hand minus quantity already on order, rounded up to whole packs.
9. **Counts:** a count sets an item's stock at a location to what was counted at `counted_at`, and covers all of the item's batches there. An item without batch tracking counts its default batch. For a batch-tracked item, each named batch takes its counted quantity, any other batch with stock counts as zero, and the default batch is set to zero (stock without a batch comes in through a receipt). Each batch the count covers, the default batch included, gets a line with its expected and counted quantities, and each difference posts one `count_adjust` at that batch's `unit_cost`. A count is a barrier: it must be dated at or after every movement of the item at that location, and nothing for the item there can then be dated at or before it, reversals of earlier requests included.

## Constraints and indexes

The database enforces every rule it can, so an API bug cannot corrupt the ledger.

- **Append-only:** a trigger on `stock_movements` rejects UPDATE, DELETE and TRUNCATE. The trigger alone enforces it: the app connects as a superuser, so revoking privileges would have no effect. A non-superuser app role arrives with row-level security, before any pilot.
- **Checks:** `qty_delta` ≠ 0, and its sign matches the type (receive > 0; usage and waste < 0). `reason` is set for waste and only for waste; `reverses_id` is set if and only if the type is reversal; `receipt_line_id` is set if and only if the type is receive; `stock_count_line_id` is set if and only if the type is count_adjust. Also `factor_to_base` > 0, `counted_qty` ≥ 0 and `reorder_point` ≤ `order_up_to`.
- **Uniqueness:** (`business_id`, `client_id`, `client_seq`) on `stock_movements` makes retries idempotent, and (`business_id`, `client_id`) on `stock_counts` does the same for counts, including counts that post nothing; `reverses_id`, `receipt_line_id` and `stock_count_line_id` are unique; a count has one line per batch (`count_id`, `batch_id`). Composite keys tie a count line to its count's location and to a batch there, and a count adjustment to its line's batch.
- **Partial unique indexes:** one default batch per (`location_id`, `item_id`); one draft order per (`location_id`, `supplier_id`); one open alert per (`type`, `item_id`, `batch_id`), using NULLS NOT DISTINCT.
- **Indexes:** a FEFO index on batches (`location_id`, `item_id`, `expires_on`, `received_at`), where ascending order already puts no expiry last; `stock_movements` on (`business_id`, `item_id`, `occurred_at`) for history and reports; `stock_balances` on (`location_id`, `item_id`); `stock_count_lines` on (`location_id`, `item_id`) for the count barrier.
- **Row-level security:** off in v1; the API scopes every query by the request's `business_id`, read from the `X-Business-Id` header until demo login (D43) and from the token after that. Turn it on before any real tenant: it costs a session setting per request and makes debugging slower now.

## Deferred tables

Each later feature adds tables or enum values; none of them rewrites the ledger.

- **Transfers:** transfer_out and transfer_in movement types plus a transfers header.
- **Recipes:** products, recipe_lines, sales and sale_lines, posting usage with source recipe (D9 in [decisions.md](decisions.md)); sub-recipes and production batches after that.
- **E-invoice import:** `invoice_documents` holding the raw UBL XML and QR payload, linked to receipts.
- **Offline sync:** devices and sync cursors; `client_id` and `occurred_at` already exist.
- **Capture extras:** attachments for waste photos and voice notes.
- **Other domains:** payables and supplier statements, temperature and storage logs, an audit log.
