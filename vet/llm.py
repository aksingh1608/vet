"""Thin wrapper over the Anthropic SDK: structured output validated by pydantic, one repair
retry, token logging to the current trace.

Determinism: the current models accept no sampling parameters (no temperature), so identical
calls can differ. Answers are cached instead (``LLMCache``, keyed by model, effort, prompt,
input and output schema; no keys or headers): the same input always gets the same answer, so
a rerun of a product is reproducible. Turn it off with VET_LLM_CACHE=false.

Structured output uses ``messages.parse(output_format=...)`` (forced tool_choice is rejected
by current models). Outside content (vendor pages, search snippets, register text, and facts
derived from them) always goes in the user message inside <untrusted_content> tags, and every
system prompt says that content is data, never instructions.
"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
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
MAX_TOKENS_CEILING = 16000  # a cut-off answer is retried once with twice the budget, up to this
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


class LLMCache:
    """Validated structured answers in SQLite (same file as the fetch cache, own table)."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS llm_responses (
            key TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            response TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(self._SCHEMA)

    @staticmethod
    def key(model: str, effort: str, system: str, user: str, output: type[BaseModel]) -> str:
        blob = json.dumps([model, effort, system, user, output.model_json_schema()], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> str | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT response FROM llm_responses WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def put(self, key: str, step: str, response: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_responses (key, step, response, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key, step, response, datetime.now(UTC).isoformat()),
            )


class LLM:
    def __init__(
        self,
        settings: Settings,
        client: anthropic.AsyncAnthropic | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        if client is None:
            if settings.anthropic_api_key is None:
                raise ValueError("ANTHROPIC_API_KEY is not set")
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                timeout=LLM_TIMEOUT_S,
                max_retries=LLM_MAX_RETRIES,
            )
        self._client = client
        self._cache = cache
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
        key = LLMCache.key(model, EFFORT[kind], system, user, output) if self._cache else ""
        if self._cache is not None:
            hit = await asyncio.to_thread(self._cache.get, key)
            if hit is not None:
                current = trace.current()
                if current is not None:
                    current.usage.llm_cache_hits += 1
                trace.emit({"event": "llm", "step": step, "model": model, "cache": "hit"})
                return output.model_validate_json(hit)
        result = await self._ask(kind, model, system, user, output, step, max_tokens)
        if self._cache is not None:
            await asyncio.to_thread(self._cache.put, key, step, result.model_dump_json())
        return result

    async def _ask(
        self,
        kind: ModelKind,
        model: str,
        system: str,
        user: str,
        output: type[T],
        step: str,
        max_tokens: int,
    ) -> T:
        messages: list[dict[str, str]] = [{"role": "user", "content": user}]
        last_error = ""
        budget = max_tokens
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
            try:
                response = await self._client.messages.parse(
                    model=model,
                    max_tokens=budget,
                    system=system,
                    messages=messages,
                    output_format=output,
                    output_config={"effort": EFFORT[kind]},
                )
            except ValidationError as exc:
                # The SDK parses the JSON itself; a cut-off answer fails here, before we can
                # look at stop_reason. Retry once with a larger budget and a shorter answer.
                logger.warning("%s: structured output did not parse (attempt %d)", step, attempt)
                trace.emit({"event": "llm_parse_error", "step": step, "attempt": attempt})
                last_error = f"the answer was not valid JSON ({exc.errors()[0]['msg']}); be concise"
                budget = min(budget * 2, MAX_TOKENS_CEILING)
                continue
            self._log_usage(step, model, attempt, response)
            if response.stop_reason == "refusal":
                raise LLMError(f"{step}: model refused")
            if response.stop_reason == "max_tokens":
                last_error = "the answer was cut off at max_tokens; be more concise"
                budget = min(budget * 2, MAX_TOKENS_CEILING)
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
