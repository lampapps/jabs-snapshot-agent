import re
from pathlib import Path

from app import settings

ROOT = Path(settings.__file__).resolve().parent.parent


def test_version_comes_from_the_version_file():
    assert re.fullmatch(r"\d+\.\d+\.\d+", settings.VERSION)
    assert settings.VERSION == (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_a_missing_or_malformed_file_reads_as_unknown(tmp_path, caplog):
    path = tmp_path / "VERSION"
    assert settings.read_version(path) == "unknown"
    path.write_text("v1.2\n", encoding="utf-8")
    assert settings.read_version(path) == "unknown"
    assert "not MAJOR.MINOR.PATCH" in caplog.text


def test_surrounding_whitespace_is_ignored(tmp_path):
    path = tmp_path / "VERSION"
    path.write_text("  1.2.3 \n\n", encoding="utf-8")
    assert settings.read_version(path) == "1.2.3"
