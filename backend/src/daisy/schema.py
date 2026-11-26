"""Request, persistence, and strict SSE event schemas for the chat endpoints."""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_MODEL: Final[str] = "qwen2.5-coder:7b"

ChatEventName = Literal["token", "status", "error"]


class ChatRequest(BaseModel):
    """Body accepted by POST /chat/stream."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    model: str = DEFAULT_MODEL

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class Message(BaseModel):
    """A persisted chat message returned by GET /chat/history."""

    model_config = ConfigDict(extra="forbid")

    id: int
    role: str
    content: str
    model: str | None = None
    created_at: str


class TokenData(BaseModel):
    """Payload of a `token` event: a fragment of generated text."""

    model_config = ConfigDict(extra="forbid")

    content: str


class StatusData(BaseModel):
    """Payload of a `status` event: a progress notice."""

    model_config = ConfigDict(extra="forbid")

    message: str


class ErrorData(BaseModel):
    """Payload of an `error` event: a terminal failure description."""

    model_config = ConfigDict(extra="forbid")

    message: str


def _encode(name: ChatEventName, data: BaseModel) -> dict[str, str]:
    return {"event": name, "data": data.model_dump_json()}


def token_event(content: str) -> dict[str, str]:
    """Build a `token` SSE event."""
    return _encode("token", TokenData(content=content))


def status_event(message: str) -> dict[str, str]:
    """Build a `status` SSE event."""
    return _encode("status", StatusData(message=message))


def error_event(message: str) -> dict[str, str]:
    """Build an `error` SSE event."""
    return _encode("error", ErrorData(message=message))
