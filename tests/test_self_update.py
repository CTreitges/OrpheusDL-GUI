"""Installing a release into the one-folder app (hires/self_update.py).

Every test that changes an app folder starts from a realistic one -- EXE,
config with the four files that hold logins, settings and queue, downloads,
extensions, the six bundled modules plus one of the user's own -- and proves
by hashing that config/, downloads/ and extensions/ come out byte-identical.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_github import FakeGitHub  # noqa: E402
from hires import self_update as su  # noqa: E402
from hires import update_check as uc  # noqa: E402

BUNDLED = ("deezer", "qobuz", "soundcloud", "spotify", "tidal", "youtube")
OLD_EXE_BYTES = b"MZ old exe " + b"o" * 2000
NEW_EXE_BYTES = b"MZ new exe " + b"n" * 3000
NEW_VERSION = "v2.0.3-hires.13"


def tree_hash(root: Path) -> dict:
    """``relative path -> sha256`` of every file below ``root``."""
    out = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def protected_hash(app: Path) -> dict:
    return {name: tree_hash(app / name) for name in su.PROTECTED}


@pytest.fixture
def app(tmp_path) -> Path:
    """An old one-folder install with real-looking user data."""
    root = tmp_path / "OrpheusDL-GUI-windows"
    (root / "config").mkdir(parents=True)
    (root / "OrpheusDL_GUI.exe").write_bytes(OLD_EXE_BYTES)
    (root / "config" / "settings.json").write_text('{"global": {"general": {"download_path": "D:/Music"}}}')
    (root / "config" / "loginstorage.bin").write_bytes(os.urandom(512))
    (root / "config" / "hires_queue.json").write_text('{"items": [{"id": "1", "url": "x"}]}')
    (root / "config" / "hires_spotify_tokens.json").write_text('{"refresh_token": "secret"}')
    (root / "downloads" / "Artist").mkdir(parents=True)
    (root / "downloads" / "Artist" / "track.flac").write_bytes(os.urandom(4096))
    (root / "extensions" / "myext").mkdir(parents=True)
    (root / "extensions" / "myext" / "ext.py").write_text("print('ext')")
    for name in BUNDLED + ("mymodule",):
        (root / "modules" / name).mkdir(parents=True)
        (root / "modules" / name / "interface.py").write_text(f"# old {name}")
    return root


@pytest.fixture
def bundle(tmp_path) -> Path:
    """What a new EXE carries in its PyInstaller bundle."""
    root = tmp_path / "_MEI_bundle"
    for name in BUNDLED:
        (root / "modules" / name).mkdir(parents=True)
        (root / "modules" / name / "interface.py").write_text(f"# new {name}")
    return root


def release_zip(exe: bytes = NEW_EXE_BYTES, extra: dict = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("OrpheusDL_GUI.exe", exe)
        archive.writestr("Update OrpheusDL.cmd", "@echo off\r\n")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def channel_for(server) -> uc.Channel:
    return uc.channel_from_env({uc.TEST_SERVER_ENV: server.base})


# -- download -----------------------------------------------------------------

class TestDownload:
    def test_verified_download(self, tmp_path):
        payload = b"x" * 600_000
        with FakeGitHub() as gh:
            release = gh.publish(NEW_VERSION, payload)
            asset = release["assets"][0]
            seen = []
            dest = su.download(
                asset["browser_download_url"], str(tmp_path / "r.zip"), size=asset["size"],
                sha256=asset["digest"][7:], channel=channel_for(gh),
                progress=lambda done, total: seen.append(done),
            )
        assert Path(dest).read_bytes() == payload
        assert seen[-1] == len(payload)

    def test_wrong_digest_leaves_nothing(self, tmp_path):
        """N2: a file that is not the published one never stays on disk."""
        with FakeGitHub() as gh:
            gh.publish(NEW_VERSION, b"tampered")
            url = gh.base + gh.asset_path(NEW_VERSION)
            with pytest.raises(su.UpdateError, match="checksum mismatch"):
                su.download(url, str(tmp_path / "r.zip"), size=8, sha256="0" * 64, channel=channel_for(gh))
        assert list(tmp_path.iterdir()) == []

    def test_cut_off_download_leaves_nothing(self, tmp_path):
        with FakeGitHub() as gh:
            gh.publish(NEW_VERSION, b"short")
            url = gh.base + gh.asset_path(NEW_VERSION)
            with pytest.raises(su.UpdateError, match="cut off"):
                su.download(url, str(tmp_path / "r.zip"), size=50,
                            sha256=hashlib.sha256(b"short").hexdigest(), channel=channel_for(gh))
        assert list(tmp_path.iterdir()) == []

    def test_larger_than_announced_is_refused(self, tmp_path):
        with FakeGitHub() as gh:
            gh.publish(NEW_VERSION, b"x" * 1000)
            url = gh.base + gh.asset_path(NEW_VERSION)
            with pytest.raises(su.UpdateError, match="larger"):
                su.download(url, str(tmp_path / "r.zip"), size=10, sha256="0" * 64, channel=channel_for(gh))
        assert list(tmp_path.iterdir()) == []

    def test_foreign_address_is_never_requested(self, tmp_path):
        with pytest.raises(su.UpdateError, match="not this app's release"):
            su.download("https://evil.example/x.zip", str(tmp_path / "r.zip"), size=1,
                        sha256="0" * 64, channel=uc.GITHUB)

    def test_redirect_off_the_allowed_hosts_is_refused(self, tmp_path):
        """T2: GitHub may redirect to its storage hosts, nowhere else."""
        with FakeGitHub() as gh:
            path = gh.asset_path(NEW_VERSION)
            gh.routes[path] = (302, {"Location": "http://example.com/evil.zip"}, b"")
            with pytest.raises(su.UpdateError, match="HTTP 302"):
                su.download(gh.base + path, str(tmp_path / "r.zip"), size=5,
                            sha256="0" * 64, channel=channel_for(gh))
        assert list(tmp_path.iterdir()) == []

    def test_cancel_stops_and_cleans_up(self, tmp_path):
        with FakeGitHub() as gh:
            gh.publish(NEW_VERSION, b"x" * 600_000)
            url = gh.base + gh.asset_path(NEW_VERSION)
            with pytest.raises(su.UpdateError, match="cancelled"):
                su.download(url, str(tmp_path / "r.zip"), size=600_000, sha256="0" * 64,
                            channel=channel_for(gh), cancelled=lambda: True)
        assert list(tmp_path.iterdir()) == []


# -- extracting ---------------------------------------------------------------

class TestExtractExe:
    def test_takes_only_the_exe(self, tmp_path):
        zip_path = tmp_path / "r.zip"
        zip_path.write_bytes(release_zip())
        out = tmp_path / "out"
        out.mkdir()
        su.extract_exe(str(zip_path), str(out / "OrpheusDL_GUI.new.exe"))
        assert (out / "OrpheusDL_GUI.new.exe").read_bytes() == NEW_EXE_BYTES
        assert [p.name for p in out.iterdir()] == ["OrpheusDL_GUI.new.exe"]

    def test_zip_slip_entries_are_never_written(self, tmp_path):
        """T4: names with a path are ignored -- not even a decoy EXE in a sub folder."""
        evil = {"../evil.exe": b"MZ evil", "sub/OrpheusDL_GUI.exe": b"MZ sub", "C:/x.exe": b"MZ abs"}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in evil.items():
                archive.writestr(name, data)
            archive.writestr("OrpheusDL_GUI.exe", NEW_EXE_BYTES)
        (tmp_path / "work").mkdir()
        zip_path = tmp_path / "work" / "r.zip"
        zip_path.write_bytes(buffer.getvalue())
        before = sorted(p.as_posix() for p in tmp_path.rglob("*"))
        su.extract_exe(str(zip_path), str(tmp_path / "work" / "new.exe"))
        after = sorted(p.as_posix() for p in tmp_path.rglob("*"))
        assert set(after) - set(before) == {(tmp_path / "work" / "new.exe").as_posix()}
        assert (tmp_path / "work" / "new.exe").read_bytes() == NEW_EXE_BYTES

    def test_zip_without_exe(self, tmp_path):
        zip_path = tmp_path / "r.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "x")
        zip_path.write_bytes(buffer.getvalue())
        with pytest.raises(su.UpdateError, match="no OrpheusDL_GUI.exe"):
            su.extract_exe(str(zip_path), str(tmp_path / "new.exe"))
        assert not (tmp_path / "new.exe").exists()

    def test_not_a_windows_program(self, tmp_path):
        zip_path = tmp_path / "r.zip"
        zip_path.write_bytes(release_zip(exe=b"#!/bin/sh\necho hi"))
        with pytest.raises(su.UpdateError, match="not a Windows program"):
            su.extract_exe(str(zip_path), str(tmp_path / "new.exe"))
        assert not (tmp_path / "new.exe").exists()
        assert not (tmp_path / "new.exe.part").exists()

    def test_damaged_zip(self, tmp_path):
        zip_path = tmp_path / "r.zip"
        zip_path.write_bytes(release_zip()[:100])
        with pytest.raises(su.UpdateError, match="damaged"):
            su.extract_exe(str(zip_path), str(tmp_path / "new.exe"))


# -- swapping the EXE ---------------------------------------------------------

class TestSwapExe:
    def test_new_exe_in_place_old_one_kept(self, app, tmp_path):
        new = tmp_path / "new.exe"
        new.write_bytes(NEW_EXE_BYTES)
        su.swap_exe(str(app), str(new))
        assert (app / "OrpheusDL_GUI.exe").read_bytes() == NEW_EXE_BYTES
        assert (app / "OrpheusDL_GUI.old.exe").read_bytes() == OLD_EXE_BYTES
        assert not (app / "OrpheusDL_GUI.new.exe").exists()

    def test_previous_backup_is_replaced(self, app, tmp_path):
        (app / "OrpheusDL_GUI.old.exe").write_bytes(b"MZ ancient")
        new = tmp_path / "new.exe"
        new.write_bytes(NEW_EXE_BYTES)
        su.swap_exe(str(app), str(new))
        assert (app / "OrpheusDL_GUI.old.exe").read_bytes() == OLD_EXE_BYTES

    def test_failed_second_rename_puts_the_old_exe_back(self, app, tmp_path, monkeypatch):
        """T7/N5: the app folder never ends up without OrpheusDL_GUI.exe."""
        new = tmp_path / "new.exe"
        new.write_bytes(NEW_EXE_BYTES)
        real_rename = os.rename
        calls = []

        def flaky(src, dst):
            calls.append(os.path.basename(src))
            if os.path.basename(src) == su.NEW_EXE:
                raise PermissionError("locked")
            return real_rename(src, dst)

        monkeypatch.setattr(su.os, "rename", flaky)
        with pytest.raises(su.UpdateError, match="could not be put in place"):
            su.swap_exe(str(app), str(new))
        assert (app / "OrpheusDL_GUI.exe").read_bytes() == OLD_EXE_BYTES
        assert not (app / "OrpheusDL_GUI.new.exe").exists()

    def test_no_write_access_changes_nothing(self, app, tmp_path, monkeypatch):
        """N4: refused with the reason, folder unchanged."""
        before = tree_hash(app)
        monkeypatch.setattr(su, "write_problem", lambda folder: "no write access to the program folder (PermissionError)")
        new = tmp_path / "new.exe"
        new.write_bytes(NEW_EXE_BYTES)
        with pytest.raises(su.UpdateError, match="no write access"):
            su.swap_exe(str(app), str(new))
        assert tree_hash(app) == before


# -- modules ------------------------------------------------------------------

class TestRefreshModules:
    def test_six_bundled_replaced_own_module_kept(self, app, bundle):
        """Mengentest: all six bundled modules renewed, the user's own one stays."""
        before = protected_hash(app)
        result = su.refresh_modules(str(app), str(bundle / "modules"), NEW_VERSION)
        assert sorted(result.replaced) == sorted(BUNDLED)
        for name in BUNDLED:
            assert (app / "modules" / name / "interface.py").read_text() == f"# new {name}"
            assert (Path(result.backup_dir) / name / "interface.py").read_text() == f"# old {name}"
        assert (app / "modules" / "mymodule" / "interface.py").read_text() == "# old mymodule"
        assert Path(result.backup_dir).name == "modules.backup-before-hires.13"
        assert su.read_marker(str(app)) == NEW_VERSION
        assert protected_hash(app) == before

    def test_backup_is_named_after_the_old_version(self, app, bundle):
        (app / "modules" / su.MARKER).write_text("v2.0.3-hires.12")
        result = su.refresh_modules(str(app), str(bundle / "modules"), NEW_VERSION)
        assert Path(result.backup_dir).name == "modules.backup-v2.0.3-hires.12"

    def test_failed_copy_restores_the_module(self, app, bundle, monkeypatch):
        real = su.shutil.copytree

        def flaky(src, dst, *a, **k):
            if os.path.basename(src) == "spotify":
                raise OSError("disk full")
            return real(src, dst, *a, **k)

        monkeypatch.setattr(su.shutil, "copytree", flaky)
        with pytest.raises(su.UpdateError, match="spotify"):
            su.refresh_modules(str(app), str(bundle / "modules"), NEW_VERSION)
        assert (app / "modules" / "spotify" / "interface.py").read_text() == "# old spotify"
        assert su.read_marker(str(app)) == ""

    def test_on_start_runs_once_per_version(self, app, bundle):
        assert su.refresh_modules_on_start(str(app), str(bundle), NEW_VERSION) is not None
        assert su.refresh_modules_on_start(str(app), str(bundle), NEW_VERSION) is None
        assert len(list(app.glob("modules.backup-*"))) == 1

    def test_on_start_development_build_does_nothing(self, app, bundle):
        before = tree_hash(app)
        assert su.refresh_modules_on_start(str(app), str(bundle), None) is None
        assert su.refresh_modules_on_start(str(app), None, NEW_VERSION) is None
        assert tree_hash(app) == before


