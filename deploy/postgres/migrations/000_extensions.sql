-- PatchTrack 000: required PostgreSQL extensions.
-- These are part of the standard PostgreSQL 13+ distribution shipped with
-- RHEL 9 / Rocky Linux 9 (postgresql-contrib is not required for these).

CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
