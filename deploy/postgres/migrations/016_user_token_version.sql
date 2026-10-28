-- Session revocation counter.
--
-- Access tokens are self-contained, so a JWT stays valid until it expires no
-- matter what happens to the account behind it. Every token now carries the
-- user's token_version; downstream services compare it with this column and
-- refuse the token once the numbers differ. Signing out, changing or resetting
-- a password, and changing a role or disabling an account all increment it,
-- which invalidates every session issued before that moment.

ALTER TABLE identity.users
    ADD COLUMN IF NOT EXISTS token_version integer NOT NULL DEFAULT 0;

-- Login throttling counts recent failures by address and by address/account
-- pair. Without this, every sign-in would scan the whole attempt history.
CREATE INDEX IF NOT EXISTS login_attempts_failed_recent_idx
    ON identity.login_attempts (attempted_at DESC, source_ip)
    WHERE NOT succeeded;
