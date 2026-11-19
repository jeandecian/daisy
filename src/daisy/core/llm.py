from collections.abc import Callable
from typing import Any

import ollama
from ollama import ChatResponse

from daisy.core.storage import ChatSession, SessionStore
from daisy.core.tools import ToolRegistry, create_default_registry

DEFAULT_MODEL: str = "llama3.2"
DEFAULT_SYSTEM_PROMPT: str = (
    "You are D.A.I.S.Y. (Decian Artificial Intelligence System for You), "
    "a concise, highly capable AI assistant built by Jean Decian. "
    "Keep responses clear, direct, and conversational. "
    "Use available tools only when needed to answer accurately."
)


class LocalBrain:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        session_store: SessionStore | None = None,
        session: ChatSession | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self.model: str = model
        self.system_prompt: str = system_prompt
        self.store: SessionStore = session_store or SessionStore()
        self.session: ChatSession = session or ChatSession()
        self.tools: ToolRegistry = tool_registry or create_default_registry()

        if not self.session.messages:
            self.session.messages.append(
                {"role": "system", "content": self.system_prompt}
            )

    def chat(
        self,
        prompt: str,
        remember: bool = True,
        on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> str:
        working_messages: list[dict[str, Any]] = self._build_working_messages(
            prompt, remember
        )

        while True:
            try:
                response: ChatResponse = ollama.chat(  # type: ignore
                    model=self.model,
                    messages=working_messages,
                    tools=self.tools.callables or None,
                )

            except ConnectionError as e:
                return f"Error: {e}"

            assistant_msg: dict[str, Any] = response.message.model_dump(
                exclude_none=True
            )
            working_messages.append(assistant_msg)

            tool_calls: Any = response.message.tool_calls
            if not tool_calls:
                return self._finalize_reply(
                    response.message.content or "", working_messages, remember
                )

            working_messages.extend(self._execute_tool_calls(tool_calls, on_tool_call))

    def _build_working_messages(
        self, prompt: str, remember: bool
    ) -> list[dict[str, Any]]:
        user_msg: dict[str, Any] = {"role": "user", "content": prompt}

        if remember:
            return [*self.session.messages, user_msg]

        return [{"role": "system", "content": self.system_prompt}, user_msg]

    def _finalize_reply(
        self,
        reply: str,
        working_messages: list[dict[str, Any]],
        remember: bool,
    ) -> str:
        if remember:
            self.session.messages = working_messages
            self.store.save(self.session)

        return reply

    def _execute_tool_calls(
        self,
        tool_calls: Any,
        on_tool_call: Callable[[str, dict[str, Any]], None] | None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        for call in tool_calls:
            tool_name: str = call.function.name
            tool_args: dict[str, Any] = dict(call.function.arguments or {})

            if on_tool_call:
                on_tool_call(tool_name, tool_args)

            tool_output: str = self.tools.execute(tool_name, tool_args)
            results.append(
                {
                    "role": "tool",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "content": tool_output,
                }
            )

        return results

    def new_session(self) -> None:
        self.session: ChatSession = ChatSession()
        self.session.messages.append({"role": "system", "content": self.system_prompt})

    def load_session(self, session_id: str) -> bool:
        loaded: ChatSession | None = self.store.load(session_id)
        if loaded:
            self.session = loaded
            return True

        return False
