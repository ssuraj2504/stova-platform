"""Exercise the actual container HTTP stack without running paid inference."""
import argparse
import http.cookiejar
import json
import secrets
import urllib.error
import urllib.request
from uuid import uuid4


def run(base):
    base = base.rstrip("/")
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(path, method="GET", data=None, expected=200):
        request = urllib.request.Request(base + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", "Origin": base})
        try:
            response = client.open(request, timeout=20)
        except urllib.error.HTTPError as exc:
            response = exc
        body = response.read()
        assert response.status == expected, f"{method} {path}: expected{expected}, received{response.status}"
        return json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body.decode()

    assert call("/healthz")["status"] == "ok"
    assert call("/readyz")["status"] == "ready"
    assert "STOVA" in call("/").upper()
    call("/api/projects", expected=401)
    call("/api/auth/register", "POST", {"email": f"smoke-{uuid4().hex}@example.com", "password": secrets.token_urlsafe(24), "name": "Container check"}, 201)
    assert call("/api/auth/me")["user"]["name"] == "Container check"
    project = call("/api/projects", "POST", {"name": "Temporary container check", "color": "#1B2A4A", "logo": None, "audience": "Readers", "tone": "Clear", "topics": ["A practical publication"]}, 201)["project"]
    path = f"/api/projects/{project['id']}"
    article = project["articles"][0]
    try:
        call(f"/api/publications/{project['id']}", expected=404)
        content = "## A practical publication\n\n" + " ".join(["A useful editorial draft keeps its readers informed through clear examples and practical detail."] * 12)
        saved = call(f"{path}/articles/{article['id']}", "PATCH", {"content": content, "dropCap": True})["article"]
        assert saved["status"] == "ready" and saved["wordCount"] >= 100
        assert call(path)["project"]["articles"][0]["content"] == content
        result = call(f"{path}/articles/{article['id']}/publish", "POST", {})
        public = call(f"/api/publications/{project['id']}/articles/{saved['slug']}")
        assert public["article"]["content"] == content
        assert public["article"]["constraints"] == ""
        assert "STOVA" in call(result["url"]).upper()
        assert call("/api/writing-status")["cloudflare"]["model"] == "@cf/qwen/qwen3-30b-a3b-fp8"
    finally:
        call(path, "DELETE", {})
    call("/api/auth/logout", "POST", {})
    call("/api/auth/me", expected=401)
    print("PASS: React, Tornado, FastAPI, PostgreSQL, cookie auth, draft persistence and public publishing; no inference used.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    run(parser.parse_args().base_url)
