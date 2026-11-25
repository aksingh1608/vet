"""Run outputs: verdicts.json (the file we submit), one JSON per product, trace.jsonl.

The verdict-file format lives behind write_verdict_file / validate_verdict_file only, so it
can change to the organiser's format in one place. Labels are mapped to the organiser's
labels here and nowhere else.
"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from vet.collectors.base import CollectorResult, SiteIdentity
from vet.config import Settings
from vet.schemas import MAX_REASONS, MIN_REASONS, Claim, Fact, RealAlive, Trust, Verdict
from vet.trace import Trace, Usage
from vet.verify import VerifyResult

VERDICTS_FILE = "verdicts.json"
TRACE_FILE = "trace.jsonl"
PRODUCTS_DIR = "products"


class ProductRecord(BaseModel):
    """Everything about one product's run, for the report and the eval harness."""

    verdict: Verdict
    identity: SiteIdentity
    claims: list[Claim]
    facts: list[Fact]
    collectors: list[CollectorResult]
    verification: list[VerifyResult]
    judge_notes: list[str]
    used_llm_judge: bool
    usage: Usage
    seconds: float
    errors: list[str]


def product_slug(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return re.sub(r"[^a-z0-9.-]+", "-", host.lower()).strip("-") or "product"


def verdict_to_output(verdict: Verdict, settings: Settings) -> dict[str, Any]:
    data = verdict.model_dump(mode="json")
    data["real_alive"] = settings.real_alive_labels[verdict.real_alive]
    data["trust"] = settings.trust_labels[verdict.trust]
    return data


def write_verdict_file(verdicts: list[Verdict], path: Path, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [verdict_to_output(v, settings) for v in verdicts]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def validate_verdict_file(path: Path, settings: Settings) -> list[str]:
    """Problems found in a verdict file; empty when it is valid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read {path}: {exc}"]
    if not isinstance(data, list):
        return ["top level must be a list of verdicts"]
    ra_back = {v: k for k, v in settings.real_alive_labels.items()}
    tr_back = {v: k for k, v in settings.trust_labels.items()}
    problems: list[str] = []
    for i, item in enumerate(data):
        where = f"verdict {i} ({item.get('product_url', '?') if isinstance(item, dict) else '?'})"
        if not isinstance(item, dict):
            problems.append(f"{where}: not an object")
            continue
        internal = dict(item)
        if internal.get("real_alive") not in ra_back:
            problems.append(f"{where}: unknown real_alive label {internal.get('real_alive')!r}")
            continue
        if internal.get("trust") not in tr_back:
            problems.append(f"{where}: unknown trust label {internal.get('trust')!r}")
            continue
        internal["real_alive"] = RealAlive(ra_back[internal["real_alive"]])
        internal["trust"] = Trust(tr_back[internal["trust"]])
        try:
            verdict = Verdict.model_validate(internal)
        except ValidationError as exc:
            problems.append(
                f"{where}: {exc.error_count()} schema error(s): {exc.errors()[0]['msg']}"
            )
            continue
        if not MIN_REASONS <= len(verdict.reasons) <= MAX_REASONS:
            problems.append(
                f"{where}: {len(verdict.reasons)} reasons (need {MIN_REASONS}-{MAX_REASONS})"
            )
        for j, reason in enumerate(verdict.reasons):
            if not reason.source_url.startswith(("http://", "https://")):
                problems.append(f"{where}: reason {j} has no openable link")
    return problems


def write_run(
    run_dir: Path, records: list[ProductRecord], traces: list[Trace], settings: Settings
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    write_verdict_file([r.verdict for r in records], run_dir / VERDICTS_FILE, settings)
    products = run_dir / PRODUCTS_DIR
    products.mkdir(exist_ok=True)
    for record in records:
        (products / f"{product_slug(record.verdict.product_url)}.json").write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )
    trace_path = run_dir / TRACE_FILE
    for t in traces:
        t.write_jsonl(trace_path)
    return run_dir / VERDICTS_FILE
