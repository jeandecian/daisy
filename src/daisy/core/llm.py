import ollama
from ollama import ChatResponse

from daisy.core.storage import ChatSession, SessionStore

DEFAULT_MODEL: str = "llama3.2"
DEFAULT_SYSTEM_PROMPT: str = (
    "You are D.A.I.S.Y. (Decian Artificial Intelligence System for You), "
    "a concise, highly capable AI assistant built by Jean Decian. "
    "Keep responses clear, direct, and conversational."
)


class LocalBrain:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        session_store: SessionStore | None = None,
        session: ChatSession | None = None,
    ):
        self.model: str = model
        self.system_prompt: str = system_prompt
        self.store: SessionStore = session_store or SessionStore()
        self.session: ChatSession = session or ChatSession()

        if not self.session.messages:
            self.session.messages.append(
                {"role": "system", "content": self.system_prompt}
            )

    def chat(self, prompt: str, remember: bool = True) -> str:
        user_msg: dict[str, str] = {"role": "user", "content": prompt}
        messages: list[dict[str, str]] = (
            [*self.session.messages, user_msg]
            if remember
            else [{"role": "system", "content": self.system_prompt}, user_msg]
        )

        try:
            response: ChatResponse = ollama.chat(  # type: ignore
                model=self.model,
                messages=messages,
            )

        except ConnectionError as e:
            return str(e)

        reply: str = response["message"]["content"]

        if remember:
            self.session.messages.extend(
                [user_msg, {"role": "assistant", "content": reply}]
            )
            self.store.save(self.session)

        return reply

    def new_session(self) -> None:
        self.session: ChatSession = ChatSession()
        self.session.messages.append({"role": "system", "content": self.system_prompt})

    def load_session(self, session_id: str) -> bool:
        loaded: ChatSession | None = self.store.load(session_id)
        if loaded:
            self.session = loaded
            return True

        return False
