from daisy.core.llm import LocalBrain

brain: LocalBrain = LocalBrain()

while True:
    user_input: str = input("[INPUT] ")
    if user_input == "exit":
        break

    reply: str = brain.chat(user_input, remember=True)

    print("[DAISY]", reply)
