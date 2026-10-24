"""Offline-import acceptance tests; only a disposable PostgreSQL *_test is used."""

import asyncio
from copy import deepcopy
from datetime import datetime
import json
import os
from uuid import uuid4

from asgi_lifespan import LifespanManager
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from scripts import import_publications as importer
from app.budget import account_scope, read_budget, reserve_attempt
from app.config import Settings
from app.db import Database
from app.errors import AppError
from app.main import create_app
from app.models import Article, Base, Project, User, WritingAccount, WritingReservation

pytestmark = pytest.mark.asyncio
ACCOUNT = "a" * 32
DAY = "2026-10-24"
ORIGIN = "http://testserver"
OWNER = "destination@example.com"
LOGO = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6c9sAAAAASUVORK5CYII="


def backup(*, published=True):
    return {"version": 1, "projects": [{
        "id": str(uuid4()), "name": "Existing publication", "color": "#aa3311", "logo": LOGO,
        "audience": "Returning readers", "tone": "Clear & helpful", "status": "published" if published else "draft",
        "createdAt": "2025-02-03T04:05:06.789Z", "updatedAt": "2026-10-23T18:00:00.000Z",
        "articles": [{"id": str(uuid4()), "title": "A preserved story", "slug": "original-story-address",
                      "content": "\n## Practical ideas\n\n" + "Useful writing for readers. " * 50 + "\n",
                      "status": "published" if published else "ready", "wordCount": 0,
                      "tag": "Ideas", "constraints": "Keep the original editorial direction.",
                      "dropCap": True, "updatedAt": "2026-10-23T17:00:00.000Z", "error": None}],
    }]}


def with_budget(value, amount=600):
    value["writingBudget"] = {"accountId": ACCOUNT, "utcDay": DAY, "reservedNeurons": amount}
    return value


@pytest_asyncio.fixture
async def import_database():
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("Set TEST_DATABASE_URL to a dedicated PostgreSQL database ending in _test.")
    url = importer.database_url(url)
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Refusing to create or drop tables outside a dedicated *_test database.")
    database = Database(url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await database.close()


@pytest_asyncio.fixture
async def destination(import_database):
    settings = Settings(_env_file=None, database_url=str(import_database.engine.url.render_as_string(hide_password=False)),
                        jwt_secret="test_import_signing_key_with_at_least_32_characters", cookie_secure=False,
                        allowed_origins=ORIGIN, environment="test", cloudflare_account_id="",
                        cloudflare_api_token="", cloudflare_ai_free_tier_confirmed="false")

    def no_provider_network(request):
        raise AssertionError("Import and public reading must never contact a writing provider.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_provider_network)) as provider:
        application = create_app(settings, http_client=provider)
        async with LifespanManager(application):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=ORIGIN,
                                         headers={"Origin": ORIGIN}) as client:
                response = await client.post("/api/auth/register", json={"email": OWNER,
                                             "password": "A destination password for tests", "name": "Destination owner"})
                assert response.status_code == 201, response.text
                yield {"database": import_database, "client": client, "app": application,
                       "owner": response.json()["user"]}


async def test_import_preserves_public_addresses_content_dates_branding_and_ownership(destination):
    value = backup()
    source = deepcopy(value["projects"][0])
    result = await importer.import_backup(destination["database"], value, " DESTINATION@example.com ")
    assert result == {"projects": 1, "articles": 1, "publishedArticles": 1, "budgetReservationsAdded": 0}
    client = destination["client"]
    url = f"/api/publications/{source['id']}/articles/{source['articles'][0]['slug']}"
    # The original publication and story URL segments are preserved for public readers.
    client.cookies.clear()
    public = await client.get(url)
    assert public.status_code == 200, public.text
    story, project = public.json()["article"], public.json()["project"]
    assert story["id"] == source["articles"][0]["id"] and story["content"] == source["articles"][0]["content"]
    assert story["wordCount"] == 202 and story["dropCap"] is True
    assert story["updatedAt"] == source["articles"][0]["updatedAt"]
    assert project["color"] == "#AA3311" and project["logo"] == LOGO
    assert project["createdAt"] == source["createdAt"] and project["updatedAt"] == source["updatedAt"]
    assert project["audience"] == "" and story["constraints"] == ""
    async with destination["database"].sessions() as session:
        stored = await session.get(Project, source["id"])
        assert stored.owner_id == destination["owner"]["id"] and stored.audience == source["audience"]
        article = await session.get(Article, source["articles"][0]["id"])
        assert article.constraints == source["articles"][0]["constraints"]
        assert article.generation_token is None and article.generation_started_at is None


async def test_queued_and_error_drafts_remain_private_and_preserve_unsaved_work(destination):
    value = backup(published=False)
    project = value["projects"][0]
    project["status"] = "queued"
    article = project["articles"][0]
    article.update(status="queued", content="An unfinished but valuable original draft.", wordCount=0)
    failed = deepcopy(article)
    failed.update(id=str(uuid4()), title="Another saved thought", slug="another-saved-thought", status="error", error="Writing was interrupted.")
    project["articles"].append(failed)
    await importer.import_backup(destination["database"], value, OWNER)
    saved = (await destination["client"].get(f"/api/projects/{project['id']}")).json()["project"]
    assert saved["status"] == "draft"
    assert [item["status"] for item in saved["articles"]] == ["draft", "error"]
    assert saved["articles"][0]["content"] == article["content"]
    assert saved["articles"][1]["error"] == failed["error"]
    assert (await destination["client"].get(f"/api/publications/{project['id']}")).status_code == 404


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(apiKey="PRIVATE_VALUE_MUST_NOT_APPEAR"),
    lambda value: value["projects"][0].update(ownerId="attacker"),
    lambda value: value["projects"][0]["articles"][0].update(token="PRIVATE_VALUE_MUST_NOT_APPEAR"),
    lambda value: value["projects"][0].update(color="blue"),
    lambda value: value["projects"][0].update(id=" original-id-with-spaces "),
    lambda value: value["projects"][0].update(logo="data:image/svg+xml;base64,PHN2Zy8+"),
    lambda value: value["projects"][0]["articles"][0].update(content="Too short to publish."),
    lambda value: value["projects"][0]["articles"][0].update(slug="../unsafe"),
    lambda value: value["projects"][0].update(updatedAt="2026-10-24T12:00:00"),
    lambda value: value["projects"][0]["articles"][0].update(status="ready"),
    lambda value: value.update(version=True),
    lambda value: value.update(projects=value["projects"] * 101),
    lambda value: value["projects"][0].update(articles=value["projects"][0]["articles"] * 11),
])
async def test_all_input_is_validated_before_transaction_and_sensitive_values_are_never_logged(mutate):
    value = backup()
    mutate(value)
    # This object has no engine or sessions; any database access would fail differently.
    with pytest.raises(importer.ImportRejected) as caught:
        await importer.import_backup(object(), value, OWNER)
    assert "PRIVATE_VALUE_MUST_NOT_APPEAR" not in str(caught.value)


