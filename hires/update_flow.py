"""The in-app update, step by step -- logic only, no widgets.

check (worker) -> offer (dialog) -> download + verify (worker) -> wait until
nothing downloads -> swap the EXE -> start the new one -> quit.

Rules this class keeps:

* Network work never runs on the main thread; results come back through the
  dispatcher, so the window never blocks (N1, T14).
* The EXE is swapped and the app restarted only while nothing downloads: no
  download of the stock GUI, no queue item, no "Convert folder…" job. While
  it waits, the queue starts nothing new; Cancel lets it continue (N3, T13).
* Any failure leaves the installed app as it was and says why (N2, N4).
* Only Windows builds with a version install themselves; elsewhere the dialog
  offers the release page (macOS/Linux) or nothing (development build).

The view is anything with ``offer``, ``progress``, ``waiting``,
``installing``, ``failed``, ``info`` and ``close`` -- the dialog in
``update_panel.py``, or a recorder in the tests.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

from . import self_update as su
from . import update_check as uc
from .app_version import short_label

#: How often to look again whether the app has become free to restart.
WAIT_POLL_MS = 2000


def updates_enabled(settings: Any) -> bool:
    """*Settings → Advanced → Check for updates*; on when the key is missing.

    A settings.json from before this version has no such key and must keep
    loading unchanged -- and get the default, which is on.
    """
    if not isinstance(settings, dict):
        return True
    advanced = (settings.get("globals") or {}).get("advanced") or {}
    return bool(advanced.get("check_for_updates", True))


def _thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True, name="hires-update").start()


class UpdateController:
    IDLE = "idle"
    CHECKING = "checking"
    OFFERED = "offered"
    DOWNLOADING = "downloading"
    WAITING = "waiting"
    INSTALLING = "installing"

    def __init__(
        self,
        *,
        version: Optional[str],
        app_dir: str,
        exe_name: str,
        dispatch: Callable[..., None],
        schedule: Callable[[int, Callable[[], None]], Any],
        busy: Callable[[], bool],
        hold: Callable[[bool], None],
        quit_app: Callable[[], None],
        enabled: Callable[[], bool],
        view_factory: Callable[[], Any],
        can_self_install: bool,
        open_page: Callable[[str], None] = lambda url: None,
        log: Callable[[str], None] = print,
        channel: Optional[uc.Channel] = None,
        check_fn: Callable[..., uc.CheckResult] = uc.check,
        stage_fn: Callable[..., str] = su.stage_release,
        swap_fn: Callable[..., None] = su.swap_exe,
        restart_fn: Callable[..., Any] = su.restart,
        run_worker: Callable[[Callable[[], None]], None] = _thread,
        now: Callable[[], float] = time.time,
    ):
        self.version = version
        self.app_dir = app_dir
        self.exe_name = exe_name
        self._dispatch = dispatch
        self._schedule = schedule
        self._busy = busy
        self._hold = hold
        self._quit = quit_app
        self._enabled = enabled
        self._view_factory = view_factory
        self.can_self_install = can_self_install
        self._open_page = open_page
        self._log = log
        self.channel = channel or uc.channel_from_env()
        self._check = check_fn
        self._stage = stage_fn
        self._swap = swap_fn
        self._restart = restart_fn
        self._run_worker = run_worker
        self._now = now

        self.state = self.IDLE
        self.release: Optional[uc.ReleaseInfo] = None
        self.staged: str = ""
        self.view: Any = None
        self._cancelled = False
        if self.channel.is_test:
            self._log("[Update] Using the local test server instead of GitHub.")

    # -- checking ---------------------------------------------------------------
    def start_auto(self) -> bool:
        """The check on start: at most once a day, only when switched on."""
        if self.version is None or not self._enabled():
            return False
        state_file = su.state_path(self.app_dir)
        if not uc.check_due(uc.load_state(state_file), self._now()):
            return False
        return self._start_check(manual=False)

    def check_now(self) -> bool:
        """*About → Check for updates*: always asks, always answers."""
        if self.version is None:
            self._show("info", "This is a development build: it does not update itself.")
            return False
        return self._start_check(manual=True)

    def _start_check(self, manual: bool) -> bool:
        if self.state not in (self.IDLE, self.OFFERED):
            if manual:
                self._show("info", "An update is already in progress.")
            return False
        self.state = self.CHECKING

        def work():
            result = self._check(self.version, channel=self.channel)
            state_file = su.state_path(self.app_dir)
            state = uc.load_state(state_file)
            state["last_check"] = self._now()
            uc.save_state(state_file, state)
            self._dispatch(self._on_result, result, manual)

        self._run_worker(work)
        return True

    def _on_result(self, result: uc.CheckResult, manual: bool) -> None:
        self.state = self.IDLE
        self._log(f"[Update] {result.message}")
        if result.status == uc.AVAILABLE:
            self.release = result.release
            self.state = self.OFFERED
            self._show("offer", result.release, self.installable())
        elif manual and result.status == uc.UP_TO_DATE:
            self._show("info", f"OrpheusDL is up to date ({short_label(self.version)}).")
        elif manual:
            self._show("info", f"{result.message} Try again later.")

    def installable(self) -> bool:
        return bool(self.can_self_install and self.release and self.release.installable)

    # -- installing -------------------------------------------------------------
    def accept(self) -> bool:
        """*Update now*: download and verify in the background."""
        if self.state != self.OFFERED or not self.installable():
            return False
        self.state = self.DOWNLOADING
        self._cancelled = False
        release = self.release

        def work():
            try:
                staged = self._stage(
                    self.app_dir, release, self.channel,
                    progress=lambda done, total: self._dispatch(self._progress, done, total),
                    cancelled=lambda: self._cancelled,
                )
            except su.UpdateError as exc:
                self._dispatch(self._failed, str(exc))
                return
            except Exception as exc:  # pragma: no cover - last line of defence
                self._dispatch(self._failed, f"unexpected error ({type(exc).__name__})")
                return
            self._dispatch(self._downloaded, staged)

        self._run_worker(work)
        return True

    def _progress(self, done: int, total: int) -> None:
        # Blocks still queued after Cancel would open a fresh dialog otherwise.
        if not self._cancelled:
            self._show("progress", done, total)

    def _downloaded(self, staged: str) -> None:
        if self._cancelled:
            self._discard(staged)
            self.state = self.IDLE
            return
        self.staged = staged
        self._log(f"[Update] {self.release.tag} downloaded and verified (SHA-256).")
        self._try_install()

    def _try_install(self) -> None:
        if self._cancelled or self.state not in (self.DOWNLOADING, self.WAITING):
            return
        if self._busy():
            if self.state != self.WAITING:
                self.state = self.WAITING
                self._hold(True)
                self._log("[Update] Waiting until the running download has finished.")
                self._show("waiting")
            self._schedule(WAIT_POLL_MS, self._try_install)
            return
        self.state = self.INSTALLING
        self._show("installing")
        try:
            self._swap(self.app_dir, self.staged, exe_name=self.exe_name)
        except su.UpdateError as exc:
            if os.path.isfile(os.path.join(self.app_dir, self.exe_name)):
                self._discard(self.staged)
                self._failed(str(exc))
            else:
                # Neither EXE is in place: swap_exe kept the new one and says
                # how to rename it -- keep it, and don't claim nothing changed.
                self._failed(str(exc), replaced_nothing=False)
            return
        su.log(self.app_dir, f"auto-update {self.version} -> {self.release.tag}: exe replaced, restarting")
        self._log(f"[Update] Installed {self.release.tag}; restarting.")
        try:
            self._restart(self.app_dir, self.exe_name)
        except OSError as exc:
            self._hold(False)
            self.state = self.IDLE
            self._show(
                "failed",
                f"{self.release.tag} is installed but could not be started "
                f"({type(exc).__name__}). Close OrpheusDL and start it again.",
            )
            return
        self._quit()

    def cancel(self) -> None:
        """Cancel / Later. A download stops; a waiting update is dropped."""
        self._cancelled = True
        if self.state == self.WAITING:
            self._discard(self.staged)
            self._log("[Update] Update cancelled; the queue continues.")
        if self.state in (self.WAITING, self.OFFERED):
            self.state = self.IDLE
        self._hold(False)
        self._show("close")

    def open_release_page(self) -> None:
        url = self.release.page_url if self.release else self.channel.page_url
        self._open_page(url)

    def _failed(self, message: str, replaced_nothing: bool = True) -> None:
        self._hold(False)
        cancelled = self._cancelled
        self.state = self.IDLE
        if cancelled:
            self._log("[Update] Download cancelled.")
            return
        self._log(f"[Update] Update failed: {message}")
        tail = " Nothing was replaced." if replaced_nothing else ""
        self._show("failed", f"Update failed: {message}.{tail}")

    def _discard(self, staged: str) -> None:
        if staged:
            try:
                os.remove(staged)
            except OSError:
                pass
        self.staged = ""

    # -- view -------------------------------------------------------------------
    def _show(self, what: str, *args) -> None:
        if what == "close":
            if self.view is not None:
                self.view.close()
                self.view = None
            return
        if self.view is None:
            self.view = self._view_factory()
        getattr(self.view, what)(*args)

    def view_closed(self) -> None:
        """The dialog was closed by the user (window X): forget it."""
        self.view = None
