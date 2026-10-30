-- In-app restore requests.
--
-- Restoring is destructive: the archive's database replaces the live one. The
-- request is queued here and performed by the backup worker, which is the only
-- container with pg_restore matching the server major version. A partial unique
-- index allows one restore at a time, matching the single backup worker.
--
-- There is deliberately no foreign key to shared.backup_runs: an archive's
-- backup_runs data is excluded from dumps, so the row need not exist after a
-- restore. The filename is copied here when the request is made.

CREATE TABLE IF NOT EXISTS shared.restore_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    backup_id       uuid NOT NULL,
    filename        text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    requested_by    text NOT NULL DEFAULT '',
    requested_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    message         text NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS restore_runs_one_active_idx
    ON shared.restore_runs ((true))
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS restore_runs_requested_idx
    ON shared.restore_runs (requested_at DESC);
