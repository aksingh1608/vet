import json

from pydantic import BaseModel

from tests.conftest import FakeLLM
from vet.collectors.classify import classify_in_chunks
from vet.llm import LLMError, unwrap
from vet.tavily import TavilyResult


class _Item(BaseModel):
    url: str


class _Out(BaseModel):
    items: list[_Item]


async def test_one_failing_chunk_does_not_lose_the_other_results() -> None:
    results = [TavilyResult(url=f"https://r{i}.example", content="c") for i in range(14)]
    poison = "https://r7.example"  # e.g. content that makes the model refuse mid-answer

    def respond(user: str) -> _Out:
        urls = [r["url"] for r in json.loads(unwrap(user))["results"]]
        if poison in urls:
            raise LLMError("classify_risk: invalid structured output after repair")
        return _Out(items=[_Item(url=u) for u in urls])

    llm = FakeLLM({_Out: respond})
    items = await classify_in_chunks(
        llm,
        prompt="risk_results.md",
        vendor={"name": "x"},
        results=results,
        output=_Out,
        step="classify_risk",
        snippet_chars=100,
        chunk_size=6,
    )
    got = {i.url for i in items}
    assert len(llm.calls) == 3  # 6 + 6 + 2
    assert poison not in got and len(got) == 8  # only the poisoned chunk of 6 is lost
    assert "https://r0.example" in got and "https://r13.example" in got