# -- update script: --install-into ----------------------------------------------

class TestInstallInto:
    def _new_exe(self, tmp_path) -> Path:
        folder = tmp_path / "OrpheusDL-GUI-windows" / "OrpheusDL-GUI-windows-new"
        folder.mkdir(parents=True, exist_ok=True)
        exe = folder / "OrpheusDL_GUI.exe"
        exe.write_bytes(NEW_EXE_BYTES)
        return exe

    def test_script_update_keeps_user_data_byte_identical(self, app, bundle, tmp_path):
        """D3 Vorbestand: config/, downloads/, extensions/ hashed before and after."""
        before = protected_hash(app)
        exe = self._new_exe(tmp_path)
        result = su.install_into(str(app), own_exe=str(exe), bundle_dir=str(bundle),
                                 new_version=NEW_VERSION, is_running=lambda: False)
        assert result.code == su.EXIT_OK, result.message
        assert "Updated" in result.message and "hires.13" in result.message
        assert (app / "OrpheusDL_GUI.exe").read_bytes() == NEW_EXE_BYTES
        assert (app / "OrpheusDL_GUI.old.exe").read_bytes() == OLD_EXE_BYTES
        assert (app / "modules" / "tidal" / "interface.py").read_text() == "# new tidal"
        assert (app / "modules" / "mymodule" / "interface.py").read_text() == "# old mymodule"
        assert protected_hash(app) == before
        assert before["config"], "fixture must really have config files"

    def test_wrong_folder_changes_nothing(self, tmp_path, bundle):
        """N6: no OrpheusDL_GUI.exe + config/ -> refused, not a single file written."""
        folder = tmp_path / "Downloads"
        folder.mkdir()
        (folder / "OrpheusDL_GUI.exe").write_bytes(OLD_EXE_BYTES)  # but no config/
        before = tree_hash(folder)
        exe = self._new_exe(tmp_path)
        result = su.install_into(str(folder), own_exe=str(exe), bundle_dir=str(bundle),
                                 new_version=NEW_VERSION, is_running=lambda: False)
        assert result.code == su.EXIT_REFUSED
        assert "Nothing was changed" in result.message
        assert tree_hash(folder) == before

    def test_running_app_changes_nothing(self, app, bundle, tmp_path):
        exe = self._new_exe(tmp_path)
        before = tree_hash(app)
        result = su.install_into(str(app), own_exe=str(exe), bundle_dir=str(bundle),
                                 new_version=NEW_VERSION, is_running=lambda: True)
        assert result.code == su.EXIT_REFUSED
        assert "still running" in result.message
        assert tree_hash(app) == before

    def test_second_run_keeps_the_real_old_exe(self, app, bundle, tmp_path):
        """Running the script twice must not overwrite the fallback with the new EXE."""
        exe = self._new_exe(tmp_path)
        kwargs = dict(own_exe=str(exe), bundle_dir=str(bundle), new_version=NEW_VERSION,
                      is_running=lambda: False)
        su.install_into(str(app), **kwargs)
        again = su.install_into(str(app), **kwargs)
        assert again.code == su.EXIT_OK
        assert "already" in again.message
        assert (app / "OrpheusDL_GUI.old.exe").read_bytes() == OLD_EXE_BYTES
        assert len(list(app.glob("modules.backup-*"))) == 1

    def test_zip_unpacked_over_the_old_folder_refreshes_modules(self, app, bundle):
        """The EXE was already overwritten by unpacking; only the modules are left."""
        (app / "OrpheusDL_GUI.exe").write_bytes(NEW_EXE_BYTES)
        before = protected_hash(app)
        result = su.install_into(str(app), own_exe=str(app / "OrpheusDL_GUI.exe"),
                                 bundle_dir=str(bundle), new_version=NEW_VERSION,
                                 is_running=lambda: False)
        assert result.code == su.EXIT_OK
        assert (app / "modules" / "youtube" / "interface.py").read_text() == "# new youtube"
        assert not (app / "OrpheusDL_GUI.old.exe").exists()
        assert protected_hash(app) == before

    def test_log_names_versions_not_paths(self, app, bundle, tmp_path):
        """T12: the update log holds no path with the user's name in it."""
        exe = self._new_exe(tmp_path)
        su.install_into(str(app), own_exe=str(exe), bundle_dir=str(bundle),
                        new_version=NEW_VERSION, is_running=lambda: False)
        text = (app / "update" / "update.log").read_text(encoding="utf-8")
        assert NEW_VERSION in text
        assert str(tmp_path) not in text and tmp_path.as_posix() not in text


