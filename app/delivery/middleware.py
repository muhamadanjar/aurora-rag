from fastapi.responses import JSONResponse

CHAT_COMPLETIONS_PATH = "/api/v1/chat/completions"
MAX_CHAT_REQUEST_BODY_BYTES = 1_048_576


class ChatRequestBodyLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != CHAT_COMPLETIONS_PATH
        ):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(status_code=413, content={"detail": "Chat request body is too large"})
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_CHAT_REQUEST_BODY_BYTES:
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
