from backend.config import settings
from backend.orchestrator.context_manager import ContextManager


class EmptyGeneration(Exception):
    """The model returned no runnable code (e.g. a reasoning model used its
    whole completion allowance on hidden reasoning)."""


SYSTEM_PROMPT = """You are an autonomous Python coding agent.
You will be given a task and, on later turns, the real stdout/stderr of your previous attempt.
Output ONLY one Python code block that solves the task when executed in an isolated
sandbox with NO network access. No prose.
The sandbox has python3 and the standard library only (pandas is NOT available).
Input files are in /workspace/ (the working directory). Print the final result to stdout.
If the task asks for JSON, print exactly one JSON object as the last line.
Keep any reasoning brief: the code block is the only thing that is executed."""


class CodeGenerator:
    """Builds every prompt through the ContextManager, so the governor's
    context compaction affects the actual model input."""

    def __init__(self, inference, context: ContextManager):
        self.inference = inference
        self.context = context
        self.last_prompt_chars = 0

    def start(self, task: str) -> None:
        if not self.context.messages:
            self.context.add("system", SYSTEM_PROMPT)
            self.context.add("user", f"Task: {task}")

    def record_result(self, code: str, result: dict) -> None:
        self.context.add("assistant", f"```python\n{code}\n```")
        self.context.add("user",
                         f"Exit code: {result.get('exit_code')}\n"
                         f"stdout:\n{(result.get('stdout') or '')[:3000]}\n"
                         f"stderr:\n{(result.get('stderr') or '')[:3000]}\n"
                         "The attempt failed. Fix the code.")

    def record_exception(self, code: str, error: str) -> None:
        self.context.add("assistant", f"```python\n{code}\n```")
        self.context.add("user", f"Sandbox execution error: {error[:2000]}\nFix the code.")

    async def generate(self, model: str | None = None) -> str:
        msgs = self.context.build()
        self.last_prompt_chars = ContextManager.size(msgs)
        response = await self.inference.chat(
            model=model or settings.vultr_model_default,
            messages=msgs,
            max_tokens=settings.agent_max_completion_tokens,
        )
        msg = (response.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        code = self._extract_code(content) if content.strip() else ""
        if not code.strip():
            # Some reasoning models put the answer in a separate field.
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            if "```" in reasoning:
                code = self._extract_code(reasoning)
        if not code.strip():
            finish = (response.get("choices") or [{}])[0].get("finish_reason")
            raise EmptyGeneration(f"model returned no code (finish_reason={finish}, "
                                  f"content_chars={len(content)})")
        return code

    def record_empty(self, detail: str) -> None:
        self.context.add("assistant", "(no code returned)")
        self.context.add("user", f"Your previous reply contained no Python code block ({detail}). "
                                 "Reply with ONLY one ```python code block, with minimal reasoning.")

    @staticmethod
    def _extract_code(text: str) -> str:
        if "```" in text:
            parts = text.split("```")
            for part in parts[1::2]:
                s = part.strip()
                for tag in ("python3", "python", "py"):
                    if s.startswith(tag):
                        return s[len(tag):].lstrip("\n")
                return s
        return text.strip()
