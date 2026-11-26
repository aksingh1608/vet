from tests.test_report import record
from vet.collectors.base import CollectorResult
from vet.evaluate import render_markdown, score
from vet.labels import Label
from vet.schemas import RealAlive, Trust


def test_scores_confusion_and_misses_with_a_cause() -> None:
    ok = record("https://acme.example/")  # yes / caution
    slow = record("https://slow.example/").model_copy(
        update={
            "collectors": [
                CollectorResult(name="footprint", facts=[], seconds=45, error="timed out")
            ]
        }
    )
    labels = [
        Label(url="https://www.acme.example", real_alive=RealAlive.YES, trust=Trust.CAUTION),
        Label(
            url="https://slow.example",
            real_alive=RealAlive.YES,
            trust=Trust.TRUST,
            reasons=["Long history"],
        ),
        Label(url="https://never-run.example", trust=Trust.TRUST),
    ]
    result = score([ok, slow], labels)
    assert (result.real_alive.correct, result.real_alive.total) == (2, 2)
    assert (result.trust.correct, result.trust.total) == (1, 2)
    assert result.trust.confusion[("trust", "caution")] == 1
    (miss,) = result.misses
    assert (miss.ours, miss.theirs) == ("caution", "trust")
    assert miss.cause == "missing signal: footprint failed"
    assert result.unmatched == ["https://never-run.example"]

    md = render_markdown(result, "run-1", ["data/labelled/x.csv"])
    assert "| trust | 1 | 2 | 50% |" in md
    assert "| **trust** | 0 | 1 | 0 |" in md  # theirs=trust: 0 do_not_trust, 1 caution
    assert "missing signal: footprint failed" in md and "Long history" in md
    assert "never-run.example" in md
