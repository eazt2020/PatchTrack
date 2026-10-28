-- PatchTrack 005: generated reports, schedules, delivery attempts and
-- report file retention metadata.

CREATE TABLE IF NOT EXISTS reporting.reports (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_type    text NOT NULL CHECK (report_type IN
                     ('monthly_management', 'quarterly_compliance', 'inventory_register')),
    period_start   date NOT NULL,
    period_end     date NOT NULL,
    filters        jsonb NOT NULL DEFAULT '{}'::jsonb,
    snapshot_id    uuid REFERENCES compliance.snapshots(id),
    status         text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'generating', 'completed', 'failed')),
    requested_by   text NOT NULL,
    requested_at   timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,
    completed_at   timestamptz,
    filename       text NOT NULL DEFAULT '',
    file_path      text NOT NULL DEFAULT '',
    sha256         text NOT NULL DEFAULT '',
    file_size      bigint NOT NULL DEFAULT 0,
    page_count     integer NOT NULL DEFAULT 0,
    error_summary  text NOT NULL DEFAULT '',
    schedule_id    uuid,
    expires_at     timestamptz
);

CREATE INDEX IF NOT EXISTS reports_requested_idx
    ON reporting.reports (requested_at DESC);
CREATE INDEX IF NOT EXISTS reports_period_idx
    ON reporting.reports (report_type, period_start DESC);

CREATE TABLE IF NOT EXISTS reporting.schedules (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    report_type     text NOT NULL CHECK (report_type IN
                      ('monthly_management', 'quarterly_compliance')),
    cadence         text NOT NULL CHECK (cadence IN ('monthly', 'quarterly')),
    day_of_month    integer NOT NULL DEFAULT 1 CHECK (day_of_month BETWEEN 1 AND 28),
    run_hour        integer NOT NULL DEFAULT 6 CHECK (run_hour BETWEEN 0 AND 23),
    timezone        text NOT NULL DEFAULT 'UTC',
    recipients      text[] NOT NULL DEFAULT '{}',
    filters         jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled         boolean NOT NULL DEFAULT true,
    last_run_at     timestamptz,
    last_run_status text NOT NULL DEFAULT '',
    next_run_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL DEFAULT 'system',
    updated_at      timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE reporting.reports
    DROP CONSTRAINT IF EXISTS reports_schedule_fk;
ALTER TABLE reporting.reports
    ADD CONSTRAINT reports_schedule_fk
    FOREIGN KEY (schedule_id) REFERENCES reporting.schedules(id) ON DELETE SET NULL;

-- Delivery outcome is tracked independently of generation success.
CREATE TABLE IF NOT EXISTS reporting.delivery_attempts (
    id          bigserial PRIMARY KEY,
    report_id   uuid NOT NULL REFERENCES reporting.reports(id) ON DELETE CASCADE,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    recipients  text[] NOT NULL DEFAULT '{}',
    status      text NOT NULL CHECK (status IN ('sent', 'failed', 'skipped')),
    detail      text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS delivery_attempts_report_idx
    ON reporting.delivery_attempts (report_id, attempted_at DESC);

-- Guards against duplicate scheduled execution if two scheduler instances
-- race, and against re-running a period that already produced a report.
CREATE UNIQUE INDEX IF NOT EXISTS reports_scheduled_unique_idx
    ON reporting.reports (schedule_id, report_type, period_start)
    WHERE schedule_id IS NOT NULL AND status <> 'failed';

-- Idempotency ledger for scheduled jobs.
CREATE TABLE IF NOT EXISTS reporting.job_executions (
    id           bigserial PRIMARY KEY,
    schedule_id  uuid NOT NULL REFERENCES reporting.schedules(id) ON DELETE CASCADE,
    period_start date NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running',
    message      text NOT NULL DEFAULT '',
    UNIQUE (schedule_id, period_start)
);
