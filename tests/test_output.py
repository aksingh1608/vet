import json
from pathlib import Path

from tests.test_schemas import make_verdict
from vet.config import Settings
from vet.output import product_slug, validate_verdict_file, write_verdict_file
from vet.schemas import RealAlive, Trust


def test_write_then_validate(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.json"
    settings = Settings()
    write_verdict_file([make_verdict(4), make_verdict(7)], path, settings)
    assert validate_verdict_file(path, settings) == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0]["real_alive"] == "yes" and data[0]["trust"] == "caution"


def test_label_mapping_only_in_writer(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.json"
    settings = Settings(
        real_alive_labels={RealAlive.YES: "REAL", RealAlive.UNCLEAR: "?", RealAlive.NO: "FAKE"},
        trust_labels={Trust.TRUST: "GREEN", Trust.CAUTION: "AMBER", Trust.DO_NOT_TRUST: "RED"},
    )
    write_verdict_file([make_verdict()], path, settings)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data[0]["real_alive"], data[0]["trust"]) == ("REAL", "AMBER")
    assert validate_verdict_file(path, settings) == []
    assert validate_verdict_file(path, Settings())  # unknown labels under default mapping


def test_validate_reports_problems(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.json"
    write_verdict_file([make_verdict(3)], path, Settings())
    problems = validate_verdict_file(path, Settings())
    assert any("3 reasons" in p for p in problems)
    path.write_text("{not json", encoding="utf-8")
    assert validate_verdict_file(path, Settings())


def test_product_slug() -> None:
    assert product_slug("https://www.Example.com/pricing?x=1") == "www.example.com"


def test_csv_output_maps_labels_and_validates(tmp_path, settings) -> None:
    import csv

    from tests.test_report import record
    from vet.output import validate_verdict_file, write_run
    from vet.schemas import RealAlive, Trust
    from vet.trace import Trace

    s = settings.model_copy(
        update={
            "output_format": "csv",
            "real_alive_labels": {RealAlive.YES: "real", RealAlive.UNCLEAR: "?", RealAlive.NO: "x"},
            "trust_labels": {Trust.TRUST: "A", Trust.CAUTION: "B", Trust.DO_NOT_TRUST: "C"},
        }
    )
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "verdicts.json").write_text("[]")  # stale file from a JSON run
    path = write_run(tmp_path / "run", [record()], [Trace(product_url="x")], s)
    assert path.name == "verdicts.csv" and not (tmp_path / "run" / "verdicts.json").exists()
    (row,) = csv.DictReader(path.read_text(encoding="utf-8").splitlines())
    assert (row["real_alive"], row["trust"]) == ("real", "B")
    assert row["reason_1_url"].startswith("https://") and row["reason_5_text"] == ""
    assert "javascript:alert(1)" in row["findings"]  # written as-is; the report filters links
    assert validate_verdict_file(path, s) == []
    assert validate_verdict_file(path, settings) != []  # default labels: "real" is unknown


def test_merge_replaces_a_rerun_product_and_keeps_the_rest() -> None:
    from tests.test_report import record
    from vet.cli import _merge

    a, b = record("https://a.example/"), record("https://b.example/")
    b2 = b.model_copy(update={"seconds": 1.0})
    c = record("https://c.example/")
    merged = _merge([a, b], [b2, c])
    assert [r.verdict.product_url for r in merged] == [
        "https://a.example/",
        "https://b.example/",
        "https://c.example/",
    ]
    assert merged[1].seconds == 1.0
