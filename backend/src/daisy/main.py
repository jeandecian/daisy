"""Daisy Python sidecar: bearer-authenticated FastAPI server on a loopback port.

Tauri launches this process with ``--auth-token <secret>`` and reads ``READY:<port>``
from stdout once the socket is bound. Logs are written to stderr.
"""

import argparse
import hmac
import socket
import sys
from collections.abc import Awaitable, Callable
from typing import Final

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 0
OPEN_PATHS: Final[frozenset[str]] = frozenset({"/health"})
SERVICE_NAME: Final[str] = "daisy-backend"


def create_app(auth_token: str) -> FastAPI:
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
