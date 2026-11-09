# Decisions Log — Perishable Stock Manager

Last updated: 9 Nov 2026. One line per decision, oldest first. To add one, append a row with the next ID. To change one, add a new row and mark the old one "Superseded by Dn" rather than deleting it.

Related: [spec.md](spec.md) and [schema.md](schema.md). The Jordan market research behind D1 is kept outside this repo.

## Open (needs a call)

None.

## Decisions

| ID | Date | Decision | Status |
| --- | --- | --- | --- |
| D1 | 2026-10-24 | Market focus: Jordan first; GCC only after Jordan is proven. | Active |
| D2 | 2026-11-01 | Build as a personal portfolio project, not a business, for now. | Active |
| D3 | 2026-11-06 | General system for any small business with perishable stock; no specific café or restaurant; generated demo data instead of real data collection. | Active |
| D4 | 2026-11-06 | Core design: business_id on every table; configurable items (expiry/batch tracking, unit conversions); append-only stock ledger; earliest-expiry-first usage (FEFO). | Active |
| D5 | 2026-11-06 | Pluggable consumption: direct sale, recipes, partial units. | Superseded by D9 |
| D6 | 2026-11-06 | Stack: FastAPI + PostgreSQL, React dashboard, Expo mobile app, Docker Compose; local and zero cost. | Active |
| D7 | 2026-11-06 | v1 is built in four weekly milestones; no ML in v1. | Active |
| D8 | 2026-11-06 | The project runs in a Claude Project, with instructions and the research doc in its knowledge. | Active |
| D9 | 2026-11-06 | v1 logs usage directly (manual, counts, simulator); recipe-based usage is deferred; movements carry a source field so recipes can be added later. | Active (reaffirmed by D17) |
| D10 | 2026-11-07 | location_id on every stock table from v1; one location per demo business. | Active |
| D11 | 2026-11-07 | Quantities are stored in the item's base unit (g, ml or piece) as numeric(14,3); money is numeric(14,3) with a currency per business. | Active |
| D12 | 2026-11-07 | Negative stock is allowed: it posts to the item's default batch, raises an alert and is settled by the next receipt. | Active |
| D13 | 2026-11-07 | FEFO skips expired batches; they stay on hand with an alert until logged as waste. | Active |
| D14 | 2026-11-07 | Milestones: W1 ledger core, W2 rules + simulator, W3 dashboard, W4 capture app. If gate 1 slips by more than two days, the capture screens ship as responsive web pages and Expo moves to v1.1. | Active |
| D15 | 2026-11-07 | Week 1 is backend only; it is done when 30 simulated café days reconcile exactly with the ledger. | Active |
| D16 | 2026-11-07 | Decisions are tracked in this log as a project file; the spec and schema copies in the project are snapshots of the editable docs. | Active |
| D17 | 2026-11-07 | Recipes stay out of v1 (reaffirms D9). Usage is logged per item with a source (manual, count or simulator); the products, recipe_lines, sales and sale_lines tables are deferred; spec and schema updated to match. | Active |
| D18 | 2026-11-07 | Code is written with Claude Code and Yazan reviews every change before it merges. Development happens on Yazan's Windows laptop. | Active |
| D19 | 2026-11-09 | Development runs directly on Windows, with no Ubuntu/WSL workspace: Postgres runs in Docker Desktop, the API through uv, web and mobile through Node. Everyday commands are documented instead of a Makefile; CI stays on Linux. | Active |
