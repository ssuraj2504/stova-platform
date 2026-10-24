from contextlib import AsyncExitStack
import os

from asgi_lifespan import LifespanManager
import httpx
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.models import Base
from .helpers import ACCOUNT_ID, FAKE_TOKEN, ORIGIN, ProviderStub, register


@pytest.fixture
def test_database_url():
    value = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not value:
        pytest.fail("Set TEST_DATABASE_URL to an isolated PostgreSQL database named *_test. These tests deliberately do not substitute SQLite.")
    if value.startswith("postgresql://"):
        value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = make_url(value)
    if url.drivername != "postgresql+asyncpg" or not (url.database or "").endswith("_test"):
        pytest.fail("Acceptance tests require PostgreSQL/asyncpg and a database whose name ends _test; refusing to change any other database.")
    return value


@pytest.fixture
def settings(test_database_url):
    return Settings(
        _env_file=None,
        database_url=test_database_url,
        jwt_secret="stova_test_signing_key_with_at_least_32_characters",
        cookie_secure=False,
        allowed_origins=ORIGIN,
        environment="test",
        cloudflare_account_id=ACCOUNT_ID,
        cloudflare_api_token=FAKE_TOKEN,
        cloudflare_ai_free_tier_confirmed="true",
    )


@pytest_asyncio.fixture
async def database(test_database_url):
    database = Database(test_database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await database.close()


@pytest.fixture
def provider():
    return ProviderStub()


@pytest_asyncio.fixture
async def application(settings, database, provider):
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        application = create_app(settings, http_client=provider_client)
        async with LifespanManager(application):
            yield application


@pytest_asyncio.fixture
async def client(application):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        yield client


@pytest_asyncio.fixture
async def users(application):
    async with AsyncExitStack() as stack:
        clients = {}
        for name in ["alice", "bob", "anonymous"]:
            clients[name] = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=ORIGIN, headers={"Origin": ORIGIN}))
        clients["alice_user"] = await register(clients["alice"], "alice@example.com")
        clients["bob_user"] = await register(clients["bob"], "bob@example.com")
        yield clients
