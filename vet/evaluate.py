"""Eval harness: run the agent on labelled products and write eval.md.

Labels come from data/labelled/ (the organiser's examples, any CSV/JSON/YAML; gitignored) and
eval/extra_set.yaml (our own set). Products without any label are skipped. eval.md holds
accuracy per axis, a confusion table per axis, and a misses table with a suspected cause:

* missing signal: a collector failed, or too few verified reasons / independent facts
* rule clamp: the deterministic rules ruled out the expected label (named in the judge notes)
* weak evidence: low confidence, the call could go either way
* wrong weight: the evidence was there, but it was weighed differently
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from vet.labels import Label
from vet.output import ProductRecord
from vet.schemas import RealAlive, SourceKind, Trust
from vet.text import registered_domain

logger = logging.getLogger(__name__)

EVAL_FILE = "eval.md"
LOW_CONFIDENCE = 0.6
MIN_INDEPENDENT_FACTS = 3
TOP_REASONS_SHOWN = 2


@dataclass
class Miss:
    product: str
    axis: str
    ours: str
    theirs: str
    our_reasons: list[str]
    their_reasons: list[str]
    cause: str


@dataclass
class AxisScore:
    axis: str
    labels: list[str]
    total: int = 0
    correct: int = 0
    confusion: dict[tuple[str, str], int] = field(default_factory=dict)  # (theirs, ours)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class EvalResult:
    real_alive: AxisScore
    trust: AxisScore
    misses: list[Miss]
    unmatched: list[str]  # labelled products with no record


def match_key(url: str) -> str:
    return registered_domain(url)


def suspected_cause(record: ProductRecord, axis: str, theirs: str) -> str:
    errors = [c.name for c in record.collectors if c.error]
    independent = [f for f in record.facts if f.source_kind != SourceKind.VENDOR]
    if errors:
        return f"missing signal: {', '.join(errors)} failed"
    if len(record.verdict.reasons) < 4 or len(independent) < MIN_INDEPENDENT_FACTS:
        return f"missing signal: only {len(independent)} record/independent facts"
    clamps = [
        n
        for n in record.judge_notes
        if ("not allowed" in n or "capped" in n or "floor" in n) and axis.split("_")[0] in n
    ]
    if clamps:
        return f"rule clamp: {clamps[0]} (expected {theirs})"
    if record.verdict.confidence < LOW_CONFIDENCE:
        return f"weak evidence: confidence {record.verdict.confidence:.2f}"
    return "wrong weight: evidence found but weighed differently"


def score(records: list[ProductRecord], labels: list[Label]) -> EvalResult:
    by_key = {match_key(r.verdict.product_url): r for r in records}
    ra = AxisScore("real_alive", [v.value for v in RealAlive])
    tr = AxisScore("trust", [v.value for v in Trust])
    misses: list[Miss] = []
    unmatched: list[str] = []
    for label in labels:
        record = by_key.get(match_key(label.url))
        if record is None:
            unmatched.append(label.url)
            continue
        v = record.verdict
        for axis_score, theirs, ours in (
            (ra, label.real_alive, v.real_alive),
            (tr, label.trust, v.trust),
        ):
            if theirs is None:
                continue
            axis_score.total += 1
            key = (theirs.value, ours.value)
            axis_score.confusion[key] = axis_score.confusion.get(key, 0) + 1
            if theirs == ours:
                axis_score.correct += 1
                continue
            misses.append(
                Miss(
                    product=v.product_url,
                    axis=axis_score.axis,
                    ours=ours.value,
                    theirs=theirs.value,
                    our_reasons=[r.text for r in v.reasons[:TOP_REASONS_SHOWN]],
                    their_reasons=label.reasons[:TOP_REASONS_SHOWN],
                    cause=suspected_cause(record, axis_score.axis, theirs.value),
                )
            )
    return EvalResult(real_alive=ra, trust=tr, misses=misses, unmatched=unmatched)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _confusion(axis: AxisScore) -> list[str]:
    lines = [
        f"| theirs \\ ours | {' | '.join(axis.labels)} |",
        "|---|" + "---|" * len(axis.labels),
    ]
    for theirs in axis.labels:
        counts = [str(axis.confusion.get((theirs, ours), 0)) for ours in axis.labels]
        lines.append(f"| **{theirs}** | {' | '.join(counts)} |")
    return lines


def render_markdown(result: EvalResult, run_id: str, label_sources: list[str]) -> str:
    lines = [f"# Eval {run_id}", "", "Labels: " + ", ".join(label_sources), ""]
    lines += ["## Accuracy", "", "| Axis | Correct | Labelled | Accuracy |", "|---|---|---|---|"]
    for axis in (result.real_alive, result.trust):
        lines.append(f"| {axis.axis} | {axis.correct} | {axis.total} | {axis.accuracy:.0%} |")
    for axis in (result.real_alive, result.trust):
        lines += ["", f"## Confusion: {axis.axis}", "", *_confusion(axis)]
    lines += [
        "",
        "## Misses",
        "",
        "| Product | Axis | Ours | Theirs | Our top reasons | Their reasons | Suspected cause |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in result.misses:
        lines.append(
            f"| {m.product} | {m.axis} | {m.ours} | {m.theirs} | "
            f"{_cell('<br>'.join(m.our_reasons))} | {_cell('<br>'.join(m.their_reasons))} | "
            f"{_cell(m.cause)} |"
        )
    if not result.misses:
        lines.append("| (none) | | | | | | |")
    if result.unmatched:
        lines += ["", "## Not run", "", *[f"- {u}" for u in result.unmatched]]
    return "\n".join(lines) + "\n"


def write_eval(run_dir: Path, result: EvalResult, label_sources: list[str]) -> Path:
    path = run_dir / EVAL_FILE
    path.write_text(render_markdown(result, run_dir.name, label_sources), encoding="utf-8")
    return path
