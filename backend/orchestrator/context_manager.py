class ContextManager:
    """
    Holds the agent's running conversation and bounds what is SENT to the
    model. Every model call goes through build(), so changing max_chars (the
    governor's compact_context action) changes the real prompt on the next call.

    Compaction keeps the system prompt and the task (first user message)
    intact, keeps the newest turns, and drops/truncates the middle.
    """

    def __init__(self, max_chars: int = 12000):
        self.max_chars = max_chars
        self.messages: list[dict] = []

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    @staticmethod
    def size(messages: list[dict]) -> int:
        return sum(len(m.get("content", "")) for m in messages)

    def total_chars(self) -> int:
        return self.size(self.messages)

    def build(self) -> list[dict]:
        return self.compact(self.messages)

    def compact(self, messages: list[dict]) -> list[dict]:
        if self.size(messages) <= self.max_chars:
            return list(messages)
        pinned = [m for m in messages[:2] if m.get("role") in ("system", "user")]
        rest = messages[len(pinned):]
        budget = self.max_chars - self.size(pinned)
        kept: list[dict] = []
        for m in reversed(rest):
            if budget <= 200:
                break
            c = m.get("content", "")
            if len(c) > budget:
                c = "...[compacted by governor]...\n" + c[-max(0, budget - 40):]
            kept.append({**m, "content": c})
            budget -= len(c)
        dropped = len(rest) - len(kept)
        note = [{"role": "user", "content": f"[{dropped} earlier turn(s) removed by context compaction]"}] if dropped else []
        return pinned + note + list(reversed(kept))