async def test_every_project_is_validated_before_any_existing_data_or_budget_is_changed(destination):
    value = with_budget(backup())
    invalid = deepcopy(value["projects"][0])
    invalid.update(id=str(uuid4()), color="invalid")
    value["projects"].append(invalid)
    with pytest.raises(importer.ImportRejected):
        await importer.import_backup(destination["database"], value, OWNER)
    async with destination["database"].sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 0
        assert await session.scalar(select(func.count()).select_from(WritingReservation)) == 0


async def test_missing_destination_owner_never_creates_an_account_or_publication(import_database):
    with pytest.raises(importer.ImportRejected, match="not registered"):
        await importer.import_backup(import_database, with_budget(backup()), OWNER)
    async with import_database.sessions() as session:
        for model in (User, Project, Article, WritingAccount, WritingReservation):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_existing_id_collision_rejects_the_entire_second_import(destination):
    value = with_budget(backup(), 300)
    await importer.import_backup(destination["database"], value, OWNER)
    original = value["projects"][0]
    new_project = backup()["projects"][0]
    # A unique publication placed first must not be partially inserted before a later collision.
    second = {"version": 1, "projects": [new_project, original], "writingBudget": {
        "accountId": ACCOUNT, "utcDay": DAY, "reservedNeurons": 9000}}
    with pytest.raises(importer.ImportCollision):
        await importer.import_backup(destination["database"], second, OWNER)
    async with destination["database"].sessions() as session:
        assert await session.get(Project, new_project["id"]) is None
        assert await session.scalar(select(func.count()).select_from(Project)) == 1
        assert await session.scalar(select(func.sum(WritingReservation.reserved_neurons))) == 300
    # Article IDs also collide globally, even when the publication ID is new.
    new_project["articles"][0]["id"] = original["articles"][0]["id"]
    with pytest.raises(importer.ImportCollision):
        await importer.import_backup(destination["database"], {"version": 1, "projects": [new_project]}, OWNER)


