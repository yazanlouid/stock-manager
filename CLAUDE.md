# CLAUDE.md

## Project

Perishable Stock Manager (repo `stock-manager`): stock management for small businesses with perishable stock, demoed with a café, a sweets shop and a cloud kitchen. It is a portfolio project that runs locally on generated demo data.

Before planning a task, read `docs/spec.md` (scope), `docs/schema.md` (data model and domain rules) and `docs/decisions.md` (decisions log). If a task conflicts with them, stop and ask. Never change a decision silently, and don't edit `docs/` unless asked.

## Stack

Keep it this simple, and ask before adding a dependency or a service.

- API in `api/`: Python 3.13 managed with uv, FastAPI served by uvicorn, SQLAlchemy 2 with synchronous sessions and plain `def` endpoints (no async database code), psycopg 3 (`psycopg[binary]`), Alembic, pydantic-settings for config
- Database: PostgreSQL 18 (`postgres:18` image) in Docker Compose, service `db`
- Tests: pytest, Hypothesis, and httpx for FastAPI's TestClient. Lint and format: Ruff
- CI: GitHub Actions on `ubuntu-latest` with a Postgres 18 service, running Ruff check, Ruff format check and pytest
- Later: React + TypeScript + Vite dashboard in `web/` (week 3), Expo app in `mobile/` (week 4)

## Environment

- The developer machine is Windows and Yazan's shell is PowerShell. There is no WSL and no Makefile.
- Everything must work on Windows and in Linux CI. Write any helper script in Python and run it with `uv run`; no `.sh` or `.ps1` scripts.
- When you give Yazan commands to run, put one command per line and don't chain them with `&&`, because Windows PowerShell 5.1 doesn't support it.
- Line endings are LF everywhere (`.gitattributes`).
- In development only Postgres runs in Docker. The API runs directly on Windows and reads `DATABASE_URL` from `.env` (host `127.0.0.1`, not `localhost`: Postgres is published on IPv4 loopback only, and Windows resolves `localhost` to `::1` first, where the connection hangs). The API and dashboard services join `docker-compose.yml` in week 4, for the one-command demo.
- The `postgres:18` image keeps its data under `/var/lib/postgresql/18/docker`. Mount the named volume at `/var/lib/postgresql`, never at `/var/lib/postgresql/data`, which breaks on 18.

## Commands

From the repo root:

- Start the database: `docker compose up -d db`
- Stop it: `docker compose down`

From `api/`:

- Apply migrations: `uv run alembic upgrade head`
- New migration: `uv run alembic revision --autogenerate -m "<message>"`, then review it. Triggers, checks and partial indexes are written by hand.
- Run the API: `uv run uvicorn app.main:app --reload` (docs at http://localhost:8000/docs)
- Tests: `uv run pytest`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .` (CI runs `uv run ruff format --check .`)
- Simulator: add the exact command here once it exists (week 1)

If a command stops matching the code, fix the code or update this section in the same change.

## Hard rules

1. `stock_movements` is append-only. Never UPDATE or DELETE its rows, in code, migrations or tests, and never disable its trigger. Fix mistakes with a reversal movement.
2. `stock_balances` is a projection. Write it in the same transaction as the movement. It must always equal the sum of its batch's movements, and a rebuild from the ledger must match exactly (domain rule 1).
3. Domain functions never commit. The API request, the simulator or the test owns the transaction.
4. Quantities are in the item's base unit (g, ml or piece) as numeric(14,3), and money is numeric(14,3). Use `Decimal` in Python, never floats.
5. Every table has `business_id` and every query is scoped by it. Stock rows also carry `location_id`.
6. FEFO order is `expires_on` ascending with no date last, then `received_at`. Skip expired batches. A shortfall posts to the item's default batch, which may go negative; the negative-stock alert arrives with the alerts work in week 2. See domain rules 2–5 in `docs/schema.md`.
7. Every movement has a `type` and a `source` (manual, count or simulator). Recipes are out of scope for v1: don't add products, recipe or sales tables.
8. Timestamps are timestamptz. Primary keys default to Postgres 18's `uuidv7()`.
9. No secrets in the repo. Config comes from `.env` (gitignored); `.env.example` is committed.

## Testing

- Any change to a domain rule needs a test. Ledger rules get property-based tests with Hypothesis.
- Keep FEFO allocation a pure function (batches in, allocations out), so most property tests run without the database.
- Database tests use a separate test database (`stock_test`) on the same Postgres, created by the test setup. Each test runs in a transaction that is rolled back, so tests never delete ledger rows. No testcontainers.
- In Hypothesis tests that touch the database, open and roll back a transaction inside each example, because function-scoped fixtures don't reset between examples.

## Workflow

- Plan first. Before writing code, give a short plan (files to change, tests to add) and wait for approval.
- One small task per branch (`feat/<task>`), small enough to review in about 15 minutes.
- Before saying a task is done, run the tests, lint and format check, and report what you ran and the result.
- Yazan reviews every change and must be able to explain every line, especially ledger and FEFO code. Explain anything non-obvious in the pull request description.
- Commit messages use a short imperative subject, for example "Add FEFO allocation for usage".
- Open a pull request with `gh pr create`; don't push code to `main`.
- If a task needs a new decision, say so. Yazan records it in the decisions log.

## Layout

```
docs/                spec, schema, decisions: the source of truth for scope and rules
api/                 FastAPI app (app/), Alembic migrations, tests
web/                 React dashboard (week 3)
mobile/              Expo app (week 4)
docker-compose.yml   db now; api and web services in week 4
```

## Current milestone

Setup, then week 1 (ledger core). No UI work yet.

Week 1 scope:

- Migrations: tenancy, catalogue, batches, stock_movements, stock_balances and receipts, with the append-only trigger, checks and indexes
- Domain functions: receive, use (FEFO), waste, reverse, settle, rebuild balances
- Property-based tests for domain rules 1–7
- Seed generator v0 (one café, about 40 items and 6 suppliers) and a 30-day simulator that calls the domain functions
- Thin API: receive, use, waste, reverse, read stock

Gate: 30 simulated café days reconcile exactly with the ledger, and CI is green. Update this section at each gate.
