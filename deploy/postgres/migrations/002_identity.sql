-- PatchTrack 002: users, sessions and role assignments.

CREATE TABLE IF NOT EXISTS identity.users (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username          citext NOT NULL UNIQUE,
    display_name      text NOT NULL DEFAULT '',
    email             citext,
    password_hash     text,
    role              text NOT NULL CHECK (role IN
                        ('viewer', 'operations', 'report_manager', 'administrator')),
    auth_source       text NOT NULL DEFAULT 'local'
                        CHECK (auth_source IN ('local', 'oidc')),
    is_active         boolean NOT NULL DEFAULT true,
    must_change_password boolean NOT NULL DEFAULT false,
    failed_attempts   integer NOT NULL DEFAULT 0,
    locked_until      timestamptz,
    last_login_at     timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    created_by        text NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS users_role_idx ON identity.users (role);

-- Refresh tokens are stored as SHA-256 digests; the token itself never
-- touches the database.
CREATE TABLE IF NOT EXISTS identity.refresh_tokens (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES identity.users(id) ON DELETE CASCADE,
    token_hash   text NOT NULL UNIQUE,
    issued_at    timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    user_agent   text NOT NULL DEFAULT '',
    source_ip    inet
);

CREATE INDEX IF NOT EXISTS refresh_tokens_user_idx
    ON identity.refresh_tokens (user_id) WHERE revoked_at IS NULL;

-- Login throttling is per-username and recorded independently of the user row
-- so that a lockout does not reveal whether the account exists.
CREATE TABLE IF NOT EXISTS identity.login_attempts (
    id           bigserial PRIMARY KEY,
    username     citext NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    succeeded    boolean NOT NULL,
    source_ip    inet,
    detail       text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS login_attempts_username_idx
    ON identity.login_attempts (username, attempted_at DESC);
