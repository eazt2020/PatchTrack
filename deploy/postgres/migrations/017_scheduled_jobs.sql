-- Durable state for the in-app scheduler.
--
-- Every service runs its own scheduler loop inside the application process;
-- the systemd timer and its shell runners are no longer used. The due time is
-- persisted so restarting a service does not turn every long-interval job
-- into a catch-up burst, and a failed job is retried sooner than its normal
-- interval. A PostgreSQL advisory lock (taken by the runner, not stored here)
-- keeps two replicas from executing the same job at once.

CREATE TABLE IF NOT EXISTS shared.scheduled_jobs (
    name             text PRIMARY KEY,
    interval_seconds integer NOT NULL CHECK (interval_seconds >= 1),
    next_run_at      timestamptz,
    last_started_at  timestamptz,
    last_finished_at timestamptz,
    last_status      text CHECK (last_status IN ('success', 'failure')),
    last_message     text NOT NULL DEFAULT '',
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scheduled_jobs_due_idx
    ON shared.scheduled_jobs (next_run_at);
