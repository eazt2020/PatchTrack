-- Indexes for queries on the scheduler's hot path.
--
-- The in-app scheduler made two of these noticeably more frequent: the report
-- tick reads reporting.schedules once a minute, and the daily retention sweep
-- scans reporting.reports for expired files. The rest cover orderings that
-- already existed but had no supporting index.

-- The report tick asks for enabled schedules whose next run has arrived.
CREATE INDEX IF NOT EXISTS schedules_due_idx
    ON reporting.schedules (next_run_at)
    WHERE enabled;

-- Retention selects completed reports whose expiry has passed.
CREATE INDEX IF NOT EXISTS reports_expiry_idx
    ON reporting.reports (expires_at)
    WHERE status = 'completed' AND expires_at IS NOT NULL;

-- The reports list is ordered newest-first without filtering by requester.
CREATE INDEX IF NOT EXISTS reports_requested_at_idx
    ON reporting.reports (requested_at DESC);

-- Sync-run listings without an integration filter, and the latest-run lookup.
CREATE INDEX IF NOT EXISTS sync_runs_started_idx
    ON inventory.sync_runs (started_at DESC);

-- Snapshot lookup by period only (the existing index leads with scope).
CREATE INDEX IF NOT EXISTS snapshots_period_start_idx
    ON compliance.snapshots (period_start DESC, captured_at DESC);