class TestRunCli:
    def test_not_asked_returns_none(self):
        assert su.run_cli(["OrpheusDL_GUI.exe"]) is None
        assert su.run_cli(["OrpheusDL_GUI.exe", "--after-update"]) is None

    def test_missing_folder_argument(self):
        shown = []
        assert su.run_cli(["x", "--install-into"], show=lambda t, e: shown.append(e)) == su.EXIT_REFUSED
        assert shown == [True]

    def test_quiet_no_start(self, app, bundle, tmp_path, monkeypatch):
        monkeypatch.setattr(su, "mutex_exists", lambda name=su.MUTEX_NAME: False)
        exe = tmp_path / "new" / "OrpheusDL_GUI.exe"
        exe.parent.mkdir()
        exe.write_bytes(NEW_EXE_BYTES)
        started, shown = [], []
        code = su.run_cli(
            ["x", "--install-into", str(app), "--quiet", "--no-start"],
            own_exe=str(exe), bundle_dir=str(bundle), new_version=NEW_VERSION,
            show=lambda t, e: shown.append(t), popen=lambda *a, **k: started.append(a),
        )
        assert code == su.EXIT_OK
        assert shown == [] and started == []
        assert (app / "OrpheusDL_GUI.exe").read_bytes() == NEW_EXE_BYTES

    def test_success_starts_the_updated_app(self, app, bundle, tmp_path, monkeypatch):
        monkeypatch.setattr(su, "mutex_exists", lambda name=su.MUTEX_NAME: False)
        exe = tmp_path / "new" / "OrpheusDL_GUI.exe"
        exe.parent.mkdir()
        exe.write_bytes(NEW_EXE_BYTES)
        started = []
        su.run_cli(["x", "--install-into", str(app), "--quiet"], own_exe=str(exe),
                   bundle_dir=str(bundle), new_version=NEW_VERSION,
                   popen=lambda args, **k: started.append(args))
        assert started == [[os.path.join(str(app), "OrpheusDL_GUI.exe")]]


