-- PatchTrack 006: the four authoritative source integrations.
--
-- Rows are created DISABLED with no endpoint and no secret. There is no
-- default credential: an administrator must configure each source explicitly
-- before any collection can run.

INSERT INTO inventory.integrations (key, display_name, kind, enabled)
VALUES
    ('assetexplorer', 'ManagedEngine AssetExplorer', 'inventory', false),
    ('satellite', 'Red Hat Satellite', 'patch', false),
    ('sccm', 'Microsoft Configuration Manager', 'patch', false),
    ('solaris', 'Solaris patch collection (approved read-only)', 'patch', false)
ON CONFLICT (key) DO NOTHING;
