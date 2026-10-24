"""Tornado serves the React build and forwards REST/SSE to the private FastAPI API."""
import asyncio
import hmac
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
import tornado.httpserver
import tornado.web
from tornado.iostream import StreamClosedError

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "content-length"}


class SecurityHeaders:
    def set_default_headers(self):
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.set_header("X-Frame-Options", "DENY")
        self.set_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.set_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if self.request.path == "/api/docs":
            self.set_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

    def prepare(self):
        if self.request.path in ("/healthz", "/readyz"):
            return
        secret = self.settings.get("origin_verify_secret", "")
        if secret and not hmac.compare_digest(self.request.headers.get("X-Stova-Origin", ""), secret):
            raise tornado.web.HTTPError(403)


class APIProxy(SecurityHeaders, tornado.web.RequestHandler):
    SUPPORTED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

    async def proxy(self, *args):
        self.disconnected = False
        self.pending_task = asyncio.current_task()
        headers = {name: self.request.headers[name] for name in ("Accept", "Content-Type", "Cookie", "Origin", "Sec-Fetch-Site") if name in self.request.headers}
        headers["Host"] = self.request.host
        headers["X-Forwarded-For"] = self.request.remote_ip
        # Production runs behind an HTTPS distribution; clients cannot supply this
        # flag or bypass the origin secret. Local development stays HTTP.
        headers["X-Forwarded-Proto"] = "https" if self.settings.get("public_https") else self.request.protocol
        url = self.settings["backend_url"] + self.request.path + ("?" + self.request.query if self.request.query else "")
        response_bytes = 0
        try:
            async with self.settings["http_client"].stream(self.request.method, url, headers=headers, content=self.request.body) as response:
                self.set_status(response.status_code)
                ignored = HOP_HEADERS | {value.strip().lower() for value in response.headers.get("connection", "").split(",")}
                for key, value in response.headers.multi_items():
                    if key.lower() not in ignored:
                        if key.lower() == "set-cookie":
                            self.add_header(key, value)
                        else:
                            self.set_header(key, value)
                self.set_header("Cache-Control", "no-store")
                self.set_header("X-Accel-Buffering", "no")
                async for chunk in response.aiter_raw():
                    response_bytes += len(chunk)
                    if response_bytes > 16_000_000:
                        raise ValueError("Upstream response exceeds the gateway limit")
                    if chunk:
                        self.write(chunk)
                        await self.flush()
                if not self.disconnected:
                    self.finish()
        except (asyncio.CancelledError, StreamClosedError):
            return
        except (httpx.HTTPError, ValueError):
            if not self.disconnected:
                if not self._headers_written:
                    self.set_status(502)
                    self.set_header("Content-Type", "application/json")
                    self.finish({"error": "The publishing service is temporarily unavailable. Please try again."})
                else:
                    self.finish()
        finally:
            self.pending_task = None

    get = head = post = put = patch = delete = options = proxy

    def on_connection_close(self):
        self.disconnected = True
        task = getattr(self, "pending_task", None)
        if task and not task.done():
            task.cancel()


class SPAHandler(SecurityHeaders, tornado.web.StaticFileHandler):
    def validate_absolute_path(self, root, absolute_path):
        # Only browser routes fall back to index.html. Missing JS/images stay404.
        if self.request.path == "/" or self.request.path.startswith("/blog/"):
            absolute_path = str(Path(root) / "index.html")
        return super().validate_absolute_path(root, absolute_path)

    def set_extra_headers(self, path):
        self.set_header("Cache-Control", "public, max-age=31536000, immutable" if "/assets/" in self.request.path else "no-cache")


def make_app(*, backend_url=None, static_dir=None, origin_verify_secret=None, public_https=None):
    backend = (backend_url or os.getenv("BACKEND_URL", "http://127.0.0.1:8000")).rstrip("/")
    if urlsplit(backend).scheme not in ("http", "https") or urlsplit(backend).path not in ("", "/"):
        raise ValueError("BACKEND_URL must be an HTTP origin")
    static = Path(static_dir or os.getenv("STATIC_DIR", str(ROOT / "frontend" / "dist"))).resolve()
    origin_secret = os.getenv("ORIGIN_VERIFY_SECRET", "") if origin_verify_secret is None else origin_verify_secret
    https = os.getenv("PUBLIC_HTTPS", "false").lower() == "true" if public_https is None else public_https
    if https and not origin_secret:
        raise ValueError("ORIGIN_VERIFY_SECRET is required when PUBLIC_HTTPS is enabled")
    client = httpx.AsyncClient(timeout=httpx.Timeout(210, connect=5), follow_redirects=False, trust_env=False)
    return tornado.web.Application([
        (r"/api(?:/.*)?", APIProxy),
        (r"/(?:healthz|readyz)", APIProxy),
        (r"/(.*)", SPAHandler, {"path": str(static), "default_filename": "index.html"}),
    ], backend_url=backend, http_client=client,
       origin_verify_secret=origin_secret, public_https=https,
       debug=False, compress_response=False)


async def main():
    app = make_app()
    server = tornado.httpserver.HTTPServer(app, max_body_size=1_500_000, max_buffer_size=2_000_000, xheaders=False)
    server.listen(int(os.getenv("PORT", "8080")), address="0.0.0.0")
    logging.getLogger("tornado.access").setLevel(logging.WARNING)
    try:
        await asyncio.Event().wait()
    finally:
        server.stop()
        await server.close_all_connections()
        await app.settings["http_client"].aclose()


if __name__ == "__main__":
    asyncio.run(main())
