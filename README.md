# Perishable Stock Manager

Stock management for small businesses with perishable stock, demoed with a café, a sweets shop and a cloud kitchen. It is built around an append-only stock ledger with batch expiry dates, and usage draws the earliest-expiring stock first (FEFO). It is a portfolio project that runs locally on generated demo data, with no paid services or keys.

The scope, data model and decisions live in [docs/](docs/): [spec.md](docs/spec.md), [schema.md](docs/schema.md) and [decisions.md](docs/decisions.md).

## Status

Weeks 1 and 2 of four are done, and both gates pass: the ledger reconciles exactly, and the simulator raises alerts and draft orders on its own for three venues, each reconciling after 90 days. CI runs the gate test for 14 days per venue.

- **Ledger:** append-only stock movements with batches, expiry dates and an on-hand projection that rebuilds from the ledger. Receive, use (earliest-expiring stock first, skipping expired batches), waste, reverse and count run as domain functions on Postgres, with property-based tests for the ledger rules.
- **Rules:** stock counts that post their differences and act as a barrier for backdated writes; par levels with a low-stock level, a reorder point and an order-up-to level; alerts for low stock, expiring and expired batches and negative stock, kept in step with every stock write; and one draft order per supplier, filled from the par levels in whole packs, which the owner can edit, send, cancel and receive against.
- **Demo data:** three made-up venues, a café and a sweets shop in Amman and a cloud kitchen in Dubai, simulated for up to 90 days through the domain functions: the simulated owner sends the drafts, suppliers deliver (some late or short), staff over-portion and the odd item walks, and weekly counts find it. A reconcile check proves the balances, alerts and drafts against the ledger.
- **API:** stock writes, counts, alerts, orders, item setup, and read-only stock and catalogue endpoints.

The dashboard follows in week 3 and the capture app in week 4.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 for you)
- git

## First-time setup

Copy the example environment file and set your own password in both `POSTGRES_PASSWORD` and `DATABASE_URL`. In PowerShell:

```powershell
Copy-Item .env.example .env
```

Then start the database and install the API's dependencies:

```powershell
docker compose up -d db
```

```powershell
cd api
```

```powershell
uv sync
```

## Everyday commands

From the repo root:

| Task | Command |
| --- | --- |
| Start the database | `docker compose up -d db` |
| Stop it | `docker compose down` |

From `api/`:

| Task | Command |
| --- | --- |
| Apply migrations | `uv run alembic upgrade head` |
| New migration | `uv run alembic revision --autogenerate -m "<message>"`, then review it |
| Run the API | `uv run uvicorn app.main:app --reload` (docs at http://localhost:8000/docs) |
| Tests | `uv run pytest` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Fresh demo data (asks first, then recreates the schema, seeds the three demo venues and simulates the 30 days up to now) | `uv run python -m app.cli reset` |
| Seed and simulate the three venues into an empty database | `uv run python -m app.cli simulate` |
| Check the ledger reconciles | `uv run python -m app.cli reconcile` |

`reset` and `simulate` take `--days N`, `--end-date YYYY-MM-DD` and `--random-seed N`. The end date defaults to today, and a run that ends today stops at the current time, so nothing is dated in the future and the API can write straight away; an end date in the future is refused. The same seed and a past end date always give the same data. The three venues (a café in Amman, a sweets shop in Amman and a cloud kitchen in Dubai) each get their own business, with the seed plus 0, 1 and 2. The full demo is `--days 90`, which takes about six minutes.

To try the API, run it and open http://localhost:8000/docs. Every endpoint except `/health` needs an `X-Business-Id` header: `reconcile` prints each business's id next to its name. `GET /catalogue` lists the location, supplier, item and unit ids the write endpoints take, `GET /stock` shows on-hand by batch, `GET /alerts` lists low stock, expiring and expired batches and negative stock, and `GET /orders` lists the draft orders the par levels raise, which can be edited, sent, cancelled and received against. `/items` sets items up: create and change them (an item's base unit and tracking are fixed once it has stock movements), add and rename pack units (a unit's size never changes), set supplier prices and the preferred supplier, and set par levels per location, whose alerts and draft lines follow at once. Send quantities and prices as JSON strings, such as `"2500.5"`. A JSON number is read as a float before it becomes a decimal: that is exact for every value the database stores (up to 15 significant digits), but it could round a longer number.

Tests use a separate `stock_test` database on the same Postgres. The test setup creates it and applies the migrations, and every test runs in a transaction that is rolled back.

CI (GitHub Actions) runs Ruff check, Ruff format check and pytest against Postgres 18 on every pull request and every push to `main`.
