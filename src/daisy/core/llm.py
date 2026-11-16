import ollama
from ollama import ChatResponse

DEFAULT_MODEL: str = "llama3.2"
DEFAULT_SYSTEM_PROMPT: str = (
    "You are D.A.I.S.Y. (Decian Artificial Intelligence System for You), "
    "a concise, highly capable AI assistant built by Jean Decian. "
    "Keep responses clear, direct, and conversational."
)


class LocalBrain:
    def __init__(
        self, model: str = DEFAULT_MODEL, system_prompt: str = DEFAULT_SYSTEM_PROMPT
    ):
        self.model: str = model
        self.system_prompt: str = system_prompt
        self.history: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt}
        ]

    def chat(self, prompt: str, remember: bool = True) -> str:
        user_msg: dict[str, str] = {"role": "user", "content": prompt}
        messages: list[dict[str, str]] = (
            [*self.history, user_msg]
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
            self.history.extend([user_msg, {"role": "assistant", "content": reply}])

        return reply
