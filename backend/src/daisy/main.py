"""Daisy Python sidecar: bearer-authenticated FastAPI server on a loopback port.

Tauri launches this process with ``--auth-token <secret>`` and reads ``READY:<port>``
from stdout once the socket is bound. Logs are written to stderr.
"""

import argparse
import hmac
import json
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from daisy.schema import (
    ChatRequest,
    error_event,
    status_event,
    token_event,
)

DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 0
OPEN_PATHS: Final[frozenset[str]] = frozenset({"/health"})
SERVICE_NAME: Final[str] = "daisy-backend"
OLLAMA_CHAT_URL: Final[str] = "http://127.0.0.1:11434/api/chat"
OLLAMA_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(10.0, read=300.0)
MAX_ERROR_BODY_CHARS: Final[int] = 500


async def stream_ollama_chat(
    request: ChatRequest, transport: httpx.AsyncBaseTransport | None
) -> AsyncIterator[dict[str, str]]:
    """Relay Ollama's streamed chat JSON lines as strict SSE events."""
    yield status_event("connecting")
    payload = {
        "model": request.model,
        "messages": [{"role": "user", "content": request.message}],
        "stream": True,
    }
    try:
        async with httpx.AsyncClient(transport=transport, timeout=OLLAMA_TIMEOUT) as client:
            async with client.stream("POST", OLLAMA_CHAT_URL, json=payload) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    body = body[:MAX_ERROR_BODY_CHARS]
                    yield error_event(f"Ollama returned HTTP {response.status_code}: {body}")
                    return
                yield status_event("streaming")
                finished = False
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        yield error_event("Ollama sent invalid JSON")
                        return
                    if not isinstance(chunk, dict):
                        yield error_event("Ollama sent an unexpected payload")
                        return
                    if "error" in chunk:
                        yield error_event(f"Ollama error: {chunk['error']}")
                        return
                    message = chunk.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if isinstance(content, str) and content:
                        yield token_event(content)
                    if chunk.get("done") is True:
                        finished = True
                        break
                if finished:
                    yield status_event("done")
                else:
                    yield error_event("Ollama stream ended before completion")
    except httpx.ConnectError:
        yield error_event("Could not connect to Ollama at 127.0.0.1:11434")
    except httpx.HTTPError as exc:
        yield error_event(f"Ollama request failed: {type(exc).__name__}")


def create_app(
    auth_token: str, ollama_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Build the FastAPI application guarded by the given bearer token."""
    app = FastAPI(title=SERVICE_NAME, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_bearer_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in OPEN_PATHS or request.method == "OPTIONS":
            return await call_next(request)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.encode(), auth_token.encode()
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": SERVICE_NAME, "status": "running"}

    @app.post("/chat/stream")
    async def chat_stream(request: ChatRequest) -> EventSourceResponse:
        return EventSourceResponse(stream_ollama_chat(request, ollama_transport))

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the sidecar."""
    parser = argparse.ArgumentParser(prog=SERVICE_NAME, description="Daisy Python sidecar.")
    parser.add_argument(
        "--auth-token",
        required=True,
        type=str,
        help="Ephemeral secret required as 'Authorization: Bearer <token>'.",
    )
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help="Bind address.")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Bind port; 0 requests an OS-assigned free port.",
    )
    return parser.parse_args(argv)


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind a listening-ready TCP socket, letting the OS pick the port when port is 0.

    The socket is handed directly to uvicorn so the announced port cannot be raced.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def main(argv: list[str] | None = None) -> None:
    """Bind the socket, announce the port on stdout, and serve until terminated."""
    args = parse_args(argv)
    sock = bind_socket(args.host, args.port)
    actual_port = int(sock.getsockname()[1])
    print(f"READY:{actual_port}", flush=True)
    sys.stdout.flush()
    config = uvicorn.Config(create_app(args.auth_token), log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
