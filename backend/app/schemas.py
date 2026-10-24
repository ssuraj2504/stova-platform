import base64
import re
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Register(Payload):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)
    name: str = Field(default="", max_length=100)


class Login(Payload):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)


class Topic(Payload):
    title: str = Field(min_length=1, max_length=200)
    tag: str = Field(default="", max_length=80)
    constraints: str = Field(default="", max_length=1000)


class ExistingTopic(Topic):
    id: str = Field(min_length=1, max_length=36)


class TopicsPatch(Payload):
    articles: list[ExistingTopic] = Field(min_length=1, max_length=10)


def validate_logo(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) > 1_400_000:
        raise ValueError("Use a PNG, JPEG, or WebP logo smaller than 1 MB.")
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})", value)
    if not match:
        raise ValueError("Use a PNG, JPEG, or WebP logo.")
    try:
        raw = base64.b64decode(match[2], validate=True)
    except ValueError as exc:
        raise ValueError("This logo is invalid.") from exc
    valid = raw.startswith(b"\x89PNG\r\n\x1a\n") if match[1] == "png" else raw.startswith(b"\xff\xd8\xff") if match[1] == "jpeg" else raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    if not valid or len(raw) > 1_048_576:
        raise ValueError("This logo is invalid or larger than 1 MB.")
    return value


class Branding(Payload):
    name: str = Field(min_length=1, max_length=100)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    logo: str | None = None
    audience: str = Field(default="", max_length=500)
    tone: str = Field(default="Clear & helpful", min_length=1, max_length=100)

    @field_validator("color")
    @classmethod
    def uppercase_color(cls, value: str) -> str:
        return value.upper()

    @field_validator("logo")
    @classmethod
    def raster_logo(cls, value: str | None) -> str | None:
        return validate_logo(value)


class ProjectCreate(Branding):
    topics: list[str | Topic] = Field(min_length=1, max_length=10)

    @field_validator("topics")
    @classmethod
    def distinct_topics(cls, values: list[str | Topic]) -> list[Topic]:
        topics = [Topic(title=value) if isinstance(value, str) else value for value in values]
        if len({topic.title.lower() for topic in topics}) != len(topics):
            raise ValueError("Each blog topic must be different.")
        return topics


class BrandPatch(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    logo: str | None = None
    audience: str | None = Field(default=None, max_length=500)
    tone: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("logo")
    @classmethod
    def raster_logo(cls, value: str | None) -> str | None:
        return validate_logo(value)


class KeyPayload(Payload):
    apiKey: str | None = Field(default=None, max_length=300)


class ArticlePatch(Payload):
    content: str = Field(min_length=1, max_length=30000)
    dropCap: bool | None = None


class Polish(KeyPayload):
    action: str = Field(pattern=r"^(rewrite|expand|style)$")
    section: str | None = Field(default=None, max_length=200)
    style: str | None = Field(default=None, max_length=100)
    content: str = Field(min_length=1, max_length=30000)
