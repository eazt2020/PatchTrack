-- PatchTrack 001: schemas, migration ledger and cross-cutting audit trail.
-- Each microservice owns its own PostgreSQL schema. Services never write to
-- another service's schema; they read through the owning service's API or the
-- Grafana read-only role.

CREATE SCHEMA IF NOT EXISTS shared;
CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS inventory;
CREATE SCHEMA IF NOT EXISTS compliance;
CREATE SCHEMA IF NOT EXISTS reporting;

CREATE TABLE IF NOT EXISTS shared.schema_migrations (
    version      text PRIMARY KEY,
    description  text NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    checksum     text NOT NULL
);

-- Audit events are append-only. Triggers block UPDATE and DELETE so that a
-- compromised application role cannot rewrite history.
CREATE TABLE IF NOT EXISTS shared.audit_events (
    id           bigserial PRIMARY KEY,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    actor        text NOT NULL,
    actor_role   text NOT NULL,
    event_type   text NOT NULL,
    target_type  text NOT NULL DEFAULT '',
    target_id    text NOT NULL DEFAULT '',
    outcome      text NOT NULL CHECK (outcome IN ('success', 'failure', 'denied')),
    context      jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_ip    inet,
    service      text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS audit_events_occurred_idx
    ON shared.audit_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_actor_idx
    ON shared.audit_events (actor, occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_type_idx
    ON shared.audit_events (event_type, occurred_at DESC);

CREATE OR REPLACE FUNCTION shared.audit_events_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'shared.audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_no_update ON shared.audit_events;
CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE OR DELETE ON shared.audit_events
    FOR EACH ROW EXECUTE FUNCTION shared.audit_events_immutable();

-- Application settings that must survive restarts (timezone, branding,
-- retention, report defaults). Secrets are NOT stored here.
CREATE TABLE IF NOT EXISTS shared.app_settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL DEFAULT 'system'
);
