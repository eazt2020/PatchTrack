-- Verification and justification of non-compliant, stale or unknown assets.
--
-- A justification explains why an asset is in a failing state and what is
-- being done about it. It is versioned rather than edited: superseding one
-- preserves the previous statement and its evidence, so an auditor can see
-- what was claimed at the time a management report was produced.

CREATE TABLE IF NOT EXISTS inventory.asset_justifications (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id           uuid NOT NULL REFERENCES inventory.assets(id) ON DELETE CASCADE,
    compliance_state   text NOT NULL,
    reason_code        text NOT NULL,
    statement          text NOT NULL CHECK (char_length(statement) BETWEEN 20 AND 4000),
    remediation_plan   text NOT NULL DEFAULT '',
    remediation_target date,
    status             text NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'superseded', 'withdrawn')),
    created_by         text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    superseded_at      timestamptz,
    superseded_by      text
);

-- At most one current justification per asset. Superseded and withdrawn rows
-- remain as history.
CREATE UNIQUE INDEX IF NOT EXISTS asset_justifications_one_active
    ON inventory.asset_justifications (asset_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS asset_justifications_asset
    ON inventory.asset_justifications (asset_id, created_at DESC);

CREATE TABLE IF NOT EXISTS inventory.justification_evidence (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    justification_id uuid NOT NULL
                     REFERENCES inventory.asset_justifications(id) ON DELETE CASCADE,
    filename         text NOT NULL,
    content_type     text NOT NULL,
    byte_size        integer NOT NULL CHECK (byte_size > 0),
    sha256           text NOT NULL,
    content          bytea NOT NULL,
    uploaded_by      text NOT NULL,
    uploaded_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS justification_evidence_parent
    ON inventory.justification_evidence (justification_id);
