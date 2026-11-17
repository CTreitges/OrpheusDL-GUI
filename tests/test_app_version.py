"""Version of this build and how releases are ordered (hires/app_version.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hires import app_version  # noqa: E402
from hires.app_version import current_version, is_newer, parse_tag, short_label  # noqa: E402


class TestParseTag:
    def test_release_tag(self):
        assert parse_tag("v2.0.3-hires.12") == (2, 0, 3, 12)

    def test_without_leading_v(self):
        assert parse_tag("2.0.3-hires.7") == (2, 0, 3, 7)

    @pytest.mark.parametrize(
        "junk",
        [None, "", "2.0.3", "v2.0.3", "v2.0.3-hires", "v2.0.3-hires.x", "v2.0.3-beta.1",
         "v2.0.3-hires.12; rm -rf", 12, "v2.0.3-hires.12\nX"],
    )
    def test_anything_else_is_not_ours(self, junk):
        assert parse_tag(junk) is None


class TestIsNewer:
    def test_numeric_not_string_order(self):
        """hires.10 must beat hires.9 -- a string comparison says otherwise."""
        assert is_newer("v2.0.3-hires.10", "v2.0.3-hires.9")
        assert not is_newer("v2.0.3-hires.9", "v2.0.3-hires.10")

    def test_same_version_is_not_newer(self):
        assert not is_newer("v2.0.3-hires.12", "v2.0.3-hires.12")

    def test_older_is_never_offered(self):
        assert not is_newer("v2.0.3-hires.11", "v2.0.3-hires.12")

    def test_upstream_version_counts_first(self):
        assert is_newer("v2.0.4-hires.1", "v2.0.3-hires.12")

    def test_development_build_never_updates(self):
        """N9: without a baked-in version nothing is newer."""
        assert not is_newer("v2.0.3-hires.13", None)
        assert not is_newer("v9.9.9-hires.99", "2.0.3")

    def test_foreign_candidate_is_never_newer(self):
        assert not is_newer("v3.0.0", "v2.0.3-hires.12")


class TestCurrentVersion:
    def test_checkout_without_build_file_is_a_development_build(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "hires._build_version", None)  # import fails
        assert current_version() is None

    def test_build_file_gives_the_version(self, monkeypatch):
        fake = type(sys)("hires._build_version")
        fake.VERSION = "v2.0.3-hires.13"
        monkeypatch.setitem(sys.modules, "hires._build_version", fake)
        assert current_version() == "v2.0.3-hires.13"

    def test_garbage_in_build_file_counts_as_development_build(self, monkeypatch):
        fake = type(sys)("hires._build_version")
        fake.VERSION = "main"
        monkeypatch.setitem(sys.modules, "hires._build_version", fake)
        assert current_version() is None

    def test_the_build_file_is_not_committed(self):
        root = Path(app_version.__file__).resolve().parents[1]
        assert "hires/_build_version.py" in (root / ".gitignore").read_text(encoding="utf-8")


def test_short_label():
    assert short_label("v2.0.3-hires.12") == "hires.12"
    assert short_label(None) == "development build"
