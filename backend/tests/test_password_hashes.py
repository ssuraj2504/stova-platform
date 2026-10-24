import hashlib

import pytest
from sqlalchemy import select

from app import auth
from app.models import AuthSession, User
from .helpers import PASSWORD, register


def legacy_hash(password: str) -> str:
    salt = bytes.fromhex("ab" * 16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def test_current_hashes_use_stronger_work_factor_and_unique_salts():
    password = "  a password whose whitespace matters  "
    first, second = auth.hash_password(password), auth.hash_password(password)
    assert first.startswith("scrypt$16384$8$5$")
    assert first.split("$")[4] != second.split("$")[4]
    assert auth.verify_password(password, first)
    assert auth.verify_password(password, second)
    assert not auth.verify_password(password.strip(), first)
    assert not auth.verify_password("another password", first)


def test_existing_scrypt_passwords_remain_verifiable():
    encoded = legacy_hash(PASSWORD)
    assert auth.verify_password(PASSWORD, encoded)
    assert not auth.verify_password("incorrect password", encoded)


def record(parameters="scrypt$16384$8$5", salt="00" * 16, digest="00" * 32):
    return f"{parameters}${salt}${digest}"


@pytest.mark.parametrize("encoded", [
    None,
    b"not a stored password hash",
    "",
    "not a password hash",
    record("argon2$16384$8$5"),
    record("scrypt$1073741824$8$5"),
    record("scrypt$16384$1048576$5"),
    record("scrypt$16384$8$0"),
    record("scrypt$16384$8$2"),
    record("scrypt$16384$8$999999999"),
    record("scrypt$016384$8$5"),
    record(salt="00" * 15),
    record(salt="00" * 17),
    record(salt="zz" * 16),
    record(salt="00" * 15 + "  "),
    record(digest="00" * 31),
    record(digest="00" * 33),
    record(digest="zz" * 32),
    record(digest="00" * 31 + "  "),
    record() + "$extra",
    "scrypt$16384$8$5$" + "0" * 100_000,
], ids=["missing", "nontext", "empty", "badformat", "wrongalgorithm", "excessiveN", "excessiveR", "zeroP", "unapprovedP", "excessiveP", "nonnormalizedN", "shortSalt", "longSalt", "nonhexSalt", "spacedSalt", "shortDigest", "longDigest", "nonhexDigest", "spacedDigest", "extraField", "oversized"])
def test_malformed_or_unapproved_hashes_do_not_start_password_work(monkeypatch, encoded):
    def forbidden_work(*args, **kwargs):
        raise AssertionError("Malformed or unapproved hashes must be rejected before scrypt runs.")

    monkeypatch.setattr(auth.hashlib, "scrypt", forbidden_work)
    assert auth.verify_password(PASSWORD, encoded) is False


async def test_legacy_login_upgrades_only_after_success_without_changing_existing_sessions(client, database, application, monkeypatch):
    account = await register(client)
    original_token = client.cookies.get(application.state.settings.cookie_name)
    encoded = legacy_hash(PASSWORD)
    async with database.sessions() as session:
        user = await session.get(User, account["id"])
        user.password_hash = encoded
        await session.commit()
        original_sessions = (await session.scalars(select(AuthSession).where(AuthSession.user_id == user.id))).all()
        original_ids = {item.id for item in original_sessions}
        original_created_at = user.created_at

    denied = await client.post("/api/auth/login", json={"email": account["email"], "password": "incorrect password"})
    assert denied.status_code == 401
    async with database.sessions() as session:
        user = await session.get(User, account["id"])
        assert user.password_hash == encoded, "A failed login must not rewrite the stored credential."
        sessions = (await session.scalars(select(AuthSession).where(AuthSession.user_id == user.id))).all()
        assert {item.id for item in sessions} == original_ids

    accepted = await client.post("/api/auth/login", json={"email": account["email"], "password": PASSWORD})
    assert accepted.status_code == 200 and accepted.json()["user"] == account
    async with database.sessions() as session:
        user = await session.get(User, account["id"])
        upgraded = user.password_hash
        assert upgraded.startswith("scrypt$16384$8$5$")
        assert upgraded.split("$")[4] != encoded.split("$")[4]
        assert auth.verify_password(PASSWORD, upgraded)
        assert user.created_at == original_created_at
        sessions = (await session.scalars(select(AuthSession).where(AuthSession.user_id == user.id))).all()
        assert len(sessions) == len(original_ids) + 1
        assert original_ids.issubset({item.id for item in sessions})
        assert all(not item.revoked for item in sessions)
    original_cookie = {"Cookie": f"{application.state.settings.cookie_name}={original_token}"}
    assert (await client.get("/api/auth/me", headers=original_cookie)).status_code == 200

    def unexpected_rehash(password):
        raise AssertionError("A current work-factor credential should not be rehashed again.")

    monkeypatch.setattr(auth, "hash_password", unexpected_rehash)
    repeat = await client.post("/api/auth/login", json={"email": account["email"], "password": PASSWORD})
    assert repeat.status_code == 200
    async with database.sessions() as session:
        assert (await session.get(User, account["id"])).password_hash == upgraded


async def test_unknown_account_uses_current_dummy_work_factor(client, database, monkeypatch):
    original_scrypt = auth.hashlib.scrypt
    attempts = []

    def observed_scrypt(*args, **kwargs):
        attempts.append(kwargs)
        return original_scrypt(*args, **kwargs)

    monkeypatch.setattr(auth.hashlib, "scrypt", observed_scrypt)
    response = await client.post("/api/auth/login", json={"email": "missing@example.com", "password": PASSWORD})
    assert response.status_code == 401
    assert len(attempts) == 1
    assert (attempts[0]["n"], attempts[0]["r"], attempts[0]["p"]) == (16384, 8, 5)
    assert attempts[0]["dklen"] == 32 and len(attempts[0]["salt"]) == 16
    async with database.sessions() as session:
        assert (await session.scalars(select(User))).all() == []
        assert (await session.scalars(select(AuthSession))).all() == []
