"""Development login endpoint backed by PostgreSQL users and Argon2id hashes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.security.auth import AuthSettings, authenticate_user, create_access_token
from backend.security.database import get_session


router = APIRouter(prefix="/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    """Accepts a username and password only for the development login endpoint."""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """Returns a bearer access token without returning any password or hash."""

    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, session: Session = Depends(get_session)) -> TokenResponse:
    """Authenticates one active user and issues a short-lived signed JWT."""
    user = authenticate_user(session, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Bearer"})
    return TokenResponse(access_token=create_access_token(user.id, AuthSettings.from_environment()))
