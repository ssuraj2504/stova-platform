import asyncio
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from app.budget import read_budget
from app.models import Article, utcnow
from .helpers import ACCOUNT_ID, ORIGIN, article_content, article_path, create_project, parse_events, register, save_article


async def test_generation_stream_saves_complete_markdown_and_real_progress(client, provider, database):
    await register(client)
    project = await create_project(client)
    response = await client.post(article_path(project) + "/stream", json={})
    assert response.status_code == 200 and "text/event-stream" in response.headers["content-type"]
    events = parse_events(response.text)
    assert events[0] == ("status", {"phase": "writing"})
    assert any(event == "delta" for event, _ in events)
    assert any(event == "status" and data["phase"] == "validating" for event, data in events)
    complete = [data["article"] for event, data in events if event == "complete"]
    assert len(complete) == 1 and complete[0]["status"] == "ready"
    assert complete[0]["content"] == article_content() and complete[0]["wordCount"] == 1000
    assert not any(event == "error" for event, _ in events)
    saved = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert saved == complete[0]
    assert len(provider.requests) == 1
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300
    assert (await client.patch(f"/api/projects/{project['id']}/topics", json={"articles": [{"id": saved["id"], "title": "Changed after writing"}]})).status_code == 409


async def test_concurrent_generation_cannot_duplicate_an_active_provider_request(client, provider, database):
    await register(client)
    project = await create_project(client)
    provider.block()
    first = asyncio.create_task(client.post(article_path(project) + "/generate", json={}))
    try:
        await asyncio.wait_for(provider.entered.wait(), 10)
        duplicate = await client.post(article_path(project) + "/generate", json={})
        assert duplicate.status_code == 409, duplicate.text
        assert len(provider.requests) == 1
    finally:
        provider.release.set()
    response = await asyncio.wait_for(first, 10)
    assert response.status_code == 200, response.text
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300


async def test_manual_edits_win_a_race_with_inflight_generation(client, provider, database):
    await register(client)
    project = await create_project(client)
    await save_article(client, project)
    provider.block()
    generation = asyncio.create_task(client.post(article_path(project) + "/generate", json={}))
    newest = article_content("NEWEST_SAVED_EDIT")
    try:
        await asyncio.wait_for(provider.entered.wait(), 10)
        edited = await client.patch(article_path(project), json={"content": newest, "dropCap": True})
        assert edited.status_code == 200, edited.text
    finally:
        provider.release.set()
    response = await asyncio.wait_for(generation, 10)
    assert response.status_code == 409, response.text
    saved = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert saved["content"] == newest and saved["status"] == "ready" and saved["dropCap"] is True
    async with database.sessions() as session:
        row = await session.get(Article, saved["id"])
        assert row.generation_token is None
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300


async def test_active_rewrite_blocks_article_and_collection_publication(client, provider):
    await register(client)
    project = await create_project(client)
    await save_article(client, project)
    provider.block()
    generation = asyncio.create_task(client.post(article_path(project) + "/generate", json={}))
    try:
        await asyncio.wait_for(provider.entered.wait(), 10)
        single = await client.post(article_path(project) + "/publish", json={})
        collection = await client.post(f"/api/projects/{project['id']}/publish", json={})
        assert single.status_code == collection.status_code == 409
        assert (await client.get(f"/api/publications/{project['id']}")).status_code == 404
    finally:
        provider.release.set()
        await asyncio.wait_for(generation, 10)


async def test_expired_lock_can_be_retried_but_stale_generation_cannot_save(client, provider, database):
    await register(client)
    project = await create_project(client)
    provider.block()
    original = asyncio.create_task(client.post(article_path(project) + "/generate", json={}))
    replacement = None
    try:
        await asyncio.wait_for(provider.entered.wait(), 10)
        async with database.sessions() as session:
            article = await session.get(Article, project["articles"][0]["id"])
            article.generation_started_at = utcnow() - timedelta(minutes=6)
            await session.commit()
        provider.entered.clear()
        replacement = asyncio.create_task(client.post(article_path(project) + "/generate", json={}))
        await asyncio.wait_for(provider.entered.wait(), 10)
        assert len(provider.requests) == 2
    finally:
        provider.release.set()
        results = await asyncio.wait_for(asyncio.gather(*([original, replacement] if replacement else [original])), 10)
    assert [response.status_code for response in results] == [409, 200]
    current = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert current["content"] == article_content() and current["status"] == "ready"
    async with database.sessions() as session:
        assert (await session.get(Article, current["id"])).generation_token is None
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 600


async def test_failed_rewrite_keeps_the_saved_article_and_releases_lock(client, provider, database):
    await register(client)
    project = await create_project(client)
    old = await save_article(client, project)
    provider.replies.append(503)
    response = await client.post(article_path(project) + "/generate", json={})
    assert response.status_code == 502, response.text
    saved = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert saved["content"] == old["content"] and saved["status"] == "ready"
    assert saved["error"] and "STOVA_TEST_ONLY" not in saved["error"]
    async with database.sessions() as session:
        assert (await session.get(Article, saved["id"])).generation_token is None
    assert len(provider.requests) == 1
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300


async def test_cancelled_generation_preserves_saved_work_and_cost(client, provider, database):
    await register(client)
    project = await create_project(client)
    old = await save_article(client, project)
    provider.block()
    task = asyncio.create_task(client.post(article_path(project) + "/generate", json={}))
    await asyncio.wait_for(provider.entered.wait(), 10)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        provider.release.set()
    saved = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert saved["content"] == old["content"] and saved["status"] == "ready"
    async with database.sessions() as session:
        assert (await session.get(Article, saved["id"])).generation_token is None
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300


async def test_stream_disconnect_cleans_lock_without_publishing_partial_text(client, application, provider, database):
    await register(client)
    project = await create_project(client)
    old = await save_article(client, project)
    provider.hold_after_first_chunk = True
    disconnect = asyncio.Event()
    sent = []
    delivered = False
    body = b"{}"
    token = client.cookies.get(application.state.settings.cookie_name)

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and b"event: delta" in message.get("body", b""):
            disconnect.set()

    url = article_path(project) + "/stream"
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": url, "raw_path": url.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), (b"origin", ORIGIN.encode()), (b"content-type", b"application/json"), (b"content-length", b"2"), (b"cookie", f"{application.state.settings.cookie_name}={token}".encode())],
        "client": ("127.0.0.1", 32123), "server": ("testserver", 80),
    }
    try:
        await asyncio.wait_for(application(scope, receive, send), 10)
    finally:
        provider.chunk_release.set()
    assert disconnect.is_set(), "The test must disconnect after real streamed content, not before generation starts."
    async with database.sessions() as session:
        row = await session.get(Article, old["id"])
        assert row.content == old["content"] and row.status == "ready"
        assert row.generation_token is None, "Disconnect cleanup must not leave a five-minute generation lock behind."
    assert not any(b"event: complete" in message.get("body", b"") for message in sent)
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300
