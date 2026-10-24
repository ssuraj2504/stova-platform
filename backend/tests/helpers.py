import asyncio
from collections import deque
import json

import httpx

ACCOUNT_ID = "a" * 32
FAKE_TOKEN = "STOVA_TEST_ONLY_NEVER_A_REAL_API_TOKEN_1234567890"
MODEL = "@cf/qwen/qwen3-30b-a3b-fp8"
ORIGIN = "http://testserver"
PASSWORD = "A test password with enough length"
LOGO = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6c9sAAAAASUVORK5CYII="


def article_content(first_word: str = "Thoughtful") -> str:
    vocabulary = [first_word, "work", "offers", "practical", "ideas", "for", "curious", "readers", "every", "day."]
    return "## Clear priorities\n\n" + " ".join(vocabulary[index % len(vocabulary)] for index in range(998))


def project_payload(topics=None) -> dict:
    return {
        "name": "A test publication",
        "color": "#1B2A4A",
        "logo": LOGO,
        "audience": "PRIVATE_AUDIENCE_BRIEF",
        "tone": "Clear & helpful",
        "topics": topics or [{"title": "A practical guide to thoughtful work", "tag": "Work", "constraints": "PRIVATE_EDITORIAL_CONSTRAINTS"}],
    }


async def register(client, email="alice@example.com", password=PASSWORD):
    response = await client.post("/api/auth/register", json={"email": email, "password": password, "name": email.split("@")[0].title()})
    assert response.status_code == 201, response.text
    return response.json()["user"]


async def create_project(client, topics=None):
    response = await client.post("/api/projects", json=project_payload(topics))
    assert response.status_code == 201, response.text
    return response.json()["project"]


def article_path(project, index=0):
    return f"/api/projects/{project['id']}/articles/{project['articles'][index]['id']}"


async def save_article(client, project, index=0, content=None, drop_cap=False):
    response = await client.patch(article_path(project, index), json={"content": content or article_content(), "dropCap": drop_cap})
    assert response.status_code == 200, response.text
    return response.json()["article"]


def parse_events(text: str):
    events = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        event, data = None, []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            events.append((event, json.loads("\n".join(data))))
    return events


class ProviderStream(httpx.AsyncByteStream):
    def __init__(self, owner, content: str, interrupted: bool = False):
        self.owner, self.content, self.interrupted = owner, content, interrupted

    async def __aiter__(self):
        pieces = [self.content[:41], self.content[41:193], self.content[193:]]
        for index, piece in enumerate(pieces):
            data = json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
            raw = f"data: {data}\r\n\r\n".encode()
            # Split within arbitrary byte boundaries to exercise real decoding.
            yield raw[:7]
            yield raw[7:]
            await asyncio.sleep(0)
            if index == 0 and self.owner.hold_after_first_chunk:
                self.owner.first_chunk.set()
                await self.owner.chunk_release.wait()
        if self.interrupted:
            yield b'data: {"error":{"message":"TEST_PROVIDER_INTERRUPTION"}}\n\n'
        else:
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    async def aclose(self):
        self.owner.stream_closed.set()


class ProviderStub:
    """Every provider request remains inside httpx.MockTransport."""

    def __init__(self):
        self.requests = []
        self.replies = deque()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.hold_after_first_chunk = False
        self.first_chunk = asyncio.Event()
        self.chunk_release = asyncio.Event()
        self.stream_closed = asyncio.Event()

    def block(self):
        self.entered.clear()
        self.release.clear()

    async def __call__(self, request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        self.requests.append({"url": str(request.url), "method": request.method, "body": body, "authorization": request.headers.get("authorization")})
        assert request.url.host == "api.cloudflare.com", "An unexpected provider fallback was attempted."
        if request.method == "GET":
            assert request.url.path.endswith("/ai/models/search")
            return httpx.Response(200, json={"success": True, "result": [{"name": MODEL}]})
        assert request.url.path.endswith(f"/accounts/{ACCOUNT_ID}/ai/v1/chat/completions")
        assert body["model"] == MODEL and body["max_tokens"] == 4096 and body["stream"] is True
        self.entered.set()
        await self.release.wait()
        reply = self.replies.popleft() if self.replies else article_content()
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, text=f"Raw provider details must stay private: {FAKE_TOKEN}")
        interrupted = isinstance(reply, tuple) and reply[0] == "interrupted"
        content = reply[1] if interrupted else reply
        return httpx.Response(200, stream=ProviderStream(self, content, interrupted), headers={"content-type": "text/event-stream"})
