from urllib.parse import urlsplit
from starlette.responses import JSONResponse, Response


class SafetyMiddleware:
    """Bound bodies before parsing, enforce JSON/Origin, and add explicit CORS.

    Pure ASGI keeps SSE cancellation and backpressure intact.
    """
    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        settings = getattr(self.owner.state, "settings", None)
        if settings is None:
            return await JSONResponse({"error": "The API is starting. Please try again shortly."}, 503)(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        origin = headers.get(b"origin", b"").decode("latin1")
        host = headers.get(b"host", b"").decode("latin1")
        same_origin = f"{scope.get('scheme', 'http')}://{host}"
        allowed = origin in settings.origins or origin == same_origin
        method = scope["method"]
        is_api = scope["path"].startswith("/api/")
        mutation = is_api and method in ("POST", "PATCH", "DELETE", "PUT")
        cors = [(b"access-control-allow-origin", origin.encode()), (b"access-control-allow-credentials", b"true"), (b"vary", b"Origin")] if origin and allowed else []

        async def safe_send(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                existing = {key.lower() for key, _ in response_headers}
                response_headers.extend(cors)
                response_headers.extend([(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"strict-origin-when-cross-origin")])
                if is_api and b"cache-control" not in existing:
                    response_headers.append((b"cache-control", b"private, no-store"))
                message["headers"] = response_headers
            await send(message)

        if method == "OPTIONS" and is_api:
            if not origin or not allowed:
                return await JSONResponse({"error": "This origin is not allowed."}, 403)(scope, receive, safe_send)
            return await Response(status_code=204, headers={"Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600"})(scope, receive, safe_send)
        if mutation:
            if (origin and not allowed) or headers.get(b"sec-fetch-site") == b"cross-site":
                return await JSONResponse({"error": "This request must come from your publishing studio."}, 403)(scope, receive, safe_send)
            content_type = headers.get(b"content-type", b"").decode("latin1").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return await JSONResponse({"error": "Please send a JSON request."}, 415)(scope, receive, safe_send)
        if is_api and method not in ("GET", "HEAD", "OPTIONS"):
            try:
                if int(headers.get(b"content-length", b"0")) > settings.max_body_bytes:
                    return await JSONResponse({"error": "This upload is too large."}, 413)(scope, receive, safe_send)
            except ValueError:
                return await JSONResponse({"error": "Invalid request size."}, 400)(scope, receive, safe_send)
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                data = message.get("body", b"")
                size += len(data)
                if size > settings.max_body_bytes:
                    return await JSONResponse({"error": "This upload is too large."}, 413)(scope, receive, safe_send)
                chunks.append(data)
                if not message.get("more_body", False):
                    break
            content = b"".join(chunks)
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": content, "more_body": False}
                return await receive()
            return await self.app(scope, replay, safe_send)
        return await self.app(scope, receive, safe_send)
