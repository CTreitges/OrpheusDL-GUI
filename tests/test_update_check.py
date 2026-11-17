"""Release check against GitHub (hires/update_check.py): source, digest, failures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_github import FakeGitHub  # noqa: E402
from hires import update_check as uc  # noqa: E402

TAG = "v2.0.3-hires.13"
SHA = "a" * 64
GH_ASSET = f"https://github.com/CTreitges/OrpheusDL-GUI/releases/download/{TAG}/OrpheusDL-GUI-windows.zip"


def api_answer(**over):
    asset = {
        "name": "OrpheusDL-GUI-windows.zip",
        "size": 1234,
        "digest": f"sha256:{SHA}",
        "browser_download_url": GH_ASSET,
    }
    asset.update(over.pop("asset", {}))
    data = {
        "tag_name": TAG,
        "name": "hires.13",
        "body": "notes",
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/CTreitges/OrpheusDL-GUI/releases/tag/{TAG}",
        "assets": [
            {"name": "OrpheusDL-GUI-macos.zip", "size": 5, "digest": "sha256:" + "b" * 64,
             "browser_download_url": GH_ASSET.replace("windows", "macos")},
            asset,
        ],
    }
    data.update(over)
    return data


class TestParseRelease:
    def test_picks_the_windows_zip_and_its_digest(self):
        info = uc.parse_release(api_answer())
        assert info.tag == TAG
        assert info.asset_url == GH_ASSET
        assert info.sha256 == SHA
        assert info.size == 1234
        assert info.installable

    def test_without_digest_it_is_only_a_page(self):
        """T3: nothing to verify against -> never an automatic install."""
        info = uc.parse_release(api_answer(asset={"digest": None}))
        assert info is not None and not info.installable
        assert info.page_url.startswith("https://github.com/CTreitges/OrpheusDL-GUI/releases")

    @pytest.mark.parametrize(
        "url",
        [
            GH_ASSET.replace("CTreitges", "bascurtiz"),          # other repo
            GH_ASSET.replace("https://", "http://"),             # no TLS
            "https://evil.example/CTreitges/OrpheusDL-GUI/releases/download/x.zip",
            GH_ASSET.replace(TAG, "v2.0.3-hires.1"),             # asset of another tag
            "https://user:pw@github.com/CTreitges/OrpheusDL-GUI/releases/download/"
            f"{TAG}/OrpheusDL-GUI-windows.zip",
        ],
    )
    def test_foreign_download_url_is_refused(self, url):
        """T1/N8: only this repository's release download, over HTTPS."""
        info = uc.parse_release(api_answer(asset={"browser_download_url": url}))
        assert not info.installable

    @pytest.mark.parametrize("digest", ["md5:abc", "sha256:xyz", "sha256:" + "a" * 63, ""])
    def test_malformed_digest_is_refused(self, digest):
        assert not uc.parse_release(api_answer(asset={"digest": digest})).installable

    def test_prerelease_and_draft_are_not_offered(self):
        assert uc.parse_release(api_answer(prerelease=True)) is None
        assert uc.parse_release(api_answer(draft=True)) is None

    def test_foreign_tag_is_not_offered(self):
        assert uc.parse_release(api_answer(tag_name="v2.1.0")) is None

    def test_garbage_is_not_a_release(self):
        assert uc.parse_release(["x"]) is None
        assert uc.parse_release(None) is None

    def test_page_link_from_elsewhere_falls_back_to_the_releases_page(self):
        info = uc.parse_release(api_answer(html_url="https://evil.example/x"))
        assert info.page_url == uc.RELEASES_PAGE


class TestCheck:
    def test_newer_release_is_available(self):
        result = uc.check("v2.0.3-hires.12", channel=uc.GITHUB, fetch=lambda ch: api_answer())
        assert result.status == uc.AVAILABLE
        assert result.release.tag == TAG

    def test_same_version_is_up_to_date(self):
        result = uc.check(TAG, channel=uc.GITHUB, fetch=lambda ch: api_answer())
        assert result.status == uc.UP_TO_DATE
        assert "Up to date" in result.message

    def test_development_build_does_not_even_ask(self):
        """N9: no request at all without a version."""
        calls = []
        result = uc.check(None, fetch=lambda ch: calls.append(ch))
        assert result.status == uc.DEV_BUILD
        assert calls == []

    def test_network_errors_end_quietly(self):
        def boom(ch):
            raise uc.UpdateCheckError("no connection (timeout)")

        result = uc.check("v2.0.3-hires.12", channel=uc.GITHUB, fetch=boom)
        assert result.status == uc.FAILED
        assert "no connection" in result.message

    def test_unexpected_exception_never_escapes(self):
        def boom(ch):
            raise RuntimeError("x")

        assert uc.check("v2.0.3-hires.12", channel=uc.GITHUB, fetch=boom).status == uc.FAILED


