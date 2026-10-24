import asyncio
from contextlib import aclosing
from time import perf_counter

import pytest

from app import routes
from app.budget import read_budget
from app.models import Article
from .helpers import ACCOUNT_ID, ORIGIN, article_path, create_project, register, save_article


def provider_reads():
    return [task for task in asyncio.all_tasks() if task.get_name() == "stova-provider-next" and not task.done()]


@pytest.mark.parametrize("saved_before", [False, True])
async def test_json_generation_deadline_preserves_draft_and_spent_reservation(client, provider, database, monkeypatch, saved_before):
    # Exercise the production deadline without waiting 50 seconds or allowing
    # httpx's longer read timeout to decide the response.
    monkeypatch.setattr(routes, "SILENT_REQUEST_DEADLINE_SECONDS", 0.8)
    await register(client)
    project = await create_project(client, ["First topic", "Still queued"])
    old = await save_article(client, project) if saved_before else project["articles"][0]
    provider.block()
    started = perf_counter()
    try:
        response = await asyncio.wait_for(client.post(article_path(project) + "/generate", json={}), 3)
    finally:
        provider.release.set()
    assert response.status_code == 504, response.text
    assert "saved draft" in response.json()["error"]
    assert perf_counter() - started < 2
    assert provider.entered.is_set() and len(provider.requests) == 1
    current = (await client.get(f"/api/projects/{project['id']}")).json()["project"]
    saved = current["articles"][0]
    assert saved["content"] == old["content"]
    assert saved["status"] == ("ready" if saved_before else "error")
    assert saved["wordCount"] == old["wordCount"]
    assert current["articles"][1]["content"] == "" and current["articles"][1]["status"] == "draft"
    async with database.sessions() as session:
        row = await session.get(Article, saved["id"])
        assert row.generation_token is None and row.generation_started_at is None
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300
    # The timeout releases the lock immediately, rather than waiting for its
    # five-minute stale-lock window. No retry happens automatically.
    retried = await client.post(article_path(project) + "/generate", json={})
    assert retried.status_code == 200, retried.text
    assert len(provider.requests) == 2


async def test_polish_deadline_keeps_saved_edition_unchanged(client, provider, database, monkeypatch):
    monkeypatch.setattr(routes, "SILENT_REQUEST_DEADLINE_SECONDS", 0.3)
    await register(client)
    project = await create_project(client)
    old = await save_article(client, project)
    provider.block()
    started = perf_counter()
    try:
        response = await asyncio.wait_for(client.post(article_path(project) + "/polish", json={"action": "style", "style": "Plain, practical prose", "content": old["content"]}), 3)
    finally:
        provider.release.set()
    assert response.status_code == 504, response.text
    assert "has not changed" in response.json()["error"]
    assert perf_counter() - started < 2
    assert provider.entered.is_set() and len(provider.requests) == 1
    saved = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert saved == old
    async with database.sessions() as session:
        assert (await session.get(Article, old["id"])).generation_token is None
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300


async def test_timeout_cleanup_failure_does_not_replace_safe_504(client, provider, database, monkeypatch):
    monkeypatch.setattr(routes, "SILENT_REQUEST_DEADLINE_SECONDS", 0.3)
    await register(client)
    project = await create_project(client)
    old = await save_article(client, project)

    async def failed_cleanup(*args, **kwargs):
        raise RuntimeError("INTERNAL_STORAGE_ERROR_MUST_NOT_ESCAPE")

    monkeypatch.setattr(routes, "finish_generation", failed_cleanup)
    provider.block()
    try:
        response = await asyncio.wait_for(client.post(article_path(project) + "/generate", json={}), 3)
    finally:
        provider.release.set()
    assert response.status_code == 504, response.text
    assert "INTERNAL_STORAGE_ERROR" not in response.text
    assert "saved draft" in response.json()["error"]
    saved = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert saved["content"] == old["content"] and saved["status"] == "ready"
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300


