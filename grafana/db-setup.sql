-- Creates the read-only role Grafana uses for its Postgres panels. Run by the
-- grafana-db-setup service after the gateway has applied its migrations, so
-- the tables exist. Safe to run again: it only (re)sets the password and the
-- grants. Run with: psql -v pw=<password> -f db-setup.sql
SELECT format('CREATE ROLE grafana_ro LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') \gexec
SELECT format('ALTER ROLE grafana_ro PASSWORD %L', :'pw') \gexec
ALTER ROLE grafana_ro SET default_transaction_read_only = on;
-- Anonymous viewers can send their own queries through Grafana, so cap what
-- one can cost the database the gateway writes to.
ALTER ROLE grafana_ro SET statement_timeout = '5s';
ALTER ROLE grafana_ro CONNECTION LIMIT 5;
SELECT format('GRANT CONNECT ON DATABASE %I TO grafana_ro', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON requests, jev_assessments, interventions, model_releases TO grafana_ro;
