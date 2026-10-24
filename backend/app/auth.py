import asyncio
from datetime import timedelta, timezone
import hashlib
import hmac
import secrets
import jwt
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from .db import get_session
from .errors import AppError
from .models import AuthSession, AuthThrottle, User, new_id, utcnow
from .schemas import Login, Register, Payload
from .serialization import user_json

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 5
SCRYPT_PREFIX = f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
LEGACY_SCRYPT_PREFIX = "scrypt$16384$8$1$"
DUMMY_PASSWORD_HASH = SCRYPT_PREFIX + "00" * 16 + "$" + "00" * 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return f"{SCRYPT_PREFIX}{salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        if not isinstance(password, str) or not isinstance(encoded, str) or len(encoded) > 128:
            return False
        method, n, r, p, salt, expected = encoded.split("$")
        if (method, n, r, p) not in {("scrypt", "16384", "8", "1"), ("scrypt", "16384", "8", "5")}:
            return False
        if len(salt) != 32 or len(expected) != 64:
            return False
        raw_salt, raw_expected = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(raw_salt) != 16 or len(raw_expected) != 32:
            return False
        digest = hashlib.scrypt(password.encode(), salt=raw_salt, n=SCRYPT_N, r=SCRYPT_R, p=int(p), dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(digest, raw_expected)
    except (ValueError, TypeError):
        return False


async def throttle(request: Request, session: AsyncSession) -> None:
    # Shared PostgreSQL throttle; raw client addresses are never stored.
    scope = hashlib.sha256(f"auth:{request.client.host if request.client else 'unknown'}".encode()).hexdigest()
    date = utcnow()
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    await session.execute(insert(AuthThrottle).values(scope=scope, count=0, window_started_at=date).on_conflict_do_nothing(index_elements=["scope"]))
    row = await session.scalar(select(AuthThrottle).where(AuthThrottle.scope == scope).with_for_update())
    started = row.window_started_at.replace(tzinfo=timezone.utc) if row.window_started_at.tzinfo is None else row.window_started_at
    if date - started > timedelta(minutes=15):
        row.count, row.window_started_at = 0, date
    row.count += 1
    denied = row.count > 30
    await session.commit()
    if denied:
        raise AppError("Too many sign-in attempts. Please wait fifteen minutes and try again.", 429)


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    settings = request.app.state.settings
    token = request.cookies.get(settings.cookie_name)
    try:
        payload = jwt.decode(token or "", settings.jwt_secret, algorithms=["HS256"], issuer="stova", audience="stova-web", options={"require": ["exp", "iat", "sub", "jti"]})
        auth_session = await session.get(AuthSession, payload["jti"])
        expiry = auth_session.expires_at.replace(tzinfo=timezone.utc) if auth_session and auth_session.expires_at.tzinfo is None else auth_session.expires_at if auth_session else None
        if not auth_session or auth_session.revoked or auth_session.user_id != payload["sub"] or not expiry or expiry <= utcnow():
            raise ValueError("Invalid session")
        user = await session.get(User, payload["sub"])
        if not user:
            raise ValueError("Unknown account")
        request.state.auth_session_id = auth_session.id
        return user
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise AppError("Please sign in to open your publishing studio.", 401) from None


async def sign_in(request: Request, response: Response, session: AsyncSession, user: User) -> dict:
    settings = request.app.state.settings
    date = utcnow()
    expires = date + timedelta(seconds=settings.jwt_lifetime_seconds)
    auth_session = AuthSession(id=new_id(), user_id=user.id, expires_at=expires, revoked=False)
    session.add(auth_session)
    await session.commit()
    token = jwt.encode({"sub": user.id, "jti": auth_session.id, "iat": date, "exp": expires, "iss": "stova", "aud": "stova-web"}, settings.jwt_secret, algorithm="HS256")
    response.set_cookie(settings.cookie_name, token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.jwt_lifetime_seconds, path="/")
    return {"user": user_json(user)}


@router.post("/register", status_code=201)
async def register(body: Register, request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    await throttle(request, session)
    email = str(body.email).lower()
    if await session.scalar(select(User.id).where(User.email == email)):
        raise AppError("An account already exists for this email. Please sign in.", 409)
    async with request.app.state.password_slots:
        encoded = await asyncio.to_thread(hash_password, body.password)
    user = User(id=new_id(), email=email, name=body.name.strip() or email.split("@")[0][:100], password_hash=encoded)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("An account already exists for this email. Please sign in.", 409) from None
    return await sign_in(request, response, session, user)


@router.post("/login")
async def login(body: Login, request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    await throttle(request, session)
    user = await session.scalar(select(User).where(User.email == str(body.email).lower()))
    async with request.app.state.password_slots:
        valid = await asyncio.to_thread(verify_password, body.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
        if user and valid and user.password_hash.startswith(LEGACY_SCRYPT_PREFIX):
            # Commit the upgraded hash with the new session after successful proof.
            user.password_hash = await asyncio.to_thread(hash_password, body.password)
    if not user or not valid:
        raise AppError("The email or password is incorrect.", 401)
    return await sign_in(request, response, session, user)


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"user": user_json(user)}


@router.post("/logout")
async def logout(body: Payload, request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    try:
        await current_user(request, session)
        auth_session = await session.get(AuthSession, request.state.auth_session_id)
        auth_session.revoked = True
        await session.commit()
    except AppError as exc:
        if exc.status != 401:
            raise
    response.delete_cookie(request.app.state.settings.cookie_name, path="/", secure=request.app.state.settings.cookie_secure, httponly=True, samesite="lax")
    return {"ok": True}
