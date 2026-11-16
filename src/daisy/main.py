from daisy.core.llm import LocalBrain

brain: LocalBrain = LocalBrain()

user_input: str = input("[INPUT] ")
answer: str = brain.ask(user_input)

print("[DAISY]", answer)
