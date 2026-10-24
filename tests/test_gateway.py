"""Exercise the real Tornado edge against local HTTP sockets, including SSE."""

import asyncio
import base64
import json
import socket
import tempfile
import unittest
from pathlib import Path

import httpx
import tornado.httpserver
import tornado.netutil
import tornado.web

from gateway.main import make_app


class UpstreamState:
    def __init__(self):
        self.requests = []
        self.first_event_sent = asyncio.Event()
        self.release_final_event = asyncio.Event()
        self.disconnect_started = asyncio.Event()
        self.upstream_disconnected = asyncio.Event()


class MockUpstream(tornado.web.RequestHandler):
    SUPPORTED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

    def initialize(self, state):
        self.state = state
        self.disconnected = False

    async def handle_request(self, _path):
        captured = {
            "method": self.request.method,
            "path": self.request.path,
            "query": self.request.query,
            "body_base64": base64.b64encode(self.request.body).decode(),
            "headers": {key.lower(): value for key, value in self.request.headers.items()},
        }
        self.state.requests.append(captured)
        self.set_header("X-Upstream-Method", self.request.method)

        if self.request.path == "/api/events":
            self.set_header("Content-Type", "text/event-stream")
            self.write(b'event: delta\ndata: {"text":"first words"}\n\n')
            await self.flush()
            self.state.first_event_sent.set()
            await self.state.release_final_event.wait()
            if not self.disconnected:
                self.write(b'event: complete\ndata: {"saved":true}\n\n')
                await self.flush()
                self.finish()
            return

        if self.request.path == "/api/disconnect":
            self.set_header("Content-Type", "text/event-stream")
            self.write(b'event: delta\ndata: {"text":"still writing"}\n\n')
            await self.flush()
            self.state.disconnect_started.set()
            await self.state.upstream_disconnected.wait()
            return

        if self.request.path == "/api/headers":
            self.set_status(201)
            self.set_header("Content-Type", "application/problem+json; charset=utf-8")
            self.add_header("Set-Cookie", "first=one; Path=/; HttpOnly; SameSite=Lax")
            self.add_header("Set-Cookie", "second=two; Path=/; HttpOnly; SameSite=Lax")
            self.set_header("Connection", "X-Upstream-Hop")
            self.set_header("X-Upstream-Hop", "must-not-reach-browser")
            self.set_header("X-Application-Result", "kept")
            self.finish(json.dumps({"saved": True}))
            return

        status = int(self.get_query_argument("status", "200"))
        self.set_status(status)
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(captured))

    get = head = post = put = patch = delete = options = handle_request

    def on_connection_close(self):
        self.disconnected = True
        if self.request.path == "/api/events":
            self.state.release_final_event.set()
        if self.request.path == "/api/disconnect":
            self.state.upstream_disconnected.set()


