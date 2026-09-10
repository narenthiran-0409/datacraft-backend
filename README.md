# dataquality-platform

> **Phase 3** of a multi-phase build. Phase 1 (foundation) and Phase 2
> (identity/RBAC, data sources, connections, credential vault stub, source
> adapters with PostgreSQL connectivity) are done. Phase 3 adds Schema
> Discovery: a `jobs` table + generic job lifecycle, `schemas`/`datasets`/
> `columns`/`dataset_key_columns` metadata tables, real catalog-introspection
> implementations for all five source adapters (PostgreSQL live-verified;
> SQL Server/MySQL/Oracle/SAP HANA mock-only — see below), an async Celery
> discovery task with per-dataset fault isolation, and read/manual-key-config
> endpoints over the discovered metadata. Profiling, Rule Engine, Validation,
> Review, Corrections, Approval, Staging, Publishing, Lineage, AI, Reports,
> and the frontend are **not** implemented yet — those are later phases.

## What's here

- `api` — FastAPI app (uvicorn), mounts `/api/v1`, exposes `/health` and
  `/readyz`.
- `postgres` — PostgreSQL, the application's database (46-table Database
  Design v2; Phase 2 uses 9 of those tables).
- `redis` — Redis-protocol store, used as the Celery broker/result backend,
  refresh-token store, and (locally) the credential vault backing store.
- `worker` — Celery worker; still only runs the Phase 1 trivial health-check
  task.

## Run locally

