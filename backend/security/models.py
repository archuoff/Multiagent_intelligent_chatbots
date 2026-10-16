"""PostgreSQL persistence models for development users, groups, and memberships.

No plaintext password is stored here. ``password_hash`` contains the one-way
Argon2id hash created by ``auth.py``. Group membership is the single source of
application authorization for chat, confidential retrieval, and uploads.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Table, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class shared by every PostgreSQL table in the security package."""


user_group_memberships = Table(
    "user_group_memberships",
    Base.metadata,
    Column("user_id", PostgreSQLUUID(as_uuid=True), ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PostgreSQLUUID(as_uuid=True), ForeignKey("app_groups.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    """Represents one application user with a hashed password and group memberships."""

    __tablename__ = "app_users"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    groups: Mapped[list["AccessGroup"]] = relationship(secondary=user_group_memberships, back_populates="users", lazy="selectin")


class AccessGroup(Base):
    """Represents one stable business permission group such as ``adas_chat_users``."""

    __tablename__ = "app_groups"
    __table_args__ = (UniqueConstraint("group_code", name="uq_app_groups_group_code"),)

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    group_code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    users: Mapped[list[User]] = relationship(secondary=user_group_memberships, back_populates="groups", lazy="selectin")
