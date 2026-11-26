"""Classify search results with the fast model in small chunks.

One result can make the model stop mid-answer (a refusal on sensitive content, a cut-off). With
one big batch that loses every result; in chunks it loses only that chunk. Chunks run in
parallel, and a failed chunk is logged and traced, never raised.
"""

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel

from vet import guard, trace
from vet.llm import LLMError, StructuredLLM, load_prompt, untrusted
from vet.tavily import TavilyResult
from vet.text import truncate

logger = logging.getLogger(__name__)

CHUNK_SIZE = 6


def result_payload(results: list[TavilyResult], snippet_chars: int) -> list[dict[str, str]]:
    return [
        {
            "url": r.url,
            "title": guard.scrub_text(r.title),
            "content": guard.scrub_text(truncate(r.content, snippet_chars)),
        }
        for r in results
    ]


async def classify_in_chunks[T: BaseModel](
    llm: StructuredLLM,
    *,
    prompt: str,
    vendor: dict[str, str],
    results: list[TavilyResult],
    output: type[T],
    step: str,
    snippet_chars: int,
    chunk_size: int = CHUNK_SIZE,
) -> list[Any]:
    """Items from every chunk that answered; failed chunks are skipped."""
    chunks = [results[i : i + chunk_size] for i in range(0, len(results), chunk_size)]
    system = load_prompt(prompt)

    async def one(chunk: list[TavilyResult]) -> list[Any]:
        payload = {"vendor": vendor, "results": result_payload(chunk, snippet_chars)}
        out = await llm.structured(
            kind="fast",
            system=system,
            user=untrusted(json.dumps(payload, ensure_ascii=False)),
            output=output,
            step=step,
        )
        return list(getattr(out, "items", []))

    answers = await asyncio.gather(*(one(c) for c in chunks), return_exceptions=True)
    items: list[Any] = []
    for chunk, answer in zip(chunks, answers, strict=True):
        if isinstance(answer, LLMError):
            logger.warning("%s: a chunk of %d results failed: %s", step, len(chunk), answer)
            trace.emit(
                {
                    "event": "classify_chunk_failed",
                    "step": step,
                    "urls": [r.url for r in chunk],
                    "error": str(answer)[:200],
                }
            )
            continue
        if isinstance(answer, BaseException):
            raise answer
        items += answer
    return items