class GatewayHTTPTests(unittest.IsolatedAsyncioTestCase):
    """No remote network, database or application credentials are needed."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="stova-gateway-test-")
        root = Path(self.directory.name)
        self.static = root / "dist"
        (self.static / "assets").mkdir(parents=True)
        self.index = b'<!doctype html><html><body><div id="root">Stova test shell</div><script src="/assets/app-test.js"></script></body></html>'
        self.asset = b'document.getElementById("root").textContent = "React asset served";'
        (self.static / "index.html").write_bytes(self.index)
        (self.static / "assets" / "app-test.js").write_bytes(self.asset)
        self.outside = b"private fixture outside the static directory"
        (root / "private.txt").write_bytes(self.outside)
        self.secret = "local-test-origin-proof"
        self.state = UpstreamState()
        self.servers = []
        self.apps = []
        upstream = tornado.web.Application([(r"/(.*)", MockUpstream, {"state": self.state})])
        self.upstream_url = self.serve(upstream)
        self.app = make_app(backend_url=self.upstream_url, static_dir=self.static,
                            origin_verify_secret=self.secret, public_https=True)
        self.apps.append(self.app)
        self.base_url = self.serve(self.app)
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=3, trust_env=False,
                                        follow_redirects=False)

    def serve(self, app):
        sockets = tornado.netutil.bind_sockets(0, address="127.0.0.1", family=socket.AF_INET)
        port = sockets[0].getsockname()[1]
        server = tornado.httpserver.HTTPServer(app, xheaders=False)
        server.add_sockets(sockets)
        self.servers.append(server)
        return f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        self.state.release_final_event.set()
        self.state.upstream_disconnected.set()
        await self.client.aclose()
        for server in self.servers:
            server.stop()
        for server in self.servers:
            await server.close_all_connections()
        for app in self.apps:
            await app.settings["http_client"].aclose()
        self.directory.cleanup()

    async def edge(self, method, path, **kwargs):
        headers = {"X-Stova-Origin": self.secret, **kwargs.pop("headers", {})}
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def test_serves_static_assets_and_blog_spa_routes(self):
        for path in ("/", "/blog/publication-id", "/blog/publication-id/a-good-story"):
            with self.subTest(path=path):
                response = await self.edge("GET", path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, self.index)
                self.assertIn("text/html", response.headers["content-type"])
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["x-frame-options"], "DENY")
        response = await self.edge("GET", "/assets/app-test.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.asset)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("immutable", response.headers["cache-control"])

    async def test_missing_asset_is_404_without_spa_fallback(self):
        for path in ("/assets/missing.js", "/missing.png", "/missing-font.woff2"):
            with self.subTest(path=path):
                response = await self.edge("GET", path)
                self.assertEqual(response.status_code, 404)
                self.assertNotEqual(response.content, self.index)

    async def test_path_traversal_cannot_escape_static_root(self):
        for path in ("/%2e%2e/private.txt", "/..%2fprivate.txt", "/assets/%2e%2e/%2e%2e/private.txt"):
            with self.subTest(path=path):
                response = await self.edge("GET", path)
                self.assertIn(response.status_code, (403, 404))
                self.assertNotIn(self.outside, response.content)

    async def test_rest_preserves_methods_query_body_and_status(self):
        body = b'publication=one&content=%00binary\x00end'
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            with self.subTest(method=method):
                response = await self.edge(method, "/api/echo?tag=long%20form&tag=second&status=202",
                                           content=body if method != "HEAD" else b"",
                                           headers={"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.headers["x-upstream-method"], method)
                captured = self.state.requests[-1]
                self.assertEqual(captured["method"], method)
                self.assertEqual(captured["query"], "tag=long%20form&tag=second&status=202")
                self.assertEqual(base64.b64decode(captured["body_base64"]), body if method != "HEAD" else b"")
                self.assertEqual(captured["headers"]["content-type"], "application/x-www-form-urlencoded")
                self.assertEqual(response.headers["cache-control"], "no-store")
                if method == "HEAD":
                    self.assertEqual(response.content, b"")
                else:
                    self.assertEqual(response.json()["method"], method)
        response = await self.edge("GET", "/api/echo?status=418")
        self.assertEqual(response.status_code, 418)
        self.assertEqual(response.json()["path"], "/api/echo")

    async def test_response_content_type_is_single_and_set_cookies_are_separate(self):
        response = await self.edge("POST", "/api/headers", json={"name": "A publication"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers.get_list("content-type"), ["application/problem+json; charset=utf-8"])
        cookies = response.headers.get_list("set-cookie")
        self.assertEqual(len(cookies), 2)
        self.assertTrue(any(value.startswith("first=one;") for value in cookies))
        self.assertTrue(any(value.startswith("second=two;") for value in cookies))
        self.assertEqual(response.headers["x-application-result"], "kept")
        self.assertNotIn("x-upstream-hop", response.headers)
        self.assertEqual(response.json(), {"saved": True})

    async def test_sse_first_chunk_arrives_before_upstream_finishes(self):
        context = self.client.stream("POST", "/api/events", headers={"X-Stova-Origin": self.secret}, json={})
        response = await asyncio.wait_for(context.__aenter__(), 2)
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            chunks = response.aiter_bytes()
            first = await asyncio.wait_for(anext(chunks), 1)
            self.assertIn(b"first words", first)
            self.assertFalse(self.state.release_final_event.is_set())
            self.state.release_final_event.set()
            remaining = b"".join([chunk async for chunk in chunks])
            self.assertIn(b"event: complete", remaining)
        finally:
            await context.__aexit__(None, None, None)

    async def test_client_disconnect_closes_upstream_stream(self):
        context = self.client.stream("GET", "/api/disconnect", headers={"X-Stova-Origin": self.secret})
        response = await asyncio.wait_for(context.__aenter__(), 2)
        self.assertEqual(response.status_code, 200)
        chunks = response.aiter_bytes()
        self.assertIn(b"still writing", await asyncio.wait_for(anext(chunks), 1))
        await asyncio.wait_for(self.state.disconnect_started.wait(), 1)
        await context.__aexit__(None, None, None)
        await asyncio.wait_for(self.state.upstream_disconnected.wait(), 2)

    async def test_origin_proof_is_required_except_liveness_and_readiness(self):
        for path in ("/", "/assets/app-test.js", "/blog/publication/story", "/api/echo"):
            with self.subTest(path=path):
                self.assertEqual((await self.client.get(path)).status_code, 403)
                self.assertEqual((await self.client.get(path, headers={"X-Stova-Origin": "wrong-local-proof"})).status_code, 403)
                self.assertEqual((await self.edge("GET", path)).status_code, 200)
        for path in ("/healthz", "/readyz"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["path"], path)

    async def test_forged_proxy_headers_are_not_trusted_or_forwarded(self):
        response = await self.edge("POST", "/api/echo", json={"test": True}, headers={
            "X-Forwarded-For": "203.0.113.123", "X-Forwarded-Proto": "http",
            "Forwarded": "for=203.0.113.123;proto=http", "X-Real-IP": "203.0.113.124",
            "X-Stova-Client-IP": "203.0.113.125",
            "Origin": "https://studio.example.test", "Sec-Fetch-Site": "same-origin",
            "Cookie": "local-session=test-cookie-value", "Accept": "application/json",
        })
        self.assertEqual(response.status_code, 200)
        headers = response.json()["headers"]
        self.assertEqual(headers["x-forwarded-for"], "127.0.0.1")
        self.assertEqual(headers["x-forwarded-proto"], "https")
        self.assertNotIn("forwarded", headers)
        self.assertNotIn("x-real-ip", headers)
        self.assertNotIn("x-stova-origin", headers)
        self.assertNotIn("x-stova-client-ip", headers)
        self.assertEqual(headers["origin"], "https://studio.example.test")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["cookie"], "local-session=test-cookie-value")
        self.assertEqual(headers["accept"], "application/json")

    async def test_direct_proxy_uses_one_verified_client_address(self):
        self.app.settings["hosting_mode"] = "direct"
        for address, canonical in (("203.0.113.42", "203.0.113.42"),
                                   ("2001:db8:0:0:0:0:0:123", "2001:db8::123")):
            with self.subTest(address=address):
                response = await self.edge("POST", "/api/echo", json={}, headers={
                    "X-Stova-Client-IP": address,
                    "X-Forwarded-For": "203.0.113.99",
                    "X-Real-IP": "203.0.113.98",
                })
                self.assertEqual(response.status_code, 200)
                headers = response.json()["headers"]
                self.assertEqual(headers["x-forwarded-for"], canonical)
                self.assertEqual(headers["x-forwarded-proto"], "https")
                self.assertNotIn("x-stova-client-ip", headers)
                self.assertNotIn("x-stova-origin", headers)
                self.assertNotIn("x-real-ip", headers)

    async def test_direct_proxy_rejects_missing_malformed_or_chained_client_ip(self):
        self.app.settings["hosting_mode"] = "direct"
        for address in (None, "", "not-an-ip", "203.0.113.42, 203.0.113.43",
                        "203.0.113.42:443", "[2001:db8::123]", "fe80::123%eth0"):
            with self.subTest(address=address):
                before = len(self.state.requests)
                response = await self.edge("GET", "/api/echo", headers={
                    "X-Stova-Client-IP": address} if address is not None else {})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(len(self.state.requests), before)

    async def test_direct_client_address_does_not_replace_origin_verification(self):
        self.app.settings["hosting_mode"] = "direct"
        for proof in (None, "wrong-proof"):
            with self.subTest(proof=proof):
                before = len(self.state.requests)
                headers = {"X-Stova-Client-IP": "203.0.113.42"}
                if proof is not None:
                    headers["X-Stova-Origin"] = proof
                response = await self.client.get("/api/echo", headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(len(self.state.requests), before)
        self.assertEqual((await self.client.get("/healthz")).status_code, 200)

    async def test_direct_startup_requires_https_and_a_proxy_secret(self):
        for secret, https in (("", True), (self.secret, False), ("", False)):
            with self.subTest(secret_present=bool(secret), https=https):
                with self.assertRaises(ValueError):
                    make_app(backend_url=self.upstream_url, static_dir=self.static,
                             origin_verify_secret=secret, public_https=https, hosting_mode="direct")

    async def test_https_origin_without_configured_proof_fails_closed(self):
        try:
            app = make_app(backend_url=self.upstream_url, static_dir=self.static,
                           origin_verify_secret="", public_https=True)
        except ValueError:
            return  # Refusing unsafe production startup is also fail-closed.
        self.apps.append(app)
        url = self.serve(app)
        async with httpx.AsyncClient(base_url=url, timeout=3, trust_env=False) as client:
            for path in ("/", "/api/echo"):
                with self.subTest(path=path):
                    self.assertEqual((await client.get(path)).status_code, 403)
            self.assertEqual((await client.get("/healthz")).status_code, 200)
            self.assertEqual((await client.get("/readyz")).status_code, 200)

    async def test_swagger_cdn_policy_is_limited_to_exact_docs_route(self):
        response = await self.edge("GET", "/api/docs")
        self.assertEqual(response.status_code, 200)
        policy = response.headers["content-security-policy"]
        script_policy = next(part.strip() for part in policy.split(";") if part.strip().startswith("script-src "))
        self.assertIn("https://cdn.jsdelivr.net", script_policy)
        self.assertIn("'unsafe-inline'", script_policy)
        for path in ("/", "/api/echo", "/api/docs-extra"):
            with self.subTest(path=path):
                response = await self.edge("GET", path)
                policy = response.headers["content-security-policy"]
                script_policy = next(part.strip() for part in policy.split(";") if part.strip().startswith("script-src "))
                self.assertNotIn("https://cdn.jsdelivr.net", script_policy)
                self.assertNotIn("'unsafe-inline'", script_policy)


if __name__ == "__main__":
    unittest.main()
