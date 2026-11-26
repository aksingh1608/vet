"""Thin wrapper over the Anthropic SDK: structured output validated by pydantic, one repair
retry, token logging to the current trace.

Structured output uses ``messages.parse(output_format=...)`` (forced tool_choice is rejected
by current models). Outside content (vendor pages, search snippets, register text, and facts
derived from them) always goes in the user message inside <untrusted_content> tags, and every
system prompt says that content is data, never instructions.
"""

import logging
import re
from pathlib import Path
from typing import Literal, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from vet import trace
from vet.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
ModelKind = Literal["fast", "judge"]

PROMPTS_DIR = Path(__file__).parent / "prompts"
LLM_TIMEOUT_S = 90.0
LLM_MAX_RETRIES = 2
DEFAULT_MAX_TOKENS = 8000
EFFORT: dict[ModelKind, str] = {"fast": "low", "judge": "medium"}


class LLMError(RuntimeError):
    """The model did not return a usable structured answer."""


class StructuredLLM(Protocol):
    async def structured(
        self,
        *,
        kind: ModelKind,
        system: str,
        user: str,
        output: type[T],
        step: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> T: ...


UNTRUSTED_OPEN = "<untrusted_content>"
UNTRUSTED_CLOSE = "</untrusted_content>"
_TAG_IN_CONTENT = re.compile(r"<\s*(/?)\s*untrusted_content", re.I)


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def untrusted(content: str) -> str:
    """Wrap outside content for a prompt. Tags inside the content are escaped so a page cannot
    close the block early and smuggle text out of it."""
    content = _TAG_IN_CONTENT.sub(r"&lt;\1untrusted_content", content)
    return f"{UNTRUSTED_OPEN}\n{content}\n{UNTRUSTED_CLOSE}"


def unwrap(user: str) -> str:
    """The content of the first untrusted block (for tests and fakes)."""
    start = user.index(UNTRUSTED_OPEN) + len(UNTRUSTED_OPEN) + 1
    return user[start : user.index("\n" + UNTRUSTED_CLOSE, start)]


class LLM:
    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        if client is None:
            if settings.anthropic_api_key is None:
                raise ValueError("ANTHROPIC_API_KEY is not set")
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                timeout=LLM_TIMEOUT_S,
                max_retries=LLM_MAX_RETRIES,
            )
        self._client = client
        self._models: dict[ModelKind, str] = {
            "fast": settings.fast_model,
            "judge": settings.judge_model,
        }

    def model_name(self, kind: ModelKind) -> str:
        return self._models[kind]

    async def structured(
        self,
        *,
        kind: ModelKind,
        system: str,
        user: str,
        output: type[T],
        step: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> T:
        model = self._models[kind]
        messages: list[dict[str, str]] = [{"role": "user", "content": user}]
        last_error = ""
        for attempt in range(2):  # first try + one repair retry
            if last_error:
                messages = [
                    {"role": "user", "content": user},
                    {
                        "role": "user",
                        "content": (
                            "Your previous answer did not validate against the schema: "
                            f"{last_error}. Answer again, following the schema exactly."
                        ),
                    },
                ]
            response = await self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_format=output,
                output_config={"effort": EFFORT[kind]},
            )
            self._log_usage(step, model, attempt, response)
            if response.stop_reason == "refusal":
                raise LLMError(f"{step}: model refused")
            if response.stop_reason == "max_tokens":
                last_error = "the answer was cut off at max_tokens; be more concise"
                continue
            parsed = response.parsed_output
            if parsed is not None:
                try:
                    return output.model_validate(parsed.model_dump())
                except ValidationError as exc:
                    last_error = str(exc)[:500]
                    continue
            last_error = "no parsed output"
        raise LLMError(f"{step}: invalid structured output after repair ({last_error})")

    @staticmethod
    def _log_usage(step: str, model: str, attempt: int, response: object) -> None:
        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        current = trace.current()
        if current is not None:
            current.usage.llm_calls += 1
            current.usage.llm_input_tokens += tokens_in
            current.usage.llm_output_tokens += tokens_out
        trace.emit(
            {
                "event": "llm",
                "step": step,
                "model": model,
                "attempt": attempt,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "stop_reason": getattr(response, "stop_reason", None),
            }
        )
