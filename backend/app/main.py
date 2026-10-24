from contextlib import asynccontextmanager
import asyncio
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from .auth import router as auth_router
from .config import Settings, get_settings
from .db import Database
from .errors import AppError
from .middleware import SafetyMiddleware
from .routes import router
from .writing import Writer


def create_app(settings: Settings | None = None, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        actual_settings = settings or get_settings()
        if not getattr(application.state, "settings", None):
            application.state.settings = actual_settings
            application.state.db = Database(actual_settings.database_url)
        client = http_client or httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(90, connect=10))
        application.state.http_client = client
        application.state.writer = Writer(actual_settings, application.state.db, client)
        application.state.password_slots = asyncio.Semaphore(4)
        try:
            yield
        finally:
            if http_client is None:
                await client.aclose()
            await application.state.db.close()

    application = FastAPI(title="Stova Publishing API", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    if settings:
        application.state.settings = settings
        application.state.db = Database(settings.database_url)
        application.state.password_slots = asyncio.Semaphore(4)
        if http_client:
            application.state.http_client = http_client
            application.state.writer = Writer(settings, application.state.db, http_client)
    application.add_middleware(SafetyMiddleware, owner=application)
    application.include_router(auth_router)
    application.include_router(router)

    @application.exception_handler(AppError)
    async def app_error(request: Request, error: AppError):
        return JSONResponse({"error": error.message}, error.status)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        first = error.errors()[0] if error.errors() else {}
        return JSONResponse({"error": str(first.get("msg", "The request is invalid.")).removeprefix("Value error, ")}, 400)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        return JSONResponse({"error": "The database is unavailable. Your saved work is safe; please try again later."}, 503)

    @application.exception_handler(Exception)
    async def unknown_error(request: Request, error: Exception):
        logging.getLogger("stova").error("Request failed (%s)", type(error).__name__)
        return JSONResponse({"error": "Something went wrong. Please try again shortly."}, 500)

    @application.get("/healthz", include_in_schema=False)
    async def liveness():
        return {"status": "ok"}

    @application.get("/readyz", include_in_schema=False)
    async def readiness():
        try:
            async with application.state.db.sessions() as session:
                await session.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, 503)

    return application


app = create_app()
