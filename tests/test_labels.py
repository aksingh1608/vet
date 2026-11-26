import pytest

from tests.conftest import FIXTURES
from vet.labels import LabelError, label_files, load_labels
from vet.schemas import RealAlive, Trust

LABELS = FIXTURES / "labels"


def test_csv_with_the_organisers_own_column_names(settings) -> None:
    cols = {"url": "Product URL", "real_alive": "Is Real", "trust": "Trust Level", "reasons": "Why"}
    s = settings.model_copy(update={"label_columns": {**settings.label_columns, **cols}})
    labels = load_labels(LABELS / "organiser.csv", s)
    assert [(lb.url, lb.real_alive, lb.trust) for lb in labels] == [
        ("https://ledgerly.example", RealAlive.YES, Trust.TRUST),
        ("https://fakeco.example", RealAlive.NO, Trust.DO_NOT_TRUST),
        ("https://placeholder.example", None, None),
    ]
    assert labels[0].reasons == ["Register record", "Long history"]
    assert not labels[2].labelled  # loaded, but never scored


def test_nested_json_with_dotted_columns(settings) -> None:
    cols = {"url": "site.url", "real_alive": "labels.alive", "trust": "labels.trust"}
    s = settings.model_copy(update={"label_columns": {**settings.label_columns, **cols}})
    labels = load_labels(LABELS / "organiser.json", s)
    assert [(lb.real_alive, lb.trust) for lb in labels] == [
        (RealAlive.YES, Trust.CAUTION),
        (RealAlive.NO, Trust.DO_NOT_TRUST),
    ]


def test_organiser_label_names_from_config_are_understood(settings, tmp_path) -> None:
    s = settings.model_copy(
        update={
            "real_alive_labels": {
                RealAlive.YES: "REAL",
                RealAlive.UNCLEAR: "?!",
                RealAlive.NO: "X",
            },
            "trust_labels": {Trust.TRUST: "A", Trust.CAUTION: "B", Trust.DO_NOT_TRUST: "C"},
        }
    )
    path = tmp_path / "l.json"
    path.write_text('[{"url": "https://a.example", "real_alive": "?!", "trust": "B"}]')
    (label,) = load_labels(path, s)
    assert (label.real_alive, label.trust) == (RealAlive.UNCLEAR, Trust.CAUTION)


def test_unknown_label_names_the_row(settings, tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("url,real_alive,trust\nhttps://a.example,perhaps,trust\n")
    with pytest.raises(LabelError, match="row 1.*perhaps"):
        load_labels(path, settings)


def test_extra_set_is_valid_and_all_placeholders(settings) -> None:
    labels = load_labels(FIXTURES.parent.parent / "eval" / "extra_set.yaml", settings)
    assert len(labels) == 8  # 12 entries, 4 slots without a URL yet
    assert not any(lb.labelled for lb in labels)


def test_label_files_finds_supported_files(tmp_path) -> None:
    (tmp_path / "a.csv").write_text("url\n")
    (tmp_path / ".gitkeep").write_text("")
    (tmp_path / "notes.txt").write_text("")
    assert [p.name for p in label_files(tmp_path)] == ["a.csv"]
