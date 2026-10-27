from fastapi.responses import JSONResponse

CHAT_COMPLETIONS_PATH = "/api/v1/chat/completions"
MAX_CHAT_REQUEST_BODY_BYTES = 1_048_576
MAX_CODEBASE_REQUEST_BODY_BYTES = 1_048_576


def _is_codebase_post(path: str, method: str) -> bool:
    if method != "POST":
        return False
    return path == "/codebase/search" or (
        path.startswith("/codebase/projects/") and path.endswith("/index")
    )


class ChatRequestBodyLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        method = scope.get("method", "")
        limited = scope["type"] == "http" and method == "POST" and (
            path == CHAT_COMPLETIONS_PATH or _is_codebase_post(path, method)
        )
        if not limited:
            await self.app(scope, receive, send)
            return

        is_codebase = _is_codebase_post(path, method)
        max_bytes = MAX_CODEBASE_REQUEST_BODY_BYTES if is_codebase else MAX_CHAT_REQUEST_BODY_BYTES
        detail = "Codebase request body is too large" if is_codebase else "Chat request body is too large"
        response = JSONResponse(status_code=413, content={"detail": detail})
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > max_bytes:
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        request_body = bytes(body)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": request_body, "more_body": False}

        await self.app(scope, replay_receive, send)
