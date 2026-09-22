"""ASGI middleware enforcing a configurable maximum request body size."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

SEND_TYPE = Callable[[dict], Awaitable[None]]
RECEIVE_TYPE = Callable[[], Awaitable[dict]]

# These methods conventionally carry no merge payload.
_SKIP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class RequestSizeLimitMiddleware:
    """Reject requests whose body exceeds *max_body_size* with HTTP 413.

    ``max_body_size == 0`` disables the limit. The full body is buffered
    before the inner application runs, which both enforces the limit even when
    ``Content-Length`` is missing or wrong and lets the application re-read
    the body normally.
    """

    def __init__(self, app: Any, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(
        self, scope: dict, receive: RECEIVE_TYPE, send: SEND_TYPE
    ) -> None:
        if (
            scope["type"] != "http"
            or self.max_body_size == 0
            or scope.get("method", "GET").upper() in _SKIP_METHODS
        ):
            await self.app(scope, receive, send)
            return

        declared_length = _content_length(scope)
        if declared_length is not None and declared_length > self.max_body_size:
            await _send_error(send, self.max_body_size)
            return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            body: bytes = message.get("body", b"")
            total += len(body)
            if body:
                chunks.append(body)
            if not message.get("more_body", False):
                break
            if total > self.max_body_size:
                await _send_error(send, self.max_body_size)
                return

        if total > self.max_body_size:
            await _send_error(send, self.max_body_size)
            return

        payload = b"".join(chunks)

        async def replay_receive() -> dict:
            if not getattr(replay_receive, "_sent", False):
                replay_receive._sent = True  # type: ignore[attr-defined]
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


def _content_length(scope: dict) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value.decode("latin-1"))
            except (ValueError, UnicodeDecodeError):
                return None
    return None


async def _send_error(send: SEND_TYPE, max_body_size: int) -> None:
    payload = json.dumps(
        {
            "detail": (
                "request body exceeds the maximum allowed size of "
                f"{max_body_size} bytes"
            )
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"connection", b"close"),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": payload,
            "more_body": False,
        }
    )
