# Perishable Stock Manager

Stock management for small businesses with perishable stock, demoed with a café, a sweets shop and a cloud kitchen. It is built around an append-only stock ledger with batch expiry dates, and usage draws the earliest-expiring stock first (FEFO). It is a portfolio project that runs locally on generated demo data, with no paid services or keys.

The scope, data model and decisions live in [docs/](docs/): [spec.md](docs/spec.md), [schema.md](docs/schema.md) and [decisions.md](docs/decisions.md).

## Status

Scaffold only: a FastAPI app with a `/health` endpoint, PostgreSQL in Docker, Alembic, tests and CI. The ledger core lands in week 1.

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
| Fresh demo data (asks first, then recreates the schema, seeds and simulates 30 days) | `uv run python -m app.cli reset` |
| Seed and simulate into an empty database | `uv run python -m app.cli simulate` |
| Check the ledger reconciles | `uv run python -m app.cli reconcile` |

`reset` and `simulate` take `--days N`, `--end-date YYYY-MM-DD` and `--random-seed N`. The same seed and end date always give the same data.

To try the API, run it and open http://localhost:8000/docs. Every endpoint except `/health` needs an `X-Business-Id` header: `reconcile` prints each business's id next to its name. `GET /catalogue` lists the location, supplier, item and unit ids the write endpoints take, and `GET /stock` shows on-hand by batch. Send quantities and prices as JSON strings, such as `"2500.5"`. A JSON number is read as a float before it becomes a decimal: that is exact for every value the database stores (up to 15 significant digits), but it could round a longer number.

Tests use a separate `stock_test` database on the same Postgres. The test setup creates it and applies the migrations, and every test runs in a transaction that is rolled back.

CI (GitHub Actions) runs Ruff check, Ruff format check and pytest against Postgres 18 on every pull request and every push to `main`.