# -- auto-update: stage, restart ------------------------------------------------

class TestStageRelease:
    def _release(self, gh, payload, digest=None):
        gh.publish(NEW_VERSION, payload, digest=digest)
        return uc.check("v2.0.3-hires.12", channel=channel_for(gh)).release

    def test_stages_the_verified_exe(self, app):
        with FakeGitHub() as gh:
            release = self._release(gh, release_zip())
            staged = su.stage_release(str(app), release, channel_for(gh))
        assert Path(staged).name == su.NEW_EXE
        assert Path(staged).read_bytes() == NEW_EXE_BYTES
        assert (app / "OrpheusDL_GUI.exe").read_bytes() == OLD_EXE_BYTES  # not yet swapped
        assert list((app / "update").iterdir()) == []  # ZIP removed again

    def test_bad_digest_stages_nothing(self, app):
        """N2 / D5 Gegenprobe: wrong digest -> no staged EXE, no ZIP, app as before."""
        before = tree_hash(app)
        with FakeGitHub() as gh:
            release = self._release(gh, release_zip(), digest="sha256:" + "0" * 64)
            with pytest.raises(su.UpdateError, match="checksum"):
                su.stage_release(str(app), release, channel_for(gh))
        after = {k: v for k, v in tree_hash(app).items() if not k.startswith("update/")}
        assert after == before
        assert not (app / su.NEW_EXE).exists()

    def test_release_without_digest_is_not_installable(self, app):
        with FakeGitHub() as gh:
            gh.publish(NEW_VERSION, release_zip(), digest="")
            release = uc.check("v2.0.3-hires.12", channel=channel_for(gh)).release
            with pytest.raises(su.UpdateError, match="cannot be installed"):
                su.stage_release(str(app), release, channel_for(gh))

    def test_full_auto_update_keeps_user_data(self, app, bundle):
        """Stage, swap, then the new version's first start refreshes the modules."""
        before = protected_hash(app)
        with FakeGitHub() as gh:
            release = self._release(gh, release_zip())
            staged = su.stage_release(str(app), release, channel_for(gh))
        su.swap_exe(str(app), staged)
        su.refresh_modules_on_start(str(app), str(bundle), NEW_VERSION)
        assert (app / "OrpheusDL_GUI.exe").read_bytes() == NEW_EXE_BYTES
        assert (app / "OrpheusDL_GUI.old.exe").read_bytes() == OLD_EXE_BYTES
        assert (app / "modules" / "soundcloud" / "interface.py").read_text() == "# new soundcloud"
        assert protected_hash(app) == before


