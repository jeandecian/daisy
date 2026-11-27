"""Tests for chat schemas, SSE parsing, bearer auth, Ollama relay, and chat history."""

from pathlib import Path

import httpx
import pytest
from daisy.client import iter_sse
from daisy.main import create_app
from daisy.schema import DEFAULT_MODEL, ChatRequest
from fastapi.testclient import TestClient
from pydantic import ValidationError

TOKEN = "test-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def ollama_transport(lines: list[str], status: int = 200) -> httpx.MockTransport:
    body = "\n".join(lines).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "daisy.db"


def test_chat_request_defaults_model() -> None:
    assert ChatRequest(message="hi").model == DEFAULT_MODEL


@pytest.mark.parametrize("message", ["", "   "])
def test_chat_request_rejects_blank_message(message: str) -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message=message)


def test_iter_sse_parses_events_and_multiline_data() -> None:
    lines = [": comment", "event: token", "data: a", "data: b", "", "data: plain", ""]
    assert list(iter_sse(lines)) == [("token", "a\nb"), ("message", "plain")]


def test_chat_stream_relays_tokens_and_status(db_path: Path) -> None:
    lines = [
        '{"message": {"content": "Hel"}, "done": false}',
        '{"message": {"content": "lo"}, "done": false}',
        '{"done": true}',
    ]
    client = TestClient(create_app(TOKEN, ollama_transport(lines), db_path=db_path))
    response = client.post("/chat/stream", json={"message": "hi"}, headers=AUTH)
    assert response.status_code == 200
    events = list(iter_sse(response.text.splitlines()))
    assert [name for name, _ in events] == [
        "status",
        "status",
        "token",
        "token",
        "status",
    ]
    assert events[2][1] == '{"content":"Hel"}'
    assert events[3][1] == '{"content":"lo"}'


def test_chat_stream_reports_ollama_http_error(db_path: Path) -> None:
    client = TestClient(
        create_app(TOKEN, ollama_transport(["nope"], status=500), db_path=db_path)
    )
    response = client.post("/chat/stream", json={"message": "hi"}, headers=AUTH)
    events = list(iter_sse(response.text.splitlines()))
    assert events[-1][0] == "error"
    assert "HTTP 500" in events[-1][1]


def test_chat_stream_requires_bearer_token(db_path: Path) -> None:
    client = TestClient(create_app(TOKEN, ollama_transport([]), db_path=db_path))
    response = client.post("/chat/stream", json={"message": "hi"})
    assert response.status_code == 401
    wrong = client.post(
        "/chat/stream",
        json={"message": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert wrong.status_code == 401


def test_chat_stream_rejects_blank_message_with_422(db_path: Path) -> None:
    client = TestClient(create_app(TOKEN, ollama_transport([]), db_path=db_path))
    response = client.post("/chat/stream", json={"message": " "}, headers=AUTH)
    assert response.status_code == 422


def test_health_stays_open(db_path: Path) -> None:
    client = TestClient(create_app(TOKEN, db_path=db_path))
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "timestamp" in body


def test_chat_stream_persists_user_and_assistant_messages(db_path: Path) -> None:
    lines = [
        '{"message": {"content": "Hel"}, "done": false}',
        '{"message": {"content": "lo"}, "done": false}',
        '{"done": true}',
    ]
    client = TestClient(create_app(TOKEN, ollama_transport(lines), db_path=db_path))
    client.post("/chat/stream", json={"message": "hi"}, headers=AUTH)

    history = client.get("/chat/history", headers=AUTH).json()
    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "hi"),
        ("assistant", "Hello"),
    ]
    assert history[1]["model"] == DEFAULT_MODEL
    assert history[0]["model"] == DEFAULT_MODEL


def test_chat_stream_error_saves_only_user_message(db_path: Path) -> None:
    client = TestClient(
        create_app(TOKEN, ollama_transport(["nope"], status=500), db_path=db_path)
    )
    client.post("/chat/stream", json={"message": "hi"}, headers=AUTH)
    history = client.get("/chat/history", headers=AUTH).json()
    assert [m["role"] for m in history] == ["user"]


def test_chat_history_requires_bearer_token(db_path: Path) -> None:
    client = TestClient(create_app(TOKEN, db_path=db_path))
    assert client.get("/chat/history").status_code == 401
    wrong = client.get("/chat/history", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401


def test_chat_history_returns_empty_list_when_no_messages(db_path: Path) -> None:
    client = TestClient(create_app(TOKEN, db_path=db_path))
    response = client.get("/chat/history", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == []


def test_chat_history_rejects_out_of_range_limit(db_path: Path) -> None:
    client = TestClient(create_app(TOKEN, db_path=db_path))
    assert client.get("/chat/history?limit=0", headers=AUTH).status_code == 422
