"""Load labelled products from the organiser's file (CSV or JSON) or our own YAML set.

Column names are configurable (``Settings.label_columns``, env ``VET_LABEL_COLUMNS``), and a
column may be a dotted path into nested JSON ("labels.trust"). Label values are read
leniently: our internal names, the organiser's names from ``real_alive_labels`` /
``trust_labels``, and common synonyms. An empty label means "not labelled yet": the product is
loaded but not scored on that axis.
"""

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from vet.config import Settings
from vet.schemas import Overall, RealAlive, Trust

logger = logging.getLogger(__name__)

LABEL_SUFFIXES = (".csv", ".json", ".yaml", ".yml")
REAL_ALIVE_SYNONYMS: dict[str, RealAlive] = {
    "yes": RealAlive.YES,
    "true": RealAlive.YES,
    "real": RealAlive.YES,
    "alive": RealAlive.YES,
    "1": RealAlive.YES,
    "unclear": RealAlive.UNCLEAR,
    "unknown": RealAlive.UNCLEAR,
    "maybe": RealAlive.UNCLEAR,
    "no": RealAlive.NO,
    "false": RealAlive.NO,
    "fake": RealAlive.NO,
    "dead": RealAlive.NO,
    "0": RealAlive.NO,
}
TRUST_SYNONYMS: dict[str, Trust] = {
    "trust": Trust.TRUST,
    "trusted": Trust.TRUST,
    "yes": Trust.TRUST,
    "high": Trust.TRUST,
    "caution": Trust.CAUTION,
    "medium": Trust.CAUTION,
    "maybe": Trust.CAUTION,
    "do_not_trust": Trust.DO_NOT_TRUST,
    "do not trust": Trust.DO_NOT_TRUST,
    "dont_trust": Trust.DO_NOT_TRUST,
    "untrusted": Trust.DO_NOT_TRUST,
    "no": Trust.DO_NOT_TRUST,
    "low": Trust.DO_NOT_TRUST,
}
OVERALL_SYNONYMS: dict[str, Overall] = {
    "green": Overall.GREEN,
    "yellow": Overall.YELLOW,
    "amber": Overall.YELLOW,
    "orange": Overall.YELLOW,
    "red": Overall.RED,
}


class LabelError(ValueError):
    """A label file or value could not be read."""


class Label(BaseModel):
    url: str
    real_alive: RealAlive | None = None
    trust: Trust | None = None
    overall: Overall | None = None
    id: str = ""
    product: str = ""
    vendor: str = ""
    reasons: list[str] = Field(default_factory=list)
    # The organiser's flags per colour ("red", "yellow", "green"), for side-by-side misses.
    flags: dict[str, str] = Field(default_factory=dict)
    note: str = ""
    source: str = ""  # file it came from

    @property
    def labelled(self) -> bool:
        return self.real_alive is not None or self.trust is not None or self.overall is not None


def _get(row: dict[str, Any], path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_") if value is not None else ""


def parse_real_alive(value: Any, settings: Settings) -> RealAlive | None:
    key = _key(value)
    if not key or key in ("none", "null", "todo", "?"):
        return None
    theirs = {_key(v): k for k, v in settings.real_alive_labels.items()}
    found = (
        theirs.get(key)
        or REAL_ALIVE_SYNONYMS.get(key)
        or REAL_ALIVE_SYNONYMS.get(key.replace("_", " "))
    )
    if found is None:
        raise LabelError(f"unknown real_alive label {value!r}")
    return RealAlive(found)


def parse_trust(value: Any, settings: Settings) -> Trust | None:
    key = _key(value)
    if not key or key in ("none", "null", "todo", "?"):
        return None
    theirs = {_key(v): k for k, v in settings.trust_labels.items()}
    found = theirs.get(key) or TRUST_SYNONYMS.get(key) or TRUST_SYNONYMS.get(key.replace("_", " "))
    if found is None:
        raise LabelError(f"unknown trust label {value!r}")
    return Trust(found)


def parse_overall(value: Any, settings: Settings) -> Overall | None:
    key = _key(value)
    if not key or key in ("none", "null", "todo", "?"):
        return None
    theirs = {_key(v): k for k, v in settings.overall_labels.items()}
    found = theirs.get(key) or OVERALL_SYNONYMS.get(key)
    if found is None:
        raise LabelError(f"unknown overall label {value!r}")
    return Overall(found)


def _reasons(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    parts = str(value).replace("\n", "|").split("|")
    return [p.strip() for p in parts if p.strip()]


def _rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".csv":
        return [dict(r) for r in csv.DictReader(io.StringIO(text, newline=""))]
    data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    if isinstance(data, dict):  # {"products": [...]} or any single list value
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if len(lists) == 1 else data.get("products", [])
    if not isinstance(data, list):
        raise LabelError(f"{path}: expected a list of products")
    return [r for r in data if isinstance(r, dict)]


def load_labels(path: Path, settings: Settings) -> list[Label]:
    """Every row with a URL. Rows without labels come back unlabelled (placeholders)."""
    cols = settings.label_columns
    out: list[Label] = []
    for i, row in enumerate(_rows(path)):
        url = str(_get(row, cols["url"]) or "").strip()
        if not url:
            continue
        try:
            out.append(
                Label(
                    url=url,
                    real_alive=parse_real_alive(_get(row, cols["real_alive"]), settings),
                    trust=parse_trust(_get(row, cols["trust"]), settings),
                    overall=parse_overall(_get(row, cols.get("overall", "overall")), settings),
                    id=str(_get(row, cols.get("id", "id")) or "").strip(),
                    product=str(_get(row, "product") or "").strip(),
                    vendor=str(_get(row, "vendor") or "").strip(),
                    reasons=_reasons(
                        _get(row, cols.get("reasons", "reasons")) or _get(row, "reasoning")
                    ),
                    flags={
                        colour: str(_get(row, f"{colour}_flags") or "")
                        for colour in ("red", "yellow", "green")
                        if _get(row, f"{colour}_flags")
                    },
                    note=str(_get(row, cols.get("note", "note")) or ""),
                    source=str(path),
                )
            )
        except LabelError as exc:
            raise LabelError(f"{path} row {i + 1}: {exc}") from exc
    logger.info("labels: %d products from %s", len(out), path)
    return out


def label_files(*dirs_or_files: Path) -> list[Path]:
    """Label files under the given directories (or the files themselves), sorted."""
    found: list[Path] = []
    for p in dirs_or_files:
        if p.is_file() and p.suffix in LABEL_SUFFIXES:
            found.append(p)
        elif p.is_dir():
            found += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in LABEL_SUFFIXES)
    return found
