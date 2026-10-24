from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AuthThrottle(Base):
    __tablename__ = "auth_throttles"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    logo: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    tone: Mapped[str] = mapped_column(String(100), default="Clear & helpful", nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    articles: Mapped[list["Article"]] = relationship(back_populates="project", cascade="all, delete-orphan", order_by="Article.position", lazy="selectin")


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_article_project_slug"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    tag: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    constraints: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    drop_cap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    generation_token: Mapped[str | None] = mapped_column(String(36))
    generation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    project: Mapped[Project] = relationship(back_populates="articles")


class WritingAccount(Base):
    __tablename__ = "writing_accounts"
    account_scope: Mapped[str] = mapped_column(String(67), primary_key=True)


class WritingReservation(Base):
    __tablename__ = "writing_reservations"
    __table_args__ = (
        CheckConstraint("reserved_neurons = 300", name="ck_writing_reservation_fixed"),
        Index("ix_writing_scope_day", "account_scope", "utc_day"),
        Index("ix_writing_scope_adjacent", "account_scope", "adjacent_day"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_scope: Mapped[str] = mapped_column(ForeignKey("writing_accounts.account_scope"), nullable=False)
    utc_day: Mapped[str] = mapped_column(String(10), nullable=False)
    adjacent_day: Mapped[str | None] = mapped_column(String(10))
    reserved_neurons: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
