"""Shared PostgreSQL authentication and group-authorization foundation.

Files in this package:
- ``models.py`` defines PostgreSQL users, groups, and memberships.
- ``database.py`` creates runtime-configured SQLAlchemy sessions.
- ``auth.py`` hashes passwords and creates/verifies signed JWT access tokens.
- ``dependencies.py`` exposes FastAPI current-user and group-check dependencies.

The package stores application identities only for development. Future Entra ID
integration can replace the login adapter while preserving group-based checks.
"""

from backend.security.auth import AuthSettings, authenticate_user, create_access_token, hash_password
from backend.security.dependencies import CurrentPrincipal, require_groups

__all__ = ["AuthSettings", "CurrentPrincipal", "authenticate_user", "create_access_token", "hash_password", "require_groups"]
