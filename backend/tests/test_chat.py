"""Tests for chat schemas, SSE parsing, bearer auth, and Ollama stream relay."""

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from daisy.client import iter_sse
from daisy.main import create_app
from daisy.schema import DEFAULT_MODEL, ChatRequest

TOKEN = "test-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def ollama_transport(lines: list[str], status: int = 200) -> httpx.MockTransport:
    body = "\n".join(lines).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def test_chat_request_defaults_model() -> None:
    assert ChatRequest(message="hi").model == DEFAULT_MODEL


@pytest.mark.parametrize("message", ["", "   "])
def test_chat_request_rejects_blank_message(message: str) -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message=message)


def test_iter_sse_parses_events_and_multiline_data() -> None:
    lines = [": comment", "event: token", "data: a", "data: b", "", "data: plain", ""]
    assert list(iter_sse(lines)) == [("token", "a\nb"), ("message", "plain")]


def test_chat_stream_relays_tokens_and_status() -> None:
    lines = [
        '{"message": {"content": "Hel"}, "done": false}',
        '{"message": {"content": "lo"}, "done": false}',
        '{"done": true}',
    ]
    client = TestClient(create_app(TOKEN, ollama_transport(lines)))
    response = client.post("/chat/stream", json={"message": "hi"}, headers=AUTH)
    assert response.status_code == 200
    events = list(iter_sse(response.text.splitlines()))
    assert [name for name, _ in events] == ["status", "status", "token", "token", "status"]
    assert events[2][1] == '{"content":"Hel"}'
    assert events[3][1] == '{"content":"lo"}'


def test_chat_stream_reports_ollama_http_error() -> None:
    client = TestClient(create_app(TOKEN, ollama_transport(["nope"], status=500)))
    response = client.post("/chat/stream", json={"message": "hi"}, headers=AUTH)
    events = list(iter_sse(response.text.splitlines()))
    assert events[-1][0] == "error"
    assert "HTTP 500" in events[-1][1]


def test_chat_stream_requires_bearer_token() -> None:
    client = TestClient(create_app(TOKEN, ollama_transport([])))
    response = client.post("/chat/stream", json={"message": "hi"})
    assert response.status_code == 401
    wrong = client.post(
        "/chat/stream",
        json={"message": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert wrong.status_code == 401


def test_chat_stream_rejects_blank_message_with_422() -> None:
    client = TestClient(create_app(TOKEN, ollama_transport([])))
    response = client.post("/chat/stream", json={"message": " "}, headers=AUTH)
    assert response.status_code == 422


def test_health_stays_open() -> None:
    client = TestClient(create_app(TOKEN))
    assert client.get("/health").json() == {"status": "ok"}
