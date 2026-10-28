-- PatchTrack 012: configurable retention for the audit trail.
--
-- The trail stays append-only: no screen or API can edit or delete an
-- individual event, and UPDATE is refused unconditionally. Retention is the
-- one sanctioned deletion, and it happens only inside
-- shared.purge_audit_events(), which reads the administrator-configured
-- window from shared.app_settings and deletes rows older than the cutoff.
--
-- The trigger cannot simply be relaxed: it is the guard that stops a
-- compromised application role from rewriting history. The purge function
-- marks its own transaction with a private GUC, so a delete is accepted only
-- while that function is running:
--
--     SELECT shared.purge_audit_events();
--
-- The function is SECURITY DEFINER because the service role does not own the
-- table, and it takes no cutoff argument: callers cannot ask it to delete
-- more than the configured policy allows.

CREATE OR REPLACE FUNCTION shared.audit_events_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Retention purge: the only DELETE that is allowed through. The flag is
    -- transaction-local and is set by shared.purge_audit_events() below.
    IF TG_OP = 'DELETE'
       AND coalesce(current_setting('patchtrack.audit_purge', true), '') = 'on'
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'shared.audit_events is append-only';
END;
$$;

CREATE OR REPLACE FUNCTION shared.purge_audit_events()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    retention_days integer;
    cutoff         timestamptz;
    removed        bigint;
BEGIN
    SELECT (value ->> 'days')::integer
      INTO retention_days
      FROM shared.app_settings
     WHERE key = 'audit_retention_days';

    -- Absent means "never configured": keep the seven-year default rather
    -- than surprising an installation by deleting anything.
    retention_days := coalesce(retention_days, 2555);

    -- Enforced here, not only in the API: the setting is a database row and
    -- must be safe even if it was written by another path.
    IF retention_days < 30 OR retention_days > 36500 THEN
        RAISE EXCEPTION
            'audit retention must be between 30 and 36500 days (got %)',
            retention_days;
    END IF;

    cutoff := now() - make_interval(days => retention_days);

    PERFORM set_config('patchtrack.audit_purge', 'on', true);
    DELETE FROM shared.audit_events WHERE occurred_at < cutoff;
    GET DIAGNOSTICS removed = ROW_COUNT;
    PERFORM set_config('patchtrack.audit_purge', '', true);

    RETURN jsonb_build_object(
        'purged', removed,
        'retention_days', retention_days,
        'cutoff', cutoff
    );
END;
$$;

COMMENT ON FUNCTION shared.purge_audit_events() IS
    'Delete audit events older than shared.app_settings.audit_retention_days; '
    'the only sanctioned DELETE on the append-only trail.';
