-- PatchTrack 009: configurable environment and business-service vocabulary.
--
-- Environment and business service are free text in AssetExplorer. Operatives
-- correcting a placement by hand therefore had to retype the value, and a typo
-- ("Production " / "Prodution") would silently split every report and filter.
--
-- This table is the administrator-managed list offered by the placement
-- editor. It is a picker, not a foreign key: renaming or deleting an option
-- never rewrites an asset or an override, and the AssetExplorer sync neither
-- reads nor writes it. Existing values are seeded so an upgraded installation
-- starts from the vocabulary it already uses; no value is invented.

CREATE TABLE IF NOT EXISTS inventory.placement_options (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       text NOT NULL
                 CHECK (kind IN ('environment', 'business_group')),
    value      text NOT NULL
                 CHECK (btrim(value) <> '' AND length(value) <= 128),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    updated_at timestamptz,
    updated_by text
);

-- One entry per value per kind, compared case-insensitively and ignoring
-- surrounding whitespace: "production", "Production" and " Production " are
-- the same list entry and must not all appear in the picker.
CREATE UNIQUE INDEX IF NOT EXISTS placement_options_kind_value_key
    ON inventory.placement_options (kind, lower(btrim(value)));

COMMENT ON TABLE inventory.placement_options IS
    'Administrator-managed picker values for environment and business group. '
    'Suggestions only: no asset or override is changed by editing this list.';

INSERT INTO inventory.placement_options (kind, value, created_by)
SELECT 'environment', v, 'migration'
FROM (
    SELECT DISTINCT btrim(environment) AS v
    FROM inventory.assets
    WHERE environment IS NOT NULL AND btrim(environment) <> ''
) AS existing
ON CONFLICT DO NOTHING;

INSERT INTO inventory.placement_options (kind, value, created_by)
SELECT 'business_group', v, 'migration'
FROM (
    SELECT DISTINCT btrim(business_group) AS v
    FROM inventory.assets
    WHERE business_group IS NOT NULL AND btrim(business_group) <> ''
) AS existing
ON CONFLICT DO NOTHING;