async def test_generation_revision_shares_the_same_total_deadline(client, application, provider, database, monkeypatch):
    monkeypatch.setattr(routes, "SILENT_REQUEST_DEADLINE_SECONDS", 2.0)
    monkeypatch.setattr(routes, "SILENT_CLEANUP_GRACE_SECONDS", 0.75)
    await register(client)
    project = await create_project(client)
    old = await save_article(client, project)
    provider.replies.append("## A short first attempt\n\n" + "Useful detail. " * 80)
    complete = application.state.writer.complete

    async def delayed_complete(connection, prompt):
        async for chunk in complete(connection, prompt):
            yield chunk
        await asyncio.sleep(0.8)

    monkeypatch.setattr(application.state.writer, "complete", delayed_complete)
    response = await asyncio.wait_for(client.post(article_path(project) + "/generate", json={}), 3)
    assert response.status_code == 504, response.text
    assert len(provider.requests) == 2, "The first attempt and revision must both run inside one deadline."
    saved = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert saved["content"] == old["content"] and saved["status"] == "ready"
    async with database.sessions() as session:
        assert (await session.get(Article, old["id"])).generation_token is None
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 600


async def test_keepalives_keep_one_read_until_delayed_provider_event(monkeypatch):
    monkeypatch.setattr(routes, "SSE_KEEPALIVE_SECONDS", 0.01)
    released, closed = asyncio.Event(), asyncio.Event()
    reads = 0

    async def delayed_provider():
        nonlocal reads
        try:
            reads += 1
            await released.wait()
            yield "delta", {"text": "A real delayed fragment"}
        finally:
            closed.set()

    async with aclosing(routes.keepalive_events(delayed_provider())) as events:
        assert await asyncio.wait_for(anext(events), 0.5) is None
        assert await asyncio.wait_for(anext(events), 0.5) is None
        assert not released.is_set() and reads == 1
        assert len(provider_reads()) == 1
        released.set()
        assert await asyncio.wait_for(anext(events), 0.5) == ("delta", {"text": "A real delayed fragment"})
    assert closed.is_set() and provider_reads() == []


async def test_keepalive_close_cancels_and_awaits_pending_provider(monkeypatch):
    monkeypatch.setattr(routes, "SSE_KEEPALIVE_SECONDS", 0.01)
    cancelled, closed = asyncio.Event(), asyncio.Event()

    async def silent_provider():
        try:
            await asyncio.Event().wait()
            yield "delta", {"text": "This must never be emitted"}
        except asyncio.CancelledError:
            # Async cleanup has to finish before the wrapper can be closed.
            await asyncio.sleep(0)
            cancelled.set()
            raise
        finally:
            closed.set()

    async with aclosing(routes.keepalive_events(silent_provider())) as events:
        assert await asyncio.wait_for(anext(events), 0.5) is None
        assert len(provider_reads()) == 1
    assert cancelled.is_set() and closed.is_set() and provider_reads() == []


async def test_heartbeat_disconnect_before_provider_tokens_cleans_everything(client, application, provider, database, monkeypatch):
    monkeypatch.setattr(routes, "SSE_KEEPALIVE_SECONDS", 0.01)
    await register(client)
    project = await create_project(client)
    old = await save_article(client, project)
    provider.block()
    disconnect = asyncio.Event()
    sent = []
    delivered = False
    token = client.cookies.get(application.state.settings.cookie_name)

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and b": keepalive\n\n" in message.get("body", b"") and provider.entered.is_set():
            disconnect.set()

    path = article_path(project) + "/stream"
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), (b"origin", ORIGIN.encode()), (b"content-type", b"application/json"), (b"content-length", b"2"), (b"cookie", f"{application.state.settings.cookie_name}={token}".encode())],
        "client": ("127.0.0.1", 32123), "server": ("testserver", 80),
    }
    try:
        await asyncio.wait_for(application(scope, receive, send), 3)
        assert disconnect.is_set() and provider.entered.is_set()
        assert not provider.release.is_set(), "The heartbeat must arrive while the upstream response is still silent."
        assert provider_reads() == []
    finally:
        provider.release.set()
    bodies = b"".join(message.get("body", b"") for message in sent)
    assert b"event: status" in bodies and b": keepalive\n\n" in bodies
    assert b"event: delta" not in bodies and b"event: complete" not in bodies
    async with database.sessions() as session:
        row = await session.get(Article, old["id"])
        assert row.content == old["content"] and row.status == "ready"
        assert row.generation_token is None and row.generation_started_at is None
    assert len(provider.requests) == 1
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300
