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
