import pytest

from app.budget import read_budget, reserve_attempt
from app.errors import AppError
from app.models import utcnow
from .helpers import ACCOUNT_ID, FAKE_TOKEN, MODEL, article_content, article_path, create_project, register, save_article


@pytest.mark.parametrize("field,value", [
    ("cloudflare_account_id", "bad-account"),
    ("cloudflare_api_token", ""),
    ("cloudflare_api_token", "unsafe\nBearer token"),
    ("cloudflare_ai_free_tier_confirmed", "false"),
    ("cloudflare_ai_free_tier_confirmed", "TRUE"),
])
async def test_managed_configuration_fails_closed_without_network(application, provider, settings, field, value):
    setattr(settings, field, value)
    writer = application.state.writer
    assert (await writer.status())["status"] == "setup_required"
    with pytest.raises(AppError) as error:
        await writer.connection(None)
    assert error.value.status == 422
    assert provider.requests == []


async def test_catalog_status_is_truthful_and_does_not_spend_inference(client, database, provider):
    await register(client)
    status = await client.get("/api/writing-status")
    assert status.status_code == 200 and status.json()["provider"] == "cloudflare"
    assert ACCOUNT_ID not in status.text and FAKE_TOKEN not in status.text
    health = await client.post("/api/health", json={})
    assert health.status_code == 200 and health.json()["writer"]["status"] == "connected"
    assert health.json()["writer"]["generationVerified"] is False
    assert len(provider.requests) == 1 and provider.requests[0]["method"] == "GET"
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 0


async def test_revision_and_polish_reserve_every_provider_attempt(client, database, provider):
    await register(client)
    project = await create_project(client)
    provider.replies.extend(["## Short draft\n\nToo short for the brief.", article_content()])
    response = await client.post(article_path(project) + "/generate", json={})
    assert response.status_code == 200, response.text
    assert response.json()["article"]["wordCount"] == 1000
    assert len(provider.requests) == 2
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 600
    assert all(request["body"]["model"] == MODEL and request["body"]["max_tokens"] == 4096 for request in provider.requests)
    assert all(FAKE_TOKEN not in str(request["body"]) for request in provider.requests)
    original = response.json()["article"]["content"]
    provider.replies.append(article_content("REVISED_BUT_UNSAVED"))
    polished = await client.post(article_path(project) + "/polish", json={"action": "style", "style": "Clear and direct", "content": original})
    assert polished.status_code == 200 and polished.json()["content"] != original
    current = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert current["content"] == original, "Polishing must remain an unsaved local revision."
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 900


async def test_exhausted_budget_does_not_call_provider_or_select_another_service(application, database, provider):
    for _ in range(30):
        await reserve_attempt(database, ACCOUNT_ID, utcnow())
    writer = application.state.writer
    connection = await writer.connection(None)
    with pytest.raises(AppError) as error:
        _ = [piece async for piece in writer.complete(connection, "A useful article")]
    assert error.value.status == 429
    assert provider.requests == []
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 9000


async def test_utf8_input_limit_is_checked_before_budget_or_network(application, database, provider):
    writer = application.state.writer
    connection = await writer.connection(None)
    with pytest.raises(AppError) as error:
        _ = [piece async for piece in writer.complete(connection, "é" * 12000)]
    assert error.value.status == 400
    assert provider.requests == []
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 0


async def test_provider_stream_errors_keep_existing_work_without_hidden_retry(client, database, provider):
    await register(client)
    project = await create_project(client)
    saved = await save_article(client, project)
    provider.replies.append(("interrupted", "Only a partial new draft"))
    response = await client.post(article_path(project) + "/stream", json={})
    assert response.status_code == 200 and "event: error" in response.text
    assert "event: complete" not in response.text and FAKE_TOKEN not in response.text
    current = (await client.get(f"/api/projects/{project['id']}")).json()["project"]["articles"][0]
    assert current["content"] == saved["content"] and current["status"] == "ready"
    assert len(provider.requests) == 1
    assert (await read_budget(database, ACCOUNT_ID))["reservedNeurons"] == 300
