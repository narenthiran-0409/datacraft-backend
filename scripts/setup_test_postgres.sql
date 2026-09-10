-- Run once, as the postgres superuser, to provision this project's TEST
-- database — genuinely separate from the dataquality dev database
-- scripts/setup_local_postgres.sql provisions. Does not touch any other
-- database/role on this instance, and does not touch dataquality itself.
--
-- Why a separate script instead of granting dq_user CREATEDB: least
-- privilege — the app's normal runtime role has no business being able to
-- create arbitrary databases. dq_user only needs to be the OWNER of this
-- one additional database, which this script (run as superuser) grants
-- directly.
--
-- Usage (from "C:\Program Files\PostgreSQL\18\bin"):
--   psql -U postgres -h localhost -f scripts/setup_test_postgres.sql
--
-- Assumes dq_user already exists (created by setup_local_postgres.sql).
-- tests/conftest.py runs migrations against this database automatically
-- at the start of every test session — no manual `alembic upgrade head`
-- step needed here, unlike the dev database.

CREATE DATABASE dataquality_test OWNER dq_user;
GRANT ALL PRIVILEGES ON DATABASE dataquality_test TO dq_user;
