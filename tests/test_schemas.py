from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from vet.schemas import (
    Axis,
    Claim,
    ClaimCheck,
    Fact,
    Finding,
    Polarity,
    RealAlive,
    Reason,
    SourceKind,
    Trust,
    Verdict,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def make_fact(**overrides: object) -> Fact:
    data: dict[str, object] = {
        "id": "age-1",
        "collector": "age",
        "statement": "Earliest archived copy of example.com is from 2025-10-02",
        "axis": Axis.REAL_ALIVE,
        "polarity": Polarity.NEGATIVE,
        "weight": 0.6,
        "source_url": "https://web.archive.org/web/20251002000000/https://example.com/",
        "quote": "",
        "source_kind": SourceKind.RECORD,
        "fetched_at": NOW,
    }
    data.update(overrides)
    return Fact.model_validate(data)


def make_reason(i: int = 0) -> Reason:
    return Reason(
        text=f"Reason {i}",
        axis=Axis.TRUST,
        polarity=Polarity.POSITIVE,
        source_url="https://example.org/review",
        quote="has used it since 2009",
        fact_id=f"fp-{i}",
    )


def make_verdict(n_reasons: int = 4) -> Verdict:
    return Verdict(
        product_url="https://example.com",
        real_alive=RealAlive.YES,
        trust=Trust.CAUTION,
        confidence=0.7,
        summary="Real product, but a 2023 breach is not disclosed.",
        reasons=[make_reason(i) for i in range(n_reasons)],
        findings=[
            Finding(
                text="The site says the company was founded in 2012; the domain was "
                "registered on 2024-03-01",
                source_url="https://example.com/about",
                fact_id="claims_check-0",
            )
        ],
        run_id="run-1",
        git_sha="abc123",
        model="claude-opus-5-5",
        created_at=NOW,
    )


@pytest.mark.parametrize(
    "model",
    [
        Claim(
            id="c1",
            type="founded_year",
            text="Founded in 2012",
            source_url="https://example.com/about",
            quote="Since 2012 we have...",
        ),
        make_fact(),
        make_fact(is_hard_flag=True, polarity=Polarity.NEGATIVE, axis=Axis.TRUST),
        ClaimCheck(claim_id="c1", status="contradicted", fact_ids=["age-1"], note="2 yrs"),
        make_reason(),
        make_verdict(),
    ],
    ids=lambda m: type(m).__name__,
)
def test_round_trip(model: BaseModel) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


def test_enums_serialise_as_plain_strings() -> None:
    dumped = make_verdict().model_dump(mode="json")
    assert dumped["real_alive"] == "yes"
    assert dumped["trust"] == "caution"
    assert dumped["reasons"][0]["axis"] == "trust"


@pytest.mark.parametrize("weight", [-0.1, 1.1])
def test_fact_weight_bounded(weight: float) -> None:
    with pytest.raises(ValidationError):
        make_fact(weight=weight)


def test_verdict_caps_reasons_at_seven() -> None:
    make_verdict(7)
    with pytest.raises(ValidationError):
        make_verdict(8)


def test_unknown_fields_rejected() -> None:
    data = make_reason().model_dump()
    data["invented_source"] = "https://nowhere"
    with pytest.raises(ValidationError):
        Reason.model_validate(data)


def test_unknown_claim_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Claim(id="c", type="award", text="t", source_url="u", quote="q")
