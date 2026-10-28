-- One deployment-wide timezone drives report periods, schedules and display.
--
-- Instants remain UTC; this setting decides which local calendar day, month or
-- quarter an instant belongs to. Storing it here means every service reads the
-- same value and a change applies without a restart.
INSERT INTO shared.app_settings (key, value, updated_by)
VALUES ('system_timezone', '{"name": "UTC"}'::jsonb, 'migration')
ON CONFLICT (key) DO NOTHING;
