-- Restores from an archive uploaded by an operator.
--
-- An archive downloaded earlier can be uploaded and restored on the same host
-- without shell access. Uploads are staged in their own volume, not the backups
-- volume, so retention never treats an uploaded file as an archive PatchTrack
-- produced and the reporting service needs no write access to the archives.
--
-- `backup_id` is nullable because an uploaded archive has no row in
-- shared.backup_runs. `sha256` is the digest of the uploaded bytes, computed
-- while the file was written, and `display_name` keeps the operator's filename
-- apart from the generated name on disk.

ALTER TABLE shared.restore_runs
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'archive';
ALTER TABLE shared.restore_runs
    ADD COLUMN IF NOT EXISTS sha256 text NOT NULL DEFAULT '';
ALTER TABLE shared.restore_runs
    ADD COLUMN IF NOT EXISTS display_name text NOT NULL DEFAULT '';

ALTER TABLE shared.restore_runs
    ALTER COLUMN backup_id DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'shared.restore_runs'::regclass
           AND conname = 'restore_runs_source_check'
    ) THEN
        ALTER TABLE shared.restore_runs
            ADD CONSTRAINT restore_runs_source_check
            CHECK (source IN ('archive', 'upload'));
    END IF;
END $$;
