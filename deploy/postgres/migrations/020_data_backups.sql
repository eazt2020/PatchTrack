-- In-app data archive requests and backup-worker state.
--
-- The application queues a request here; a dedicated worker container (which
-- already ships with the bundled PostgreSQL image and therefore has the
-- matching pg_dump) turns it into a self-describing archive on the `backups`
-- volume. The queue is in the database so an on-demand request survives a
-- restart and the scheduled job and the button share one code path.
--
-- A partial unique index allows at most one queued or running archive. That
-- keeps repeated clicks and a scheduled run from producing duplicate dumps
-- while one is already in progress.

CREATE TABLE IF NOT EXISTS shared.backup_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    trigger_kind    text NOT NULL CHECK (trigger_kind IN ('manual', 'schedule')),
    requested_by    text NOT NULL DEFAULT '',
    requested_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    filename        text,
    size_bytes      bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256          text,
    app_version     text NOT NULL DEFAULT '',
    schema_version  text NOT NULL DEFAULT '',
    report_files    integer CHECK (report_files IS NULL OR report_files >= 0),
    message         text NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS backup_runs_one_active_idx
    ON shared.backup_runs ((true))
    WHERE status IN ('queued', 'running');

CREATE UNIQUE INDEX IF NOT EXISTS backup_runs_filename_idx
    ON shared.backup_runs (filename)
    WHERE filename IS NOT NULL;

CREATE INDEX IF NOT EXISTS backup_runs_requested_idx
    ON shared.backup_runs (requested_at DESC);

-- One row, rewritten by the worker every few seconds. The screen uses it to
-- distinguish "waiting for the worker" from "the worker is not running", so a
-- broken sidecar cannot silently look like a slow backup.
CREATE TABLE IF NOT EXISTS shared.backup_worker (
    singleton      boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    worker_version text NOT NULL DEFAULT ''
);
