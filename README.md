# dataquality-platform

> **Phase 1 (Foundation only)** of a multi-phase build. This phase wires up
> the application skeleton, database/broker connectivity, logging, error
> handling, and health checks. No business logic, tables, auth, or
> data-quality features exist yet — those land in later phases.

## What's here

A FastAPI backend, scaffolded as a modular monolith, with:

- `api` — FastAPI app (uvicorn), mounts `/api/v1` (empty router for now),
  exposes `/health` and `/readyz`.
- `postgres` — PostgreSQL 16, the application's database.
- `redis` — Redis 7, used as the Celery broker and result backend.
- `worker` — Celery worker, currently runs a single trivial health-check
  task (`app.core.celery_app.health_check_task`, returns `"pong"`) to prove
  the worker boots and can execute work.

## Run locally via Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

This starts `postgres`, `redis`, `api`, and `worker` on a shared Docker
network. Services resolve each other by service name (`postgres`, `redis`).

- API: http://localhost:8000
- Liveness: http://localhost:8000/health
- Readiness (checks Postgres + Redis): http://localhost:8000/readyz

## Run migrations

Migrations run via Alembic, wired to `Settings.DATABASE_URL` and the shared
SQLAlchemy `Base` (`app/db/base.py`). Phase 1 ships one empty baseline
migration only — no application tables yet.

```bash
# from the project root, with DATABASE_URL pointing at a reachable Postgres
alembic upgrade head
```

## Run tests

```bash
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt

pytest
```

`test_readyz.py` exercises real Postgres/Redis connectivity (no mocks), so
the Docker Compose `postgres` and `redis` services (or equivalents on
`localhost`) must be reachable for it to pass.

## Configuration

All settings are environment variables read via `pydantic-settings`
(`app/core/config.py`). See `.env.example` for the full list and local
defaults.

## Project layout

```
app/
  main.py            # FastAPI app factory, /health, /readyz
  api/v1/router.py    # empty v1 APIRouter, mount point for future endpoints
  core/
    config.py         # Settings (pydantic-settings)
    database.py        # SQLAlchemy engine/session, get_db dependency
    redis_client.py     # Redis client factory
    celery_app.py       # Celery app + trivial health-check task
    logging.py          # JSON structured logging
    exceptions.py        # AppError hierarchy + exception handlers
    correlation.py        # X-Request-ID middleware
  db/base.py            # SQLAlchemy declarative Base (no tables yet)
alembic/                 # migration environment, empty baseline revision
tests/                    # pytest suite
docker/Dockerfile          # shared image for api & worker
docker-compose.yml          # api, postgres, redis, worker
```
