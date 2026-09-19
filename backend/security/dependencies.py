"""FastAPI dependencies for current-user and group-based authorization checks."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.security.auth import AuthSettings, decode_access_token
from backend.security.database import get_session
from backend.security.models import User


_BEARER = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class CurrentPrincipal:
    """Authenticated application user plus current group codes loaded from PostgreSQL."""

    user_id: UUID
    username: str
    group_codes: frozenset[str]


def get_current_principal(credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
                          session: Session = Depends(get_session)) -> CurrentPrincipal:
    """Authenticates a bearer token then reloads current group membership from PostgreSQL."""
    if credentials is None:
        raise _unauthorized()
    try:
        user_id = decode_access_token(credentials.credentials, AuthSettings.from_environment())
    except ValueError:
        raise _unauthorized() from None
    user = session.scalar(select(User).options(selectinload(User.groups)).where(User.id == user_id))
    if user is None or not user.is_active:
        raise _unauthorized()
    return CurrentPrincipal(user_id=user.id, username=user.username,
        group_codes=frozenset(group.group_code for group in user.groups if group.is_active))


def require_groups(*required_group_codes: str):
    """Returns a FastAPI dependency allowing users in at least one configured permission group."""
    required = frozenset(required_group_codes)

    def dependency(principal: CurrentPrincipal = Depends(get_current_principal)) -> CurrentPrincipal:
        """Denies the action without revealing protected content when no permitted group matches."""
        if not required.intersection(principal.group_codes):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not permitted to perform this action.")
        return principal

    return dependency


def _unauthorized() -> HTTPException:
    """Returns one generic authentication failure response for missing, invalid, or expired tokens."""
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication is required.",
        headers={"WWW-Authenticate": "Bearer"})
