-- PatchTrack 023: allow the Executive Board Pack report type.
--
-- The board pack is generated from the latest recorded daily snapshot through
-- the same reporting pipeline as every other report, so it inherits retention,
-- audit and delivery. Dropping and re-adding the named check is deliberate:
-- PostgreSQL cannot alter a CHECK constraint in place.

ALTER TABLE reporting.reports
    DROP CONSTRAINT IF EXISTS reports_report_type_check;

ALTER TABLE reporting.reports
    ADD CONSTRAINT reports_report_type_check CHECK (report_type IN (
        'monthly_management',
        'quarterly_compliance',
        'inventory_register',
        'board_pack'
    ));
