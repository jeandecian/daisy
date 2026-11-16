import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_SESSIONS_DIR: Path = Path("data/sessions")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    messages: list[dict[str, str]] = field(default_factory=list[dict[str, str]])


class SessionStore:
    def __init__(self, directory: str | Path = DEFAULT_SESSIONS_DIR):
        self.directory: Path = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path_for(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def save(self, session: ChatSession) -> None:
        session.updated_at = _now_iso()

        target: Path = self._path_for(session.id)
        tmp: Path = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(asdict(session), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)
