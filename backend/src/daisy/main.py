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
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from daisy.backup import DEFAULT_WORKSPACE
from daisy.db import DEFAULT_DB_PATH, get_history, init_db, save_message
from daisy.extensions import DiscoveryResult, discover_extensions
from daisy.judge import JudgeProvider, select_judge_provider
from daisy.preflight import CheckResult, PreflightReport
from daisy.proposals import (
    ApprovalRequiredError,
    Proposal,
    ProposalError,
    ProposalManager,
)
from daisy.schema import (
    ChatRequest,
    Message,
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
HISTORY_DEFAULT_LIMIT: Final[int] = 50
HISTORY_MAX_LIMIT: Final[int] = 500
CORS_ORIGINS: Final[list[str]] = [
    "tauri://localhost",
    "http://tauri.localhost",
    "http://localhost:5173",
]
# Any loopback origin on any port (the sidecar port is ephemeral).
CORS_ORIGIN_REGEX: Final[str] = r"^http://127\.0\.0\.1(:\d+)?$"
CORS_METHODS: Final[list[str]] = ["GET", "POST", "OPTIONS"]
CORS_HEADERS: Final[list[str]] = ["Authorization", "Content-Type"]


class ProposeRequest(BaseModel):
    """Body of POST /proposals/propose: workspace-relative path to full proposed content."""

    changes: dict[str, str]


class DecisionRequest(BaseModel):
    """Body of approve and reject: the named human making the decision."""

    approver: str = Field(max_length=128)


async def stream_ollama_chat(
    request: ChatRequest,
    transport: httpx.AsyncBaseTransport | None,
    db_path: Path | None = None,
) -> AsyncIterator[dict[str, str]]:
    """Relay Ollama's streamed chat JSON lines as strict SSE events.

    On successful completion the accumulated assistant text is persisted.
    """
    yield status_event("connecting")
    payload = {
        "model": request.model,
        "messages": [{"role": "user", "content": request.message}],
        "stream": True,
    }
    parts: list[str] = []
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
                        parts.append(content)
                        yield token_event(content)
                    if chunk.get("done") is True:
                        finished = True
                        break
                if finished:
                    save_message("assistant", "".join(parts), model=request.model, db_path=db_path)
                    yield status_event("done")
                else:
                    yield error_event("Ollama stream ended before completion")
    except httpx.ConnectError:
        yield error_event("Could not connect to Ollama at 127.0.0.1:11434")
    except httpx.HTTPError as exc:
        yield error_event(f"Ollama request failed: {type(exc).__name__}")


def health_payload(now: datetime | None = None) -> dict[str, str]:
    """Return the health body: a status and the current UTC time in ISO 8601."""
    moment = now if now is not None else datetime.now(UTC)
    return {"status": "ok", "timestamp": moment.isoformat()}


def _check_json(check: CheckResult) -> dict[str, object]:
    """Serialize one pre-flight tool run."""
    return {
        "name": check.name,
        "passed": check.passed,
        "errors": list(check.errors),
        "warnings": list(check.warnings),
        "stdout": check.stdout,
        "stderr": check.stderr,
        "returncode": check.returncode,
    }


def _report_json(report: PreflightReport) -> dict[str, object]:
    """Serialize a pre-flight report with every check it ran."""
    return {
        "passed": report.passed,
        "checks": [_check_json(check) for check in report.checks],
    }


def _proposal_json(proposal: Proposal) -> dict[str, object]:
    """Serialize a proposal. Paths are listed; file contents are not echoed back."""
    return {
        "proposal_id": proposal.proposal_id,
        "status": proposal.status.value,
        "created_at": proposal.created_at.isoformat(),
        "paths": sorted(proposal.changes),
        "decided_by": proposal.decided_by,
        "snapshot_id": proposal.snapshot_id,
        "error": proposal.error,
        "judge_verdict": (
            None if proposal.judge_verdict is None else proposal.judge_verdict.to_json()
        ),
        "preflight": _report_json(proposal.report),
    }


def _extensions_json(result: DiscoveryResult) -> dict[str, list[dict[str, str]]]:
    """Serialize discovered extensions, loaded and rejected, with their status."""
    entries: list[dict[str, str]] = [
        {
            "status": "loaded",
            "file": extension.path.name,
            "name": extension.meta.name,
            "version": extension.meta.version,
            "description": extension.meta.description,
        }
        for extension in result.loaded
    ]
    entries.extend(
        {"status": "failed", "file": failure.path.name, "error": failure.error}
        for failure in result.failures
    )
    return {"extensions": entries}


def _find_proposal(manager: ProposalManager, proposal_id: str) -> Proposal:
    """Return the proposal, or raise 404 when the id is unknown."""
    try:
        return manager.get(proposal_id)
    except ProposalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def create_app(
    auth_token: str,
    ollama_transport: httpx.AsyncBaseTransport | None = None,
    db_path: Path | None = None,
    workspace: Path | None = None,
    judge: JudgeProvider | None = None,
) -> FastAPI:
    """Build the FastAPI application guarded by the given bearer token.

    The SQLite database is initialized here, so it exists before the server starts.
    The proposal manager and the extension discovery result are stored on ``app.state``,
    so every request sees the same instances. ``workspace`` defaults to the project root.
    ``judge`` defaults to the provider named by DAISY_JUDGE_PROVIDER (heuristic if unset).
    """
    resolved_db = init_db(db_path or DEFAULT_DB_PATH)
    resolved_workspace = (workspace if workspace is not None else DEFAULT_WORKSPACE).resolve()
    resolved_judge = judge if judge is not None else select_judge_provider()
    app = FastAPI(title=SERVICE_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.proposals = ProposalManager(resolved_workspace, judge=resolved_judge)
    app.state.extensions = discover_extensions(resolved_workspace)

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

    # Added after the bearer middleware so CORS is the outermost layer: preflight
    # OPTIONS is answered before auth, and 401 responses still carry CORS headers
    # so the webview can read them instead of reporting a network failure.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_methods=CORS_METHODS,
        allow_headers=CORS_HEADERS,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return health_payload()

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": SERVICE_NAME, "status": "running"}

    @app.post("/chat/stream")
    async def chat_stream(request: ChatRequest) -> EventSourceResponse:
        save_message("user", request.message, model=request.model, db_path=resolved_db)
        return EventSourceResponse(
            stream_ollama_chat(request, ollama_transport, resolved_db)
        )

    @app.get("/chat/history", response_model=list[Message])
    async def chat_history(
        limit: int = Query(HISTORY_DEFAULT_LIMIT, ge=1, le=HISTORY_MAX_LIMIT),
    ) -> list[dict[str, object]]:
        return get_history(limit=limit, db_path=resolved_db)

    @app.post("/proposals/propose")
    def propose(body: ProposeRequest, request: Request) -> dict[str, object]:
        manager: ProposalManager = request.app.state.proposals
        try:
            proposal = manager.propose(body.changes)
        except ProposalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _proposal_json(proposal)

    @app.post("/proposals/{proposal_id}/approve")
    def approve(proposal_id: str, body: DecisionRequest, request: Request) -> dict[str, object]:
        manager: ProposalManager = request.app.state.proposals
        _find_proposal(manager, proposal_id)
        try:
            proposal = manager.approve(proposal_id, body.approver)
        except ProposalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _proposal_json(proposal)

    @app.post("/proposals/{proposal_id}/reject")
    def reject(proposal_id: str, body: DecisionRequest, request: Request) -> dict[str, object]:
        manager: ProposalManager = request.app.state.proposals
        _find_proposal(manager, proposal_id)
        try:
            proposal = manager.reject(proposal_id, body.approver)
        except ProposalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _proposal_json(proposal)

    @app.post("/proposals/{proposal_id}/apply")
    def apply(proposal_id: str, request: Request) -> dict[str, object]:
        manager: ProposalManager = request.app.state.proposals
        _find_proposal(manager, proposal_id)
        try:
            proposal = manager.apply(proposal_id)
        except ApprovalRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProposalError as exc:
            # Raised after the write failed and the snapshot was restored.
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _proposal_json(proposal)

    @app.post("/proposals/{proposal_id}/judge")
    def judge_proposal(proposal_id: str, request: Request) -> dict[str, object]:
        """Run the LLM judge on a proposal. The verdict is advisory and nothing is written."""
        manager: ProposalManager = request.app.state.proposals
        _find_proposal(manager, proposal_id)
        try:
            proposal = manager.audit(proposal_id)
        except ProposalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _proposal_json(proposal)

    @app.get("/extensions")
    def list_extensions(request: Request) -> dict[str, list[dict[str, str]]]:
        result: DiscoveryResult = request.app.state.extensions
        return _extensions_json(result)

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
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite database file for chat history; parent directories are created.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="Workspace root for proposals and the extensions/ folder.",
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
    """Initialize the app and database, announce the port on stdout, and serve."""
    args = parse_args(argv)
    app = create_app(args.auth_token, db_path=args.db_path, workspace=args.workspace)
    sock = bind_socket(args.host, args.port)
    actual_port = int(sock.getsockname()[1])
    print(f"READY:{actual_port}", flush=True)
    sys.stdout.flush()
    config = uvicorn.Config(app, log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
