"""Import an explicitly supplied, offline publication backup into PostgreSQL.

This command never contacts the previous host or a writing provider. Register the
destination owner first, then run it with a version-1 JSON backup and DATABASE_URL.
Existing publications are never overwritten; any collision rolls back the import.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Literal

# Support direct invocation from either the repository root or another directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictInt, TypeAdapter, ValidationError, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.engine import make_url

from app.budget import ATTEMPT_COST, DAILY_LIMIT, account_scope
from app.db import Database
from app.models import Article, Project, User, WritingAccount, WritingReservation, new_id, utcnow
from app.schemas import ArticlePatch, Branding, Topic
from app.serialization import count_words

MAX_FILE_BYTES = 20 * 1024 * 1024
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,35}$"
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class ImportRejected(ValueError):
    """A safe message that never embeds backup values or connection credentials."""


class ImportCollision(ImportRejected):
    pass


def parse_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Use a timestamp with an explicit timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use a timestamp with an explicit timezone.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Use a timestamp with an explicit timezone.")
    return parsed.astimezone(timezone.utc)


class BackupArticle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    title: str
    slug: str = Field(min_length=1, max_length=120, pattern=SLUG_PATTERN)
    content: str = Field(max_length=30000)
    status: Literal["draft", "ready", "published", "error", "queued"]
    wordCount: StrictInt = Field(ge=0, le=3000)
    tag: str = ""
    constraints: str = ""
    dropCap: bool = False
    updatedAt: datetime | None = None
    error: str | None = Field(default=None, max_length=1000)

    _timestamp = field_validator("updatedAt", mode="before")(parse_timestamp)

    @model_validator(mode="after")
    def domain_rules(self):
        topic = Topic.model_validate({"title": self.title, "tag": self.tag, "constraints": self.constraints})
        self.title, self.tag, self.constraints = topic.title, topic.tag, topic.constraints
        # Validate existing domain limits without stripping or rewriting Markdown.
        if self.content:
            ArticlePatch.model_validate({"content": self.content})
        words = count_words(self.content)
        if self.status in ("ready", "published") and not 100 <= words <= 3000:
            raise ValueError("Ready and published articles require 100–3,000 words.")
        self.wordCount = words
        if self.status == "queued":
            self.status = "draft"
        return self


class BackupProject(Branding):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: Literal["draft", "published", "error", "queued"]
    createdAt: datetime | None = None
    updatedAt: datetime | None = None
    articles: list[BackupArticle] = Field(min_length=1, max_length=10)

    _timestamps = field_validator("createdAt", "updatedAt", mode="before")(parse_timestamp)

    @field_validator("id", mode="before")
    @classmethod
    def exact_identifier(cls, value):
        if not isinstance(value, str) or not re.fullmatch(IDENTIFIER_PATTERN, value):
            raise ValueError("Use an unchanged, URL-safe publication ID.")
        return value

    @model_validator(mode="after")
    def publication_rules(self):
        if len({article.slug for article in self.articles}) != len(self.articles):
            raise ValueError("Article slugs must be unique within a publication.")
        if len({article.title.lower() for article in self.articles}) != len(self.articles):
            raise ValueError("Each blog topic must be different.")
        if self.status == "published" and any(article.status != "published" for article in self.articles):
            raise ValueError("A published collection must contain only published articles.")
        if self.status in ("error", "queued"):
            self.status = "draft"
        return self


class BackupBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    accountId: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    utcDay: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    reservedNeurons: StrictInt = Field(ge=0, le=DAILY_LIMIT, multiple_of=ATTEMPT_COST)

    @field_validator("utcDay")
    @classmethod
    def valid_day(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Use a valid UTC calendar day.")
        return value


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    projects: list[BackupProject] = Field(max_length=100)
    writingBudget: BackupBudget | None = None

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Use backup version 1.")
        return value

    @model_validator(mode="after")
    def unique_ids(self):
        project_ids = [project.id for project in self.projects]
        article_ids = [article.id for project in self.projects for article in project.articles]
        if len(project_ids) != len(set(project_ids)) or len(article_ids) != len(set(article_ids)):
            raise ValueError("Publication and article IDs must be unique.")
        return self


def validate_backup(value) -> Backup:
    try:
        if isinstance(value, Backup):
            value = value.model_dump(mode="json")
        return Backup.model_validate(value)
    except ValidationError as exc:
        known_fields = set(Backup.model_fields) | set(BackupProject.model_fields) | set(BackupArticle.model_fields) | set(BackupBudget.model_fields)
        location = ".".join(str(part) if isinstance(part, int) or part in known_fields else "unknown-field"
                            for part in exc.errors(include_input=False, include_url=False)[0]["loc"]) or "backup"
        raise ImportRejected(f"Backup validation failed at {location}. No data was imported.") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportRejected("Backup contains duplicate JSON fields. No data was imported.")
        result[key] = value
    return result


def _reject_json_constant(_):
    raise ValueError("Non-finite JSON numbers are invalid.")


def read_backup(path: Path | str) -> Backup:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise ImportRejected("The backup file could not be read.") from None
    if len(raw) > MAX_FILE_BYTES:
        raise ImportRejected("The backup exceeds the 20 MB file limit.")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ImportRejected("The backup is not valid JSON. No data was imported.") from None
    return validate_backup(value)


def database_url(value: str | None) -> str:
    if not value:
        raise ImportRejected("Set DATABASE_URL or pass --database-url for the destination PostgreSQL database.")
    if value.startswith("postgresql://"):
        value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
    try:
        valid = make_url(value).drivername == "postgresql+asyncpg"
    except (ValueError, SQLAlchemyError):
        valid = False
    if not valid:
        raise ImportRejected("The destination must use PostgreSQL with the asyncpg driver.")
    return value


def owner_address(value: str) -> str:
    try:
        return str(TypeAdapter(EmailStr).validate_python(value.strip())).lower()
    except (ValidationError, AttributeError):
        raise ImportRejected("Provide the email of an already registered destination owner.") from None


async def _carry_budget(session, budget: BackupBudget) -> int:
    scope = account_scope(budget.accountId)
    await session.execute(insert(WritingAccount).values(account_scope=scope).on_conflict_do_nothing(index_elements=["account_scope"]))
    await session.scalar(select(WritingAccount).where(WritingAccount.account_scope == scope).with_for_update())
    existing = int(await session.scalar(select(func.coalesce(func.sum(WritingReservation.reserved_neurons), 0)).where(
        WritingReservation.account_scope == scope,
        or_(WritingReservation.utc_day == budget.utcDay, WritingReservation.adjacent_day == budget.utcDay),
    )))
    if existing < 0 or existing > DAILY_LIMIT or existing % ATTEMPT_COST:
        raise ImportRejected("The destination writing ledger is inconsistent. No data was imported.")
    additional = max(0, budget.reservedNeurons - existing) // ATTEMPT_COST
    for _ in range(additional):
        session.add(WritingReservation(id=new_id(), account_scope=scope, utc_day=budget.utcDay,
                                       adjacent_day=None, reserved_neurons=ATTEMPT_COST, created_at=utcnow()))
    return additional


async def import_backup(database: Database, value, owner_email: str) -> dict[str, int]:
    # All input validation precedes any database transaction or changes.
    backup = validate_backup(value)
    email = owner_address(owner_email)
    if database.engine.dialect.name != "postgresql":
        raise ImportRejected("The destination must be PostgreSQL.")
    project_ids = [project.id for project in backup.projects]
    article_ids = [article.id for project in backup.projects for article in project.articles]
    try:
        async with database.sessions() as session:
            async with session.begin():
                owner = await session.scalar(select(User).where(User.email == email).with_for_update())
                if owner is None:
                    raise ImportRejected("The destination owner is not registered. Register first; no data was imported.")
                if project_ids and await session.scalar(select(Project.id).where(Project.id.in_(project_ids)).limit(1)):
                    raise ImportCollision("A publication ID already exists. Nothing was overwritten or imported.")
                if article_ids and await session.scalar(select(Article.id).where(Article.id.in_(article_ids)).limit(1)):
                    raise ImportCollision("An article ID already exists. Nothing was overwritten or imported.")
                added_reservations = await _carry_budget(session, backup.writingBudget) if backup.writingBudget else 0
                imported_at = utcnow()
                for source in backup.projects:
                    created = source.createdAt or imported_at
                    updated = source.updatedAt or created
                    project = Project(id=source.id, owner_id=owner.id, name=source.name, color=source.color,
                                      logo=source.logo, audience=source.audience, tone=source.tone,
                                      status=source.status, created_at=created, updated_at=updated)
                    project.articles = [Article(
                        id=article.id, title=article.title, slug=article.slug, content=article.content,
                        status=article.status, word_count=article.wordCount, error=article.error,
                        position=position, updated_at=article.updatedAt or updated, tag=article.tag,
                        constraints=article.constraints, drop_cap=article.dropCap,
                        generation_token=None, generation_started_at=None,
                    ) for position, article in enumerate(source.articles)]
                    session.add(project)
                await session.flush()
        return {"projects": len(backup.projects), "articles": len(article_ids),
                "publishedArticles": sum(article.status == "published" for project in backup.projects for article in project.articles),
                "budgetReservationsAdded": added_reservations}
    except IntegrityError:
        raise ImportCollision("A destination record conflicts with this backup. The entire import was rolled back.") from None
    except SQLAlchemyError:
        raise ImportRejected("The destination database did not confirm completion. Check existing publication IDs before retrying; existing records are never overwritten.") from None


async def _run(arguments) -> dict[str, int]:
    backup = read_backup(arguments.backup)
    email = owner_address(arguments.owner_email)
    destination = database_url(arguments.database_url or os.environ.get("DATABASE_URL"))
    database = Database(destination)
    try:
        return await import_backup(database, backup, email)
    finally:
        await database.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="Version-1 offline JSON backup (maximum 20 MB).")
    parser.add_argument("--owner-email", required=True, help="An already registered destination account.")
    parser.add_argument("--database-url", help="Destination PostgreSQL URL; defaults to DATABASE_URL.")
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(arguments))
    except ImportRejected as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("The import could not complete. No backup values or connection details were logged.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
