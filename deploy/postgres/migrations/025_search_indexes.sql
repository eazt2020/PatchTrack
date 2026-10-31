-- Trigram indexes for the register's substring search.
--
-- `/assets?search=` matches with leading-wildcard ILIKE, which no btree index
-- can serve. These cover the identity and ownership columns users actually
-- type into the box; the placement columns are resolved through the
-- asset_effective view's COALESCE/alias joins and cannot use a base-table
-- trigram index. The extension is cheap to enable and indexes only text.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS assets_hostname_trgm_idx
    ON inventory.assets USING gin (hostname gin_trgm_ops);

CREATE INDEX IF NOT EXISTS assets_fqdn_trgm_idx
    ON inventory.assets USING gin (fqdn gin_trgm_ops);

CREATE INDEX IF NOT EXISTS assets_asset_owner_trgm_idx
    ON inventory.assets USING gin (asset_owner gin_trgm_ops);

CREATE INDEX IF NOT EXISTS assets_department_trgm_idx
    ON inventory.assets USING gin (department gin_trgm_ops);

CREATE INDEX IF NOT EXISTS assets_location_trgm_idx
    ON inventory.assets USING gin (location gin_trgm_ops);
