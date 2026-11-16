import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_SESSIONS_DIR: Path = Path("data/sessions")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = "New Chat"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    messages: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def auto_title(self) -> None:
        if self.title != "New Chat":
            return

        for msg in self.messages:
            if msg.get("role") == "user" and msg.get("content"):
                first_line: str = msg["content"].strip().splitlines()[0]
                self.title = (
                    first_line[:40] + "..." if len(first_line) > 40 else first_line
                )
                return


class SessionStore:
    def __init__(self, directory: str | Path = DEFAULT_SESSIONS_DIR):
        self.directory: Path = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path_for(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def save(self, session: ChatSession) -> None:
        session.updated_at = _now_iso()
        session.auto_title()

        target: Path = self._path_for(session.id)
        tmp: Path = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(asdict(session), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)

    def load(self, session_id: str) -> ChatSession | None:
        path: Path = self._path_for(session_id)
        if not path.exists():
            return None

        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return ChatSession(**data)

        except (json.JSONDecodeError, TypeError):
            return None

    def list_sessions(self) -> list[ChatSession]:
        sessions: list[ChatSession] = []

        for path in self.directory.glob("*.json"):
            session: ChatSession | None = self.load(path.stem)
            if session is not None:
                sessions.append(session)

        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)