This phase was developed and verified against a local Python venv plus a
local PostgreSQL and local Redis-compatible server (Memurai on Windows),
rather than the full Docker Compose stack (see Phase 1 README section for
Compose; it remains valid but wasn't re-verified this phase).

```bash
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp .env.example .env     # then edit DATABASE_URL / REDIS_URL / JWT_SECRET_KEY / VAULT_LOCAL_ENCRYPTION_KEY as needed
```

### PostgreSQL

Provision a dedicated database and role once (`scripts/setup_local_postgres.sql`
does this against a local instance without touching any other database):

```
psql -U postgres -h localhost -f scripts/setup_local_postgres.sql
```

### Redis

Any Redis-protocol-compatible server on `REDIS_URL` works locally (Redis
itself, or Memurai on Windows). No special configuration needed beyond a
reachable instance.

## Run migrations

```bash
alembic upgrade head
```

This applies, in order: the Phase 1 empty baseline, Phase 2's
`0002_create_identity_tables`, `0003_create_connection_tables`,
`0004_create_audit_events_table`, `0005_seed_connection_types`,
`0006_seed_roles_and_permissions`, then Phase 3's
`0007_phase3_discovery_foundation` (creates `jobs`, `schemas`, `datasets`,
`columns`, `dataset_key_columns`; adds `DISCOVERY_RUN` to `jobs.job_type`'s
CHECK constraint and `columns.is_active`; seeds `discovery.run`,
`metadata.read`, `metadata.manage` and links them into every role per the
frozen role hierarchy). The seed migrations are data-only — see their
docstrings for the exact seeded rows and matrix rationale.

## Create the first administrator user

Not part of any migration — run manually, once, after migrations:

```bash
python scripts/create_admin_user.py --email admin@example.com --password 'Str0ng!Passw0rd'
```

Omit `--password` to have one generated and printed once. The script
requires the `administrator` role to already exist (seeded by migration
`0006`).

## Run tests

**One-time setup**, in addition to the dev database above — tests run
against a completely separate `dataquality_test` database, never the dev
one (see "Test database isolation" below). Provision it once:

```
psql -U postgres -h localhost -f scripts/setup_test_postgres.sql
```

Then:

```bash
pytest
```

- `tests/unit/` — password hashing, JWT encode/decode/expiry, permission
  resolution, credential-vault round-trip. No external services required
  (Redis is faked with `fakeredis` here).
- `tests/integration/` — real Postgres, migrations applied fresh: user/
  connection/data-source CRUD, seeded roles/permissions, uniqueness
  constraints, audit rows.
- `tests/contract/` — every Phase 2 route against a token missing the
  required permission (403) and against no token (401).
- `tests/e2e/` — full flow: seed admin, log in, create data source, create
  connection against a real local Postgres, test it (expects `HEALTHY`),
  refresh, log out, confirm the used refresh token is rejected.

Integration/contract/e2e tests require a real reachable Postgres and Redis
(see above) — they are not mocked, since proving real connectivity and
constraint enforcement is the point.

### Test database isolation

`tests/conftest.py` force-overrides `DATABASE_URL` to `<dev database
name>_test` (so `dataquality` → `dataquality_test`) **before any
application module is imported** — this has to happen that early because
`app/core/database.py`'s `engine` is built from `settings.DATABASE_URL` at
first import, and `app/core/config.py`'s `settings` is `@lru_cache`d, so
overriding the environment variable any later would have no effect. This
override is unconditional: it doesn't matter what a developer's `.env`
happens to say, because tests never read `DATABASE_URL` for their own
connection at all — only to derive the `_test` name from it. A
session-scoped fixture also hard-refuses to run at all if, for any reason,
the resolved database name doesn't end in `_test`.

This isolation exists because it was missing before, and the consequences
were real: the shared dev `dataquality` database was repeatedly damaged by
test runs — a truncated `users` table mid-session, a Postgres deadlock
from an orphaned test connection, and eventually a full wipe down to only
migration-seeded reference data. The `db` fixture (`tests/conftest.py`)
`TRUNCATE`s ~30 core tables at the start of every test that uses it — a
deliberate, necessary part of test isolation *between tests*, which is
exactly what made it so damaging when it ran against the real dev database
instead of a disposable one.

The test database is never auto-created (that would need `CREATEDB`
granted to `dq_user`, which this project deliberately doesn't do — see
`scripts/setup_test_postgres.sql`'s own comment on why). It *is*
auto-migrated to head at the start of every test session, so there's no
separate `alembic upgrade head` step to remember for the test database
the way there is for the dev one.

## Authentication & RBAC

- Access tokens: JWT, HS256, ~15 minute expiry (`ACCESS_TOKEN_EXPIRE_MINUTES`).
  Payload is `{sub, iat, exp, jti, token_type}` only — no roles/permissions
  embedded; every permission check re-resolves `user_roles → role_permissions
  → permissions` from the DB per request (`app/core/dependencies.py`).
- Refresh tokens: not a Postgres table. Stored in Redis as
  `refresh_token:{jti} -> {user_id, expires_at}` (TTL'd) plus
  `user_refresh_tokens:{user_id} -> {jti, ...}` so a password change can
  revoke every outstanding refresh token for a user at once
  (`app/modules/auth/refresh_tokens.py`). Refresh is rotate-on-use: each
  `/api/v1/auth/refresh` call consumes the presented `jti` and issues a new
  pair; reusing an already-consumed or unknown `jti` is rejected with 401.
- RBAC: `require_permission(code)` (`app/core/dependencies.py`) resolves
  permissions fresh from the DB and writes a `permission.denied` audit row
  on every denial.

## Credential vault — local-dev-only stub

`app/modules/connections/credential_vault.py`'s `LocalRedisVaultClient`
Fernet-encrypts connection credentials and stores them in Redis under
`vault:credential:{uuid}`, keyed only by an opaque `credential_ref` that's
all the `connections` table ever stores. **This is not production-grade**:
the encryption key lives in application config (`VAULT_LOCAL_ENCRYPTION_KEY`),
not a separate KMS, and a Redis flush/restart without persistence enabled
loses every stored credential. Replace with a real secrets manager (AWS
Secrets Manager, HashiCorp Vault, Azure Key Vault, ...) before any
non-local deployment.

## Source adapters

`app/source_adapters/` abstracts source database connectivity behind
`SourceDatabaseProvider`: `test_connection`, `get_capabilities`,
`list_schemas`, `list_datasets`, `get_columns`, `get_primary_keys`,
`get_foreign_keys`, `get_row_count`, and `sample_rows` are implemented for
all five vendors (`sample_rows` most recently — added for Data Preview,
see below). `fetch_rows_by_keys` is implemented for PostgreSQL only
(Staging's batched re-fetch); the other four remain `NotImplementedError`
— out of scope until Staging needs them for a non-Postgres source. Every
method is wrapped in a bounded timeout
(`DISCOVERY_QUERY_TIMEOUT_SECONDS`, `app/source_adapters/timeout.py`).
`get_row_count` always uses a cheap catalog-statistics estimate (Postgres
`pg_class.reltuples`, SQL Server `sys.partitions.rows`, MySQL
`information_schema.tables.table_rows`, Oracle `all_tables.num_rows`, HANA
`sys.m_tables.record_count`) — **never** a live `COUNT(*)`.
`get_primary_keys` is the single source of truth for both single-column
and composite PK detection, returned in the source's reported key order.

**Verification status — read this before trusting any of these providers
in production:**

| Provider | Driver | Verification |
|---|---|---|
| `PostgreSQLProvider` | `psycopg` (already in use since Phase 1/2) | **Live-verified** against a real local Postgres instance (integration tests + manual verification), including `sample_rows` |
| `SQLServerProvider` | `pyodbc` | **Mock-only** — no live SQL Server instance available in this environment |
| `MySQLProvider` | `pymysql` | **Mock-only** — no live MySQL instance available in this environment |
| `OracleProvider` | `python-oracledb` (thin mode — no Oracle Instant Client needed) | **Mock-only** — no live Oracle instance available in this environment |
| `SAPHanaProvider` | `hdbcli` | **Mock-only** — no live SAP HANA instance available in this environment |

The four mock-only providers are unit-tested against mocked cursor/driver
responses (`tests/unit/test_{sqlserver,mysql,oracle,saphana}_provider.py`)
to verify SQL correctness of intent and error-translation logic, but that
is not the same as integration verification against a real server. Treat
them as implemented-but-unverified until tested against a real instance.

Connection testing (`POST /connections/{id}/test`) remains synchronous by
design (bounded timeout, no Celery job, no jobs-table row) — unchanged
from Phase 2. Discovery (`POST /connections/{id}/discover`) is
asynchronous: it creates a `jobs` row and runs on a Celery worker.

## Schema Discovery

`POST /api/v1/connections/{id}/discover` (permission `discovery.run`)
enqueues a `DISCOVERY_RUN` job (409 `DiscoveryAlreadyRunningError` if one
is already `QUEUED`/`RUNNING` for that connection) and returns `202
{job_id, ...}`. Poll `GET /api/v1/jobs/{id}` for status; `POST
/api/v1/jobs/{id}/cancel` requests cooperative cancellation (checked
between schemas/datasets, via a Redis flag) — a still-`QUEUED` job is
cancelled immediately, a `RUNNING` one stops at its next checkpoint.

The Celery task (`app.modules.discovery.tasks.run_discovery`):
1. Resolves the connection's credential and lists schemas. A failure here
   (auth/timeout/unreachable/SSL/vault error) is **terminal**: the job is
   marked `FAILED` with a categorized message, a `discovery.failed` audit
   event is written, and nothing partial is committed.
2. For each schema, for each dataset: discovers columns, primary keys
   (single- **and** composite-key, from `get_primary_keys()`'s reported
   order), a row-count estimate, and (if the provider supports it) foreign
   keys — each dataset in its own transaction. A per-dataset failure
   (`SourceTimeoutError`/`SourceQueryError`) is caught, rolled back,
   audited as `discovery.dataset_failed` (`entity_type='CONNECTION'` —
   there's no dataset row to reference if it failed before being created),
   and the run continues to the next dataset.
3. After every schema/dataset is processed, a deactivation sweep sets
   `is_active=false` (never a hard delete) on any previously-active
   schema/dataset/column not seen this run; anything that reappears in a
   later run is reactivated, not duplicated.
4. The job is marked `COMPLETED` (with an `error_message` summarizing
   "X/Y datasets discovered, Z failed" if any per-dataset failures
   occurred — `jobs.status` has no partial-success value) and a
   `discovery.completed` audit event is written, `actor_id=job.created_by`
   (not the current request context — this runs in an async task).

Foreign keys discovered per dataset go **only** into the
`discovery.completed`/`discovery.dataset_failed` audit events' `metadata`
JSONB — there is no foreign-key table/column in the frozen schema, and
none is exposed through any API endpoint this phase.

A stale-job watchdog (`jobs.sweep_stale_jobs`, Celery Beat, every 5
minutes) marks any `RUNNING` job `FAILED` if its `updated_at` — **not**
`started_at` — is older than `STALE_JOB_THRESHOLD_MINUTES`; comparing
against `started_at` would incorrectly kill large, legitimately
still-progressing runs. This watchdog's audit trail uses
`actor_type='SYSTEM', actor_id=NULL`.

### Reading discovered metadata

`GET /api/v1/schemas?connection_id=...`, `GET /api/v1/datasets`
(`schema_id`/`search`/`is_active`/`page` filters), `GET
/api/v1/datasets/{id}`, `GET /api/v1/datasets/{id}/columns` (permission
`metadata.read`) are plain, fast Postgres reads — **none of them ever
calls a source-database provider**, and `GET /datasets/{id}` never
includes foreign-key data.

`PUT /api/v1/datasets/{id}/key-columns` (permission `metadata.manage`)
manually configures the key for a dataset Discovery couldn't find a key
for (or got wrong) — rejects duplicate ordinals, a column not belonging to
the dataset, or an empty submission. It reuses the exact same
`apply_key_columns()` helper (`app/modules/datasets/key_resolution.py`)
that Discovery's automatic path uses, so both paths stay behaviorally
identical by construction. `PATCH /api/v1/datasets/{id}` (`{is_active:
bool}`, same permission) manually overrides the auto-managed
deactivation.

## Data Preview

`GET /api/v1/datasets/{id}/preview?row_count=N` (permission
`data_preview.read` — see below, deliberately **not** `metadata.read`)
runs a live, bounded, read-only `sample_rows()` query against the
dataset's actual source database — the one endpoint under `/datasets/`
that calls a source-database provider synchronously in the request path
(same pattern as `POST /connections/{id}/test`, no Celery job). `row_count`
defaults to 20 and is **silently clamped** to 100 regardless of what's
requested — a deliberate departure from Profiling's "never silently
downgrade, reject with 422 instead" philosophy, since a preview's exact
row count has no downstream statistical meaning the way a profiling
sample's does. String values in the returned rows are each truncated to
100 characters, mirroring Profiling's `top_values` truncation precedent
(`app/modules/profiling/engine.py`).

`data_preview.read` is a dedicated permission (migration 0018), not a
reuse of `metadata.read` — this endpoint returns actual raw row content
pulled live from the source, meaningfully more sensitive than the
column-name/type/count metadata `metadata.read` has ever gated. It's
granted to all five roles (a pure read, so it follows this project's
universal-read convention — see the migration's docstring for the full
reasoning), but that default is a real access-control decision worth
reviewing, not an assumption.

Every preview attempt — success or failure — writes an `audit_events` row
(`dataset.previewed` / `dataset.preview_failed`), **never** containing the
actual row values, only counts/schema/table names and, on failure, the
categorized error message. A live-source failure (unreachable/auth/SSL/
timeout/credential-vault/query-failed — e.g. the table was renamed or
dropped at the source since it was last discovered) is caught and mapped
to a controlled `502 PREVIEW_SOURCE_UNAVAILABLE` (or `504 PREVIEW_TIMEOUT`
for a timeout specifically), never a raw unhandled 500.

`sample_rows()` for the four non-Postgres providers was implemented as
part of this feature for structural completeness — matching the ABC
contract, unit-tested against mocked cursors — but remains **mock-only,
unverified against a live instance**, exactly like every other method on
those four providers. See "Source adapters" above.

## Configuration

All settings are environment variables read via `pydantic-settings`
(`app/core/config.py`). See `.env.example` for the full list and local
defaults, including the new JWT and vault-encryption-key settings added in
Phase 2.

## Project layout (Phase 3 additions in bold)

```
app/
  api/v1/
    auth/            login, refresh, logout, me, change-password
    users/            users + roles endpoints
    data_sources/      data source CRUD (deactivate, not hard delete)
    connections/        connection + connection-type CRUD, test-connection,
                          **POST /connections/{id}/discover**
    **jobs/**              GET /jobs/{id}, POST /jobs/{id}/cancel
    **datasets/**           GET /schemas, /datasets, /datasets/{id},
                          /datasets/{id}/columns, PUT .../key-columns,
                          PATCH /datasets/{id}
  core/
    security.py        password hashing (argon2id) + JWT encode/decode
    dependencies.py     get_current_user, require_permission (RBAC)
  db/
    mixins.py           UUID PK + timestamp mixins
    models/              User, Role, Permission, UserRole, RolePermission,
                          DataSource, ConnectionType, Connection, AuditEvent,
                          **Job, Schema, Dataset, Column, DatasetKeyColumn**
  modules/
    auth/                 login/refresh/logout orchestration, Redis refresh-token store
    users/                  user CRUD, role assignment, password reset
    audit/                   AuditingService.record(...)
    data_sources/             CRUD + active-connections delete guard
    connections/               CRUD, test-connection orchestration, credential vault
    **discovery/**               DiscoveryService (upsert_schema/upsert_dataset/
                              deactivate_missing), Celery task run_discovery
    **jobs/**                     JobsService (generic lifecycle), Celery Beat
                              sweep_stale_jobs
    **datasets/**                 DatasetService (read-only), DatasetKeyService
                              (manual key config), **key_resolution.py**
                              (shared helper used by both Discovery and
                              DatasetKeyService)
  source_adapters/            SourceDatabaseProvider ABC (8 methods),
                          ProviderCapabilities, **timeout.py**, all 5
                          providers implemented (PostgreSQL live-verified;
                          4 others mock-only — see Source adapters above)
alembic/versions/              0002-0006 (Phase 2), **0007_phase3_discovery_foundation**
scripts/
  setup_local_postgres.sql     one-time local DB/role provisioning
  create_admin_user.py          manual first-administrator bootstrap
tests/{unit,integration,contract,e2e}/
```