class TestFetchAgainstAServer:
    """N1 against a real socket: 403 rate limit, 404, garbage, unreachable."""

    def _channel(self, server):
        return uc.channel_from_env({uc.TEST_SERVER_ENV: server.base})

    def test_reads_the_latest_release(self):
        with FakeGitHub() as gh:
            gh.publish(TAG, b"zip-bytes")
            result = uc.check("v2.0.3-hires.12", channel=self._channel(gh))
        assert result.status == uc.AVAILABLE
        assert result.release.installable
        assert result.release.asset_url == gh.base + gh.asset_path(TAG)

    @pytest.mark.parametrize("status", [403, 404, 500])
    def test_http_errors_are_a_quiet_failure(self, status):
        with FakeGitHub() as gh:
            gh.routes[gh.api_path] = (status, {}, b"{}")
            result = uc.check("v2.0.3-hires.12", channel=self._channel(gh))
        assert result.status == uc.FAILED
        assert f"HTTP {status}" in result.message

    def test_garbage_answer_is_a_quiet_failure(self):
        with FakeGitHub() as gh:
            gh.routes[gh.api_path] = (200, {}, b"<html>")
            result = uc.check("v2.0.3-hires.12", channel=self._channel(gh))
        assert result.status == uc.FAILED

    def test_unreachable_server_is_a_quiet_failure(self):
        channel = uc.channel_from_env({uc.TEST_SERVER_ENV: "http://127.0.0.1:9"})
        result = uc.check("v2.0.3-hires.12", channel=channel)
        assert result.status == uc.FAILED
        assert "no connection" in result.message

    def test_redirect_to_another_host_is_refused(self):
        """T2: the opener does not follow a redirect off the allowed hosts."""
        with FakeGitHub() as gh:
            gh.routes[gh.api_path] = (302, {"Location": "http://example.com/x"}, b"")
            result = uc.check("v2.0.3-hires.12", channel=self._channel(gh))
        assert result.status == uc.FAILED
        assert gh.hits == [gh.api_path]


class TestTestServerSwitch:
    """T11: the switch only ever reaches the loopback interface."""

    def test_default_is_github(self):
        assert uc.channel_from_env({}) is uc.GITHUB

    def test_loopback_is_accepted(self):
        channel = uc.channel_from_env({uc.TEST_SERVER_ENV: "http://127.0.0.1:8765"})
        assert channel.is_test
        assert channel.api_url == "http://127.0.0.1:8765/repos/CTreitges/OrpheusDL-GUI/releases/latest"
        assert channel.hosts == frozenset({"127.0.0.1"})

    @pytest.mark.parametrize(
        "value",
        ["http://evil.example:80", "https://127.0.0.1:1", "http://127.0.0.1:1/path",
         "http://u:p@127.0.0.1:1", "ftp://127.0.0.1", "http://127.0.0.2:1", "127.0.0.1:1"],
    )
    def test_anything_else_falls_back_to_github(self, value):
        assert uc.channel_from_env({uc.TEST_SERVER_ENV: value}) is uc.GITHUB

    def test_test_channel_refuses_github_assets(self):
        channel = uc.channel_from_env({uc.TEST_SERVER_ENV: "http://127.0.0.1:8765"})
        assert not uc.url_allowed(GH_ASSET, channel)


class TestOncePerDay:
    def test_due_without_state(self):
        assert uc.check_due({}, now=1000)

    def test_not_due_within_a_day(self):
        assert not uc.check_due({"last_check": 1000}, now=1000 + 3600)

    def test_due_after_a_day(self):
        assert uc.check_due({"last_check": 1000}, now=1000 + uc.CHECK_INTERVAL_SEC)

    def test_clock_going_backwards_checks_again(self):
        assert uc.check_due({"last_check": 5000}, now=1000)

    def test_state_round_trip_and_garbage(self, tmp_path):
        path = str(tmp_path / "update" / "state.json")
        assert uc.load_state(path) == {}
        uc.save_state(path, {"last_check": 42})
        assert uc.load_state(path) == {"last_check": 42}
        Path(path).write_text("[1]", encoding="utf-8")
        assert uc.load_state(path) == {}
