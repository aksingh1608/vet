"""LLM wrapper: a cut-off structured answer is retried with a larger budget, never raised raw."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from vet.config import Settings
from vet.llm import DEFAULT_MAX_TOKENS, LLM, LLMError


class _Out(BaseModel):
    items: list[str]


def _parse_error() -> ValidationError:
    try:
        _Out.model_validate_json('{"items": ["a", "b')  # cut off mid-answer
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


class _Messages:
    def __init__(self, answers: list[object]) -> None:
        self.answers = answers
        self.budgets: list[int] = []

    async def parse(self, **kw: object) -> object:
        self.budgets.append(int(kw["max_tokens"]))  # type: ignore[call-overload]
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _llm(answers: list[object]) -> tuple[LLM, _Messages]:
    messages = _Messages(answers)
    client = SimpleNamespace(messages=messages)
    return LLM(Settings(), client=client), messages  # type: ignore[arg-type]


def _ok(items: list[str]) -> object:
    return SimpleNamespace(
        stop_reason="end_turn",
        parsed_output=_Out(items=items),
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


async def test_cut_off_json_is_retried_with_twice_the_budget() -> None:
    llm, messages = _llm([_parse_error(), _ok(["a", "b"])])
    out = await llm.structured(kind="fast", system="s", user="u", output=_Out, step="classify")
    assert out.items == ["a", "b"]
    assert messages.budgets == [DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOKENS * 2]


async def test_two_cut_off_answers_raise_llm_error_not_validation_error() -> None:
    llm, _ = _llm([_parse_error(), _parse_error()])
    with pytest.raises(LLMError, match="not valid JSON"):
        await llm.structured(kind="fast", system="s", user="u", output=_Out, step="classify")


async def test_identical_calls_hit_the_cache_and_return_the_same_answer(tmp_path) -> None:
    from vet.llm import LLMCache

    messages = _Messages([_ok(["first"]), _ok(["second"])])
    client = SimpleNamespace(messages=messages)
    llm = LLM(Settings(), client=client, cache=LLMCache(tmp_path / "c.sqlite"))  # type: ignore[arg-type]
    a = await llm.structured(kind="fast", system="s", user="u", output=_Out, step="x")
    b = await llm.structured(kind="fast", system="s", user="u", output=_Out, step="x")
    c = await llm.structured(kind="fast", system="s", user="other", output=_Out, step="x")
    assert a.items == b.items == ["first"]  # the second identical call never reached the API
    assert c.items == ["second"]  # a different input is a different call
    assert len(messages.budgets) == 2


async def test_failed_calls_are_not_cached(tmp_path) -> None:
    from vet.llm import LLMCache

    llm, messages = _llm([_parse_error(), _parse_error(), _ok(["later"])])
    llm._cache = LLMCache(tmp_path / "c.sqlite")
    with pytest.raises(LLMError):
        await llm.structured(kind="fast", system="s", user="u", output=_Out, step="x")
    out = await llm.structured(kind="fast", system="s", user="u", output=_Out, step="x")
    assert out.items == ["later"]  # the failure was not remembered
