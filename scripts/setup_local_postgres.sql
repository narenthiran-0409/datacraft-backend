-- Run once, as the postgres superuser, to provision this project's own
-- database and role. Does not touch any other database/role on this instance.
--
-- Usage (from "C:\Program Files\PostgreSQL\18\bin"):
--   psql -U postgres -h localhost -f scripts/setup_local_postgres.sql
--
-- Change the password below before running if you don't want the default.

CREATE ROLE dq_user WITH LOGIN PASSWORD 'dq_password';
CREATE DATABASE dataquality OWNER dq_user;
GRANT ALL PRIVILEGES ON DATABASE dataquality TO dq_user;
