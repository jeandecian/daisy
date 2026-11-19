import platform
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any


def get_current_datetime() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S (%A)")


def get_system_info() -> str:
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def read_local_file(file_path: str) -> str:
    path: Path = Path(file_path).expanduser()
    if not path.exists():
        return f"Error: File '{file_path}' does not exist."
    if not path.is_file():
        return f"Error: '{file_path}' is not a regular file."

    try:
        return path.read_text(encoding="utf-8")

    except UnicodeDecodeError as e:
        return f"Error reading file: {e}"


class ToolRegistry:
    def __init__(
        self,
        tools: list[Callable[..., Any]] | None = None,
        labels: dict[str, str] | None = None,
    ):
        self._tools: dict[str, Callable[..., Any]] = {}
        self._labels: dict[str, str] = labels or {}

        for func in tools or []:
            self.register(func)

    def register(self, func: Callable[..., Any], label: str | None = None) -> None:
        self._tools[func.__name__] = func

        if label:
            self._labels[func.__name__] = label

    @property
    def callables(self) -> list[Callable[..., Any]]:
        return list(self._tools.values())

    def display_names(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []

        for name in self._tools:
            label: str = self._labels.get(name, name.replace("_", " ").title())
            items.append((name, label))

        return items

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        func: Callable[..., Any] | None = self._tools.get(name)
        if func is None:
            return f"Error: Unknown tool '{name}'."

        try:
            result: Any = func(**arguments)
            return str(result)

        except ValueError as e:
            return f"Error executing '{name}': {e}"


def create_default_registry() -> ToolRegistry:
    return ToolRegistry(
        tools=[
            get_current_datetime,
            get_system_info,
            read_local_file,
        ],
        labels={
            "get_current_datetime": "Current Time",
            "get_system_info": "System Info",
            "read_local_file": "Read File",
        },
    )
