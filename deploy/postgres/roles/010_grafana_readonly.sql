-- Grafana read-only role.
--
-- Grafana queries the same tables the services write, so its numbers agree
-- with the API and the reports by construction. It must never be able to
-- change anything: the dashboard is a view of the archive, not a second
-- management interface.
--
-- Run once as a superuser, after the migrations:
--
--   psql -v ON_ERROR_STOP=1 -v grafana_password="$GF_PASSWORD" \
--        -d patchtrack -f deploy/postgres/roles/010_grafana_readonly.sql
--
-- The password is supplied by the operator (generated at install time, stored
-- in the deployment's secret store). No default password ships with PatchTrack.

\set ON_ERROR_STOP on

SELECT set_config('patchtrack.grafana_password',
                  :'grafana_password', false);

DO $$
BEGIN
    IF coalesce(current_setting('patchtrack.grafana_password', true), '') = '' THEN
        RAISE EXCEPTION
            'grafana_password is empty — generate one with: openssl rand -base64 32';
    END IF;
    IF length(current_setting('patchtrack.grafana_password')) < 20 THEN
        RAISE EXCEPTION 'grafana_password must be at least 20 characters';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'patchtrack_grafana') THEN
        EXECUTE format('CREATE ROLE patchtrack_grafana LOGIN PASSWORD %L',
                       current_setting('patchtrack.grafana_password'));
    ELSE
        EXECUTE format('ALTER ROLE patchtrack_grafana PASSWORD %L',
                       current_setting('patchtrack.grafana_password'));
    END IF;
END
$$;

-- Explicitly not an administrator, and unable to create anything.
ALTER ROLE patchtrack_grafana NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE patchtrack_grafana SET default_transaction_read_only = on;
ALTER ROLE patchtrack_grafana SET statement_timeout = '30s';

REVOKE ALL ON SCHEMA shared, inventory, compliance, reporting FROM patchtrack_grafana;
REVOKE ALL ON ALL TABLES IN SCHEMA shared, inventory, compliance, reporting
    FROM patchtrack_grafana;

-- Read access to the schemas the dashboard queries. USAGE alone is not enough;
-- SELECT is granted per table below so a future table is not exposed by default.
GRANT USAGE ON SCHEMA shared, inventory, compliance, reporting TO patchtrack_grafana;

GRANT SELECT ON
    inventory.assets,
    inventory.asset_effective,
    inventory.integrations,
    inventory.sync_runs,
    inventory.reconciliation_conflicts,
    compliance.asset_compliance,
    compliance.asset_latest_assessment,
    compliance.assessments,
    compliance.findings,
    compliance.snapshots,
    compliance.snapshot_groups,
    compliance.policies,
    compliance.policy_versions,
    reporting.reports,
    reporting.schedules,
    reporting.delivery_attempts,
    reporting.job_executions,
    shared.app_settings
TO patchtrack_grafana;

-- The audit trail is deliberately readable: auditors need the dashboard's
-- "when did we last collect" answer to be verifiable. It contains no secrets
-- by design (the middleware strips them before insert).
GRANT SELECT ON shared.audit_events TO patchtrack_grafana;

-- Identity tables are NOT granted. Users, password hashes, refresh tokens and
-- login attempts have no place on a compliance dashboard.
