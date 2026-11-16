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

    def ask(self, prompt: str):
        try:
            response: ChatResponse = ollama.chat(  # type: ignore
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )

        except ConnectionError as e:
            return str(e)

        return response["message"]["content"]
