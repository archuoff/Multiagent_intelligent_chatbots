"""Password hashing and signed JWT helpers for development authentication.

Passwords are converted to Argon2id hashes before database storage. JWTs carry
only the user UUID as ``sub`` and a short expiration; group membership is read
from PostgreSQL on protected requests so changed permissions take effect quickly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from uuid import UUID

import jwt
from jwt import InvalidTokenError
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.security.models import User


class AuthSettings(BaseModel):
    """Non-secret JWT behavior plus the required runtime signing secret."""

    jwt_secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = Field(default=30, ge=5, le=480)

    @classmethod
    def from_environment(cls) -> "AuthSettings":
        """Loads only required runtime configuration without printing its secret value."""
        secret = os.getenv("JWT_SECRET_KEY")
        if not secret:
            raise RuntimeError("JWT_SECRET_KEY is required to start authentication.")
        return cls(jwt_secret_key=secret, jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
            access_token_minutes=int(os.getenv("JWT_ACCESS_TOKEN_MINUTES", "30")))


_PASSWORD_HASHER = PasswordHash.recommended()
_DUMMY_HASH = _PASSWORD_HASHER.hash("not-a-real-password")


def hash_password(plain_password: str) -> str:
    """Returns an Argon2id password hash and rejects blank passwords before storage."""
    if not plain_password or len(plain_password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    return _PASSWORD_HASHER.hash(plain_password)


def authenticate_user(session: Session, username: str, password: str) -> User | None:
    """Verifies credentials without exposing whether a username exists."""
    user = session.scalar(select(User).options(selectinload(User.groups)).where(User.username == username))
    if user is None:
        _PASSWORD_HASHER.verify(password, _DUMMY_HASH)
        return None
    if not user.is_active or not _PASSWORD_HASHER.verify(password, user.password_hash):
        return None
    return user


def create_access_token(user_id: UUID, settings: AuthSettings) -> str:
    """Creates a short-lived signed JWT containing only the application user identifier."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    return jwt.encode({"sub": str(user_id), "exp": expires_at}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: AuthSettings) -> UUID:
    """Verifies a signed JWT and returns its user UUID without trusting client-supplied groups."""
    try:
        subject = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]).get("sub")
        return UUID(subject) if subject else _invalid_token()
    except (InvalidTokenError, ValueError, TypeError) as error:
        raise ValueError("Invalid or expired access token.") from error


def _invalid_token() -> UUID:
    """Raises one consistent error when a token has no valid subject identifier."""
    raise ValueError("Invalid or expired access token.")
