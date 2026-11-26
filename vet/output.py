"""Run outputs: the verdict file we submit (verdicts.json or verdicts.csv, set by
``Settings.output_format``), one JSON per product, trace.jsonl.

The verdict-file format lives behind write_verdict_file / validate_verdict_file only, so it
can change to the organiser's format in one place. Labels are mapped to the organiser's
labels here and nowhere else.

CSV layout: one row per product with product_url, real_alive, trust, confidence, summary,
reason_1_text, reason_1_url ... reason_7_text, reason_7_url, findings ("text (url)" joined
with " | ").
"""

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from vet.collectors.base import CollectorResult, SiteIdentity
from vet.config import Settings
from vet.schemas import (
    MAX_REASONS,
    MIN_REASONS,
    Claim,
    ClaimCheck,
    Fact,
    RealAlive,
    Trust,
    Verdict,
)
from vet.trace import Trace, Usage
from vet.verify import VerifyResult

VERDICTS_FILE = "verdicts.json"
VERDICTS_CSV = "verdicts.csv"
CSV_FIELDS = [
    "product_url",
    "real_alive",
    "trust",
    "confidence",
    "summary",
    *[f"reason_{i}_{part}" for i in range(1, MAX_REASONS + 1) for part in ("text", "url")],
    "findings",
]
TRACE_FILE = "trace.jsonl"
PRODUCTS_DIR = "products"


class ProductRecord(BaseModel):
    """Everything about one product's run, for the report and the eval harness."""

    verdict: Verdict
    identity: SiteIdentity
    claims: list[Claim]
    claim_checks: list[ClaimCheck] = Field(default_factory=list)
    facts: list[Fact]
    collectors: list[CollectorResult]
    verification: list[VerifyResult]
    judge_notes: list[str]
    selection_notes: list[str] = Field(default_factory=list)
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


def verdicts_filename(settings: Settings) -> str:
    return VERDICTS_CSV if settings.output_format == "csv" else VERDICTS_FILE


def find_verdict_file(run_dir: Path) -> Path:
    """The verdict file in a run directory, whichever format it was written in."""
    for name in (VERDICTS_FILE, VERDICTS_CSV):
        if (run_dir / name).exists():
            return run_dir / name
    return run_dir / VERDICTS_FILE


def verdict_to_row(verdict: Verdict, settings: Settings) -> dict[str, str]:
    row = {
        "product_url": verdict.product_url,
        "real_alive": settings.real_alive_labels[verdict.real_alive],
        "trust": settings.trust_labels[verdict.trust],
        "confidence": f"{verdict.confidence:.2f}",
        "summary": verdict.summary,
        "findings": " | ".join(f"{f.text} ({f.source_url})" for f in verdict.findings),
    }
    for i in range(1, MAX_REASONS + 1):
        reason = verdict.reasons[i - 1] if i <= len(verdict.reasons) else None
        row[f"reason_{i}_text"] = reason.text if reason else ""
        row[f"reason_{i}_url"] = reason.source_url if reason else ""
    return row


def write_verdict_file(verdicts: list[Verdict], path: Path, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(verdict_to_row(v, settings) for v in verdicts)
        path.write_text(buf.getvalue(), encoding="utf-8")
        return
    payload = [verdict_to_output(v, settings) for v in verdicts]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def validate_csv_file(path: Path, settings: Settings) -> list[str]:
    try:
        rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]
    if not rows:
        return ["no verdict rows"]
    missing = [c for c in CSV_FIELDS if c not in rows[0]]
    if missing:
        return [f"missing columns: {', '.join(missing)}"]
    ra_labels = set(settings.real_alive_labels.values())
    tr_labels = set(settings.trust_labels.values())
    problems: list[str] = []
    for i, row in enumerate(rows):
        where = f"row {i + 1} ({row['product_url'] or '?'})"
        if row["real_alive"] not in ra_labels:
            problems.append(f"{where}: unknown real_alive label {row['real_alive']!r}")
        if row["trust"] not in tr_labels:
            problems.append(f"{where}: unknown trust label {row['trust']!r}")
        texts = [row[f"reason_{j}_text"] for j in range(1, MAX_REASONS + 1)]
        urls = [row[f"reason_{j}_url"] for j in range(1, MAX_REASONS + 1)]
        count = sum(1 for t in texts if t)
        if not MIN_REASONS <= count <= MAX_REASONS:
            problems.append(f"{where}: {count} reasons (need {MIN_REASONS}-{MAX_REASONS})")
        for j, (text, url) in enumerate(zip(texts, urls, strict=True), start=1):
            if text and not url.startswith(("http://", "https://")):
                problems.append(f"{where}: reason {j} has no openable link")
    return problems


def validate_verdict_file(path: Path, settings: Settings) -> list[str]:
    """Problems found in a verdict file (JSON or CSV); empty when it is valid."""
    if path.suffix == ".csv":
        return validate_csv_file(path, settings)
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
        for j, finding in enumerate(verdict.findings):
            if not finding.source_url.startswith(("http://", "https://")):
                problems.append(f"{where}: finding {j} has no openable link")
    return problems


def write_run(
    run_dir: Path, records: list[ProductRecord], traces: list[Trace], settings: Settings
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    verdicts_path = run_dir / verdicts_filename(settings)
    for name in (VERDICTS_FILE, VERDICTS_CSV):  # never leave two submissions in one run
        if name != verdicts_path.name:
            (run_dir / name).unlink(missing_ok=True)
    write_verdict_file([r.verdict for r in records], verdicts_path, settings)
    products = run_dir / PRODUCTS_DIR
    products.mkdir(exist_ok=True)
    for record in records:
        (products / f"{product_slug(record.verdict.product_url)}.json").write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )
    trace_path = run_dir / TRACE_FILE
    for t in traces:
        t.write_jsonl(trace_path)
    return verdicts_path


def load_records(run_dir: Path) -> list[ProductRecord]:
    return [
        ProductRecord.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted((run_dir / PRODUCTS_DIR).glob("*.json"))
    ]
