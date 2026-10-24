from functools import lru_cache
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    database_url: str = "postgresql+asyncpg://stova:stova@localhost:5432/stova"
    jwt_secret: str = Field(min_length=32, repr=False)
    jwt_lifetime_seconds: int = Field(default=604800, ge=300, le=2592000)
    cookie_name: str = "stova_session"
    cookie_secure: bool = True
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = Field(default="", repr=False)
    cloudflare_ai_free_tier_confirmed: str = "false"
    environment: str = "production"
    max_body_bytes: int = Field(default=1_500_000, ge=4096, le=2_000_000)

    @field_validator("jwt_secret")
    @classmethod
    def real_signing_secret(cls, value: str) -> str:
        if value.startswith("replace-with-"):
            raise ValueError("Generate a fresh random JWT_SECRET before starting the API.")
        return value

    @field_validator("database_url")
    @classmethod
    def asynchronous_database(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not value.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError("Use PostgreSQL with the asyncpg driver.")
        return value

    @property
    def origins(self) -> list[str]:
        return [value.strip().rstrip("/") for value in self.allowed_origins.split(",") if value.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
