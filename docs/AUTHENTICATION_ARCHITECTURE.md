# Development Authentication Architecture

## Purpose

This development-only foundation provides basic username/password login and
group-based authorization before corporate identity integration is available.

```text
React login -> FastAPI /auth/login -> PostgreSQL users/groups
              -> signed JWT containing user ID
              -> protected FastAPI route reloads current group membership
              -> allow or deny chat, upload, or confidential retrieval
```

## Storage

- `app_users`: username, email, Argon2id password hash, active state.
- `app_groups`: stable permission codes, such as `adas_chat_users`.
- `user_group_memberships`: many-to-many user/group links.

The database schema is in `migrations/001_security_schema.sql`.

## Runtime Settings

The application requires these runtime environment variables:

```text
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:5432/<database>
JWT_SECRET_KEY=<at-least-32-character-random-secret>
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_MINUTES=30
```

Do not commit these values. The JWT contains only the user UUID; the API reads
group membership from PostgreSQL on each protected request, so group changes
take effect without waiting for a token to expire.
