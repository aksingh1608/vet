"""Per-product trace. Each product run sets the current Trace in a ContextVar, so shared
components (fetcher, LLM, Tavily) can log to the right product without passing it around."""

import json
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Usage(BaseModel):
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_calls: int = 0
    tavily_searches: int = 0
    tavily_cache_hits: int = 0
    tavily_credits: int = 0
    fetches: int = 0
    fetch_cache_hits: int = 0


class Trace(BaseModel):
    product_url: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    events: list[dict[str, Any]] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    _t0: float = 0.0

    def model_post_init(self, __context: Any) -> None:
        self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append({"t": self.elapsed(), **event})

    def write_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for event in self.events:
                fh.write(json.dumps({"product": self.product_url, **event}, default=str) + "\n")


_current: ContextVar[Trace | None] = ContextVar("vet_trace", default=None)


def current() -> Trace | None:
    return _current.get()


def set_current(trace: Trace | None) -> None:
    _current.set(trace)


def emit(event: dict[str, Any]) -> None:
    trace = _current.get()
    if trace is not None:
        trace.emit(event)