def test_cleanup_leftovers(app):
    (app / su.NEW_EXE).write_bytes(b"MZ half")
    (app / "update").mkdir()
    (app / "update" / "x.zip.part").write_bytes(b"x")
    (app / "update" / "state.json").write_text("{}")
    su.cleanup_leftovers(str(app))
    assert not (app / su.NEW_EXE).exists()
    assert [p.name for p in (app / "update").iterdir()] == ["state.json"]


def test_restart_starts_the_new_exe_waiting_for_us(app):
    calls = []
    su.restart(str(app), popen=lambda args, **kw: calls.append((args, kw)))
    args, kw = calls[0]
    assert args == [os.path.join(str(app), "OrpheusDL_GUI.exe"), "--after-update"]
    assert kw["cwd"] == str(app)


def test_wait_for_previous_instance():
    states = iter([True, True, False])
    slept = []
    assert su.wait_for_previous_instance(["x", "--after-update"], exists=lambda: next(states),
                                         sleep=slept.append)
    assert len(slept) == 2
    assert not su.wait_for_previous_instance(["x"], exists=lambda: True)


class TestGuiWiring:
    """gui.py is too big to import; its source must call the core in the right order."""

    @pytest.fixture
    def main_block(self):
        source = (Path(__file__).resolve().parents[1] / "gui.py").read_text(encoding="utf-8")
        return source[source.index('if __name__ == "__main__":'):]

    def test_install_into_runs_before_the_mutex_and_any_folder(self, main_block):
        cli = main_block.index("_hires_self_update.run_cli(sys.argv)")
        assert cli < main_block.index("CreateMutexW(None, True, mutex_name)")
        assert cli < main_block.index("_DATA_DIR = get_data_directory()")
        assert main_block.index("wait_for_previous_instance(sys.argv)") < main_block.index("CreateMutexW(None, True")

    def test_update_check_replaces_the_stock_check_and_waits_for_the_main_loop(self, main_block):
        assert "run_check_in_thread(__version__, app)" not in main_block, "stock check asks bascurtiz releases"
        assert "app.after(4000, _hires_updater.start_auto)" in main_block
        assert "add_check_button(mid_inner, _hires_updater)" in main_block

    def test_modules_refresh_before_the_stock_copy(self, main_block):
        refresh = main_block.index("_hires_self_update.refresh_modules_on_start(")
        assert refresh < main_block.index("copy_bundled_resources_to_data_dir(_DATA_DIR)")
