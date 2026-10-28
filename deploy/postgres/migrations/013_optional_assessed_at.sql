-- An assessment timestamp is optional.
--
-- A patch source that omits or corrupts the timestamp must not have one
-- invented for it: a fabricated "now" makes an old, undated assessment look
-- freshly collected. NULL here means "unknown", and the compliance engine
-- already evaluates an unknown timestamp as UNKNOWN rather than compliant.
ALTER TABLE compliance.assessments
    ALTER COLUMN assessed_at DROP NOT NULL;
