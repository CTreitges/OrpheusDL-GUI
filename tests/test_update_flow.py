"""The in-app update flow (hires/update_flow.py), without widgets.

Workers run synchronously here unless a test is about the main thread never
blocking. The view is a recorder.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hires import self_update as su  # noqa: E402
from hires import update_check as uc  # noqa: E402
from hires.update_flow import UpdateController, updates_enabled  # noqa: E402

CURRENT = "v2.0.3-hires.12"
RELEASE = uc.ReleaseInfo(
    tag="v2.0.3-hires.13", name="hires.13", notes="Fixes", page_url=uc.RELEASES_PAGE,
    asset_name="OrpheusDL-GUI-windows.zip",
    asset_url="https://github.com/CTreitges/OrpheusDL-GUI/releases/download/v2.0.3-hires.13/OrpheusDL-GUI-windows.zip",
    size=10, sha256="a" * 64,
)


class Recorder:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *args: self.events.append((name,) + args)

    def names(self):
        return [e[0] for e in self.events]


class Harness:
    def __init__(self, tmp_path, *, busy=False, version=CURRENT, enabled=True,
                 result=None, stage_error=None, restart_error=None, can_self_install=True):
        self.busy = busy
        self.held = []
        self.scheduled = []
        self.swapped = []
        self.restarted = []
        self.quit = 0
        self.view = Recorder()
        self.staged = tmp_path / su.NEW_EXE
        self.result = result or uc.CheckResult(uc.AVAILABLE, RELEASE, "Version v2.0.3-hires.13 is available.")
        self.checks = 0

        def check(version, channel):
            self.checks += 1
            return self.result

        def stage(app_dir, release, channel, progress, cancelled):
            if stage_error:
                raise su.UpdateError(stage_error)
            progress(5, 10)
            progress(10, 10)
            self.staged.write_bytes(b"MZ new")
            return str(self.staged)

        def restart(app_dir, exe_name):
            if restart_error:
                raise restart_error
            self.restarted.append(exe_name)

        def quit_app():
            self.quit += 1

        self.controller = UpdateController(
            version=version, app_dir=str(tmp_path), exe_name="OrpheusDL_GUI.exe",
            dispatch=lambda fn, *a: fn(*a),
            schedule=lambda ms, fn: self.scheduled.append(fn),
            busy=lambda: self.busy, hold=self.held.append, quit_app=quit_app,
            enabled=lambda: enabled, view_factory=lambda: self.view,
            can_self_install=can_self_install, channel=uc.GITHUB,
            check_fn=check, stage_fn=stage,
            swap_fn=lambda app_dir, staged, exe_name: self.swapped.append(staged),
            restart_fn=restart, run_worker=lambda fn: fn(), now=lambda: 1_000_000.0,
        )


class TestChecking:
    def test_available_release_is_offered(self, tmp_path):
        h = Harness(tmp_path)
        assert h.controller.check_now()
        assert h.view.events[0] == ("offer", RELEASE, True)

    def test_manual_check_says_up_to_date(self, tmp_path):
        h = Harness(tmp_path, result=uc.CheckResult(uc.UP_TO_DATE, RELEASE, "Up to date."))
        h.controller.check_now()
        assert h.view.events == [("info", "OrpheusDL is up to date (hires.12).")]

    def test_failed_manual_check_tells_the_user(self, tmp_path):
        h = Harness(tmp_path, result=uc.CheckResult(uc.FAILED, None, "Update check failed: GitHub answered HTTP 403."))
        h.controller.check_now()
        assert h.view.names() == ["info"]
        assert "HTTP 403" in h.view.events[0][1]

    def test_failed_automatic_check_stays_silent(self, tmp_path):
        """N1: no window for a check the user did not ask for."""
        h = Harness(tmp_path, result=uc.CheckResult(uc.FAILED, None, "no connection"))
        h.controller.start_auto()
        assert h.view.events == []

    def test_automatic_check_once_a_day(self, tmp_path):
        h = Harness(tmp_path, result=uc.CheckResult(uc.UP_TO_DATE, RELEASE, "Up to date."))
        assert h.controller.start_auto()
        assert not h.controller.start_auto()
        assert h.checks == 1
        assert uc.load_state(su.state_path(str(tmp_path)))["last_check"] == 1_000_000.0

    def test_switched_off_means_no_automatic_check(self, tmp_path):
        h = Harness(tmp_path, enabled=False)
        assert not h.controller.start_auto()
        assert h.checks == 0
        assert h.controller.check_now(), "the manual check still works"

    def test_development_build_never_checks(self, tmp_path):
        h = Harness(tmp_path, version=None)
        assert not h.controller.start_auto()
        h.controller.check_now()
        assert h.checks == 0
        assert "development build" in h.view.events[0][1]

    def test_without_self_install_only_the_page_is_offered(self, tmp_path):
        """macOS/Linux: hint and release page, never a download."""
        h = Harness(tmp_path, can_self_install=False)
        h.controller.check_now()
        assert h.view.events[0] == ("offer", RELEASE, False)
        assert not h.controller.accept()
        assert h.swapped == []


class TestInstalling:
    def test_update_busy_idle_app_installs_and_restarts(self, tmp_path):
        h = Harness(tmp_path)
        h.controller.check_now()
        assert h.controller.accept()
        assert ("progress", 10, 10) in h.view.events
        assert h.swapped == [str(h.staged)]
        assert h.restarted == ["OrpheusDL_GUI.exe"] and h.quit == 1
        assert h.view.names()[-1] == "installing"

    def test_update_busy_waits_and_holds_the_queue(self, tmp_path):
        """N3: no swap, no restart while something downloads; the queue holds."""
        h = Harness(tmp_path, busy=True)
        h.controller.check_now()
        h.controller.accept()
        assert h.controller.state == UpdateController.WAITING
        assert h.held == [True]
        assert h.swapped == [] and h.restarted == [] and h.quit == 0
        assert "waiting" in h.view.names()
        h.scheduled.pop()()          # still busy: keeps waiting
        assert h.swapped == []
        h.busy = False
        h.scheduled.pop()()          # free now
        assert h.swapped == [str(h.staged)] and h.quit == 1

    def test_update_busy_cancel_while_waiting_lets_the_queue_go_on(self, tmp_path):
        h = Harness(tmp_path, busy=True)
        h.controller.check_now()
        h.controller.accept()
        h.controller.cancel()
        assert h.held == [True, False]
        assert not h.staged.exists(), "the staged EXE is removed"
        h.busy = False
        for fn in h.scheduled:
            fn()
        assert h.swapped == [] and h.quit == 0

    def test_bad_download_replaces_nothing(self, tmp_path):
        """N2: the stage step failed -> message, no swap, queue released."""
        h = Harness(tmp_path, stage_error="checksum mismatch: the download is not the published file")
        h.controller.check_now()
        h.controller.accept()
        assert h.swapped == [] and h.quit == 0
        assert h.view.events[-1][0] == "failed"
        assert "Nothing was replaced" in h.view.events[-1][1]
        assert h.controller.state == UpdateController.IDLE

    def test_swap_error_is_reported(self, tmp_path):
        h = Harness(tmp_path)

        def refuse(app_dir, staged, exe_name):
            raise su.UpdateError("no write access to the program folder (PermissionError)")

        (tmp_path / "OrpheusDL_GUI.exe").write_bytes(b"MZ old")
        h.controller._swap = refuse
        h.controller.check_now()
        h.controller.accept()
        assert h.quit == 0
        assert "no write access" in h.view.events[-1][1]
        assert not h.staged.exists()

    def test_swap_failure_without_exe_keeps_the_new_one(self, tmp_path):
        """Review PR #3: swap_exe keeps .new.exe when neither EXE is in place
        and says to rename it -- the flow must not delete it, nor claim that
        nothing was replaced."""
        h = Harness(tmp_path)

        def half_done(app_dir, staged, exe_name):
            raise su.UpdateError("the previous program is OrpheusDL_GUI.old.exe and the new one "
                                 "OrpheusDL_GUI.new.exe -- rename one of them to OrpheusDL_GUI.exe")

        h.controller._swap = half_done
        h.controller.check_now()
        h.controller.accept()
        assert h.staged.exists(), "the only complete new program stays"
        assert h.view.events[-1][0] == "failed"
        assert "rename one of them" in h.view.events[-1][1]
        assert "Nothing was replaced" not in h.view.events[-1][1]
        assert h.quit == 0 and h.held[-1] is False

    def test_progress_after_cancel_opens_no_window(self, tmp_path):
        """Review PR #3: progress dispatches still queued after Cancel must not
        open a fresh 'Downloading' dialog that nothing closes."""
        h = Harness(tmp_path)
        h.controller.check_now()
        workers, queued = [], []
        h.controller._run_worker = workers.append            # the download runs "later"
        h.controller._dispatch = lambda fn, *a: queued.append((fn, a))
        assert h.controller.accept()
        h.controller.cancel()                                 # closes the view
        assert h.controller.view is None
        h.view.events.clear()
        opened = []
        h.controller._view_factory = lambda: opened.append(1) or h.view
        workers[0]()                                          # worker reads more blocks
        for fn, args in queued:                               # main thread drains the queue
            fn(*args)
        assert opened == [] and h.view.events == []
        assert h.swapped == [] and not h.staged.exists()

    def test_restart_failure_keeps_the_app_open(self, tmp_path):
        h = Harness(tmp_path, restart_error=OSError("blocked"))
        h.controller.check_now()
        h.controller.accept()
        assert h.quit == 0
        assert "start it again" in h.view.events[-1][1]
        assert h.held[-1] is False

    def test_later_closes_and_forgets_the_offer(self, tmp_path):
        h = Harness(tmp_path)
        h.controller.check_now()
        h.controller.cancel()
        assert h.view.events[-1] == ("close",)
        assert h.controller.state == UpdateController.IDLE
        assert not h.controller.accept()


def test_update_busy_check_never_blocks_the_caller(tmp_path):
    """The check runs on a worker: the (main-thread) caller returns at once."""
    release = threading.Event()
    seen = []

    def slow_check(version, channel):
        release.wait(5)
        return uc.CheckResult(uc.UP_TO_DATE, RELEASE, "Up to date.")

    controller = UpdateController(
        version=CURRENT, app_dir=str(tmp_path), exe_name="x.exe",
        dispatch=lambda fn, *a: (seen.append(threading.current_thread().name), fn(*a)),
        schedule=lambda ms, fn: None, busy=lambda: False, hold=lambda flag: None,
        quit_app=lambda: None, enabled=lambda: True, view_factory=Recorder,
        can_self_install=True, channel=uc.GITHUB, check_fn=slow_check,
    )
    started = time.monotonic()
    assert controller.check_now()
    assert time.monotonic() - started < 0.5
    release.set()
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["hires-update"]


class TestSetting:
    def test_old_settings_without_the_key_keep_checking(self):
        """A settings.json from before this version has no key: default on."""
        old = {"globals": {"advanced": {"debug_mode": False}}}
        assert updates_enabled(old) is True
        assert updates_enabled({}) is True
        assert updates_enabled(None) is True

    def test_switched_off_is_honoured(self):
        assert updates_enabled({"globals": {"advanced": {"check_for_updates": False}}}) is False
