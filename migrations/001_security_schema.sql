-- Development PostgreSQL identity and group schema for the JLR platform.
-- Password hashes are produced by backend/security/auth.py and never plaintext.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS app_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(128) NOT NULL UNIQUE,
    email VARCHAR(320) NOT NULL UNIQUE,
    password_hash VARCHAR(512) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_code VARCHAR(128) NOT NULL UNIQUE,
    display_name VARCHAR(256) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_group_memberships (
    user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    group_id UUID NOT NULL REFERENCES app_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);

CREATE INDEX IF NOT EXISTS ix_user_group_memberships_group_id ON user_group_memberships(group_id);
