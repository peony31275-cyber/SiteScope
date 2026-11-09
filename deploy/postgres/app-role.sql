-- The application's PostgreSQL role (Phase 11; ADR-0011 asks for it, ADR-0025 wires it in).
--
-- The API, the scheduler and the workers connect as sitescope_app. It reads and writes rows and
-- nothing else. It is not a superuser: a superuser could disable the fencing triggers
-- (session_replication_role), read the server's files or run programs (COPY ... PROGRAM). It is
-- not an owner, so it can run no DDL. The migrations, backups and restores connect as the
-- database owner (POSTGRES_USER of the postgres service).
--
-- Run by the one-shot "db-roles" service (deploy/compose.yaml) after every migration step,
-- connected as the owner. Idempotent: it creates the role once, then sets its password and
-- grants again, so that tables added by later migrations are covered. The password comes from
-- the environment variable SITESCOPE_APP_PASSWORD through \getenv, never from the command line.

\set ON_ERROR_STOP on
\getenv app_password SITESCOPE_APP_PASSWORD
\if :{?app_password}
\else
  \set app_password ''
\endif
-- A missing or empty password is an error, so the step fails (psql exits 3) and nothing starts.
SELECT length(:'app_password') = 0 AS no_password \gset
\if :no_password
  DO $$ BEGIN RAISE EXCEPTION 'SITESCOPE_APP_PASSWORD is not set'; END $$;
\endif

SELECT 'CREATE ROLE sitescope_app'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sitescope_app') \gexec

ALTER ROLE sitescope_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'app_password';

-- Only the owner and the application may connect; nobody else may create objects.
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO sitescope_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sitescope_app;

-- Rows of every table, now and after later migrations; not the migration history.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sitescope_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sitescope_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sitescope_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO sitescope_app;
REVOKE ALL ON TABLE alembic_version FROM sitescope_app;

SELECT rolname, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'sitescope_app';