async def test_budget_carry_only_adds_missing_fixed_reservations_and_never_reduces_usage(destination):
    database = destination["database"]
    at = datetime.fromisoformat(DAY + "T12:00:00+00:00")
    await reserve_attempt(database, ACCOUNT, at)
    first = await importer.import_backup(database, with_budget(backup(), 900), OWNER)
    assert first["budgetReservationsAdded"] == 2
    second = await importer.import_backup(database, with_budget(backup(), 300), OWNER)
    assert second["budgetReservationsAdded"] == 0
    assert (await read_budget(database, ACCOUNT, at))["reservedNeurons"] == 900
    async with database.sessions() as session:
        rows = (await session.scalars(select(WritingReservation))).all()
        assert len(rows) == 3 and all(row.reserved_neurons == 300 for row in rows)
        assert all(row.account_scope == account_scope(ACCOUNT) and ACCOUNT not in row.account_scope for row in rows)


async def test_budget_carry_counts_adjacent_day_reservations_and_cannot_reopen_a_full_day(destination):
    database = destination["database"]
    await reserve_attempt(database, ACCOUNT, datetime.fromisoformat("2026-10-23T23:58:00+00:00"))
    first = await importer.import_backup(database, with_budget(backup(), 9000), OWNER)
    assert first["budgetReservationsAdded"] == 29
    second = await importer.import_backup(database, with_budget(backup(), 9000), OWNER)
    assert second["budgetReservationsAdded"] == 0
    at = datetime.fromisoformat(DAY + "T12:00:00+00:00")
    assert (await read_budget(database, ACCOUNT, at))["reservedNeurons"] == 9000
    with pytest.raises(AppError) as stopped:
        await reserve_attempt(database, ACCOUNT, at)
    assert stopped.value.status == 429


@pytest.mark.parametrize("field,value", [
    ("accountId", "invalid"), ("utcDay", "2026-02-30"),
    ("reservedNeurons", -300), ("reservedNeurons", 9300),
    ("reservedNeurons", 301), ("reservedNeurons", True),
])
async def test_invalid_budget_carry_is_rejected_before_database_access(field, value):
    data = with_budget(backup())
    data["writingBudget"][field] = value
    with pytest.raises(importer.ImportRejected):
        await importer.import_backup(object(), data, OWNER)


async def test_concurrent_budget_imports_and_late_id_conflict_rollback_together(destination, monkeypatch):
    database, client = destination["database"], destination["client"]
    other_owner = "other-destination@example.com"
    registered = await client.post("/api/auth/register", json={"email": other_owner,
                                   "password": "Another destination test password", "name": "Other owner"})
    assert registered.status_code == 201
    first = with_budget(backup(), 600)
    second = deepcopy(first)
    second["writingBudget"]["reservedNeurons"] = 900
    original = importer._carry_budget
    both_prechecked = asyncio.Event()
    entered = 0

    async def gated_carry(session, budget):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_prechecked.set()
        await asyncio.wait_for(both_prechecked.wait(), 10)
        return await original(session, budget)

    monkeypatch.setattr(importer, "_carry_budget", gated_carry)
    results = await asyncio.wait_for(asyncio.gather(
        importer.import_backup(database, first, OWNER),
        importer.import_backup(database, second, other_owner),
        return_exceptions=True,
    ), 15)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, importer.ImportCollision) for result in results) == 1
    winner = next(index for index, result in enumerate(results) if isinstance(result, dict))
    expected_usage = [600, 900][winner]
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 1
        assert await session.scalar(select(func.sum(WritingReservation.reserved_neurons))) == expected_usage
        stored = await session.get(Project, first["projects"][0]["id"])
        assert stored.owner_id == [destination["owner"]["id"], registered.json()["user"]["id"]][winner]


async def test_offline_file_bounds_duplicate_json_fields_and_safe_cli_output(destination, tmp_path, capsys):
    path = tmp_path / "backup.json"
    path.write_text('{"version":1,"version":1,"projects":[]}', encoding="utf-8")
    with pytest.raises(importer.ImportRejected):
        importer.read_backup(path)
    path.write_bytes(b" " * (importer.MAX_FILE_BYTES + 1))
    with pytest.raises(importer.ImportRejected, match="20 MB"):
        importer.read_backup(path)
    value = backup()
    path.write_text(json.dumps(value), encoding="utf-8")
    destination_url = destination["database"].engine.url.render_as_string(hide_password=False)
    result = await asyncio.to_thread(importer.main, [str(path), "--owner-email", OWNER, "--database-url", destination_url])
    assert result == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"projects": 1, "articles": 1, "publishedArticles": 1, "budgetReservationsAdded": 0}
    assert output.err == ""
    assert OWNER not in output.out and value["projects"][0]["name"] not in output.out
    assert destination_url not in output.out
