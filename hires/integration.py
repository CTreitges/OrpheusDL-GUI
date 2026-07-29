"""Glue between the hi-res queue and the stock GUI.

Design constraint: gui.py is a 22k-line module that owns the download loop. We
do not restructure it. Instead this runtime couples to exactly three things:

* the global ``download_process_active`` flag        (is a download running?)
* the global ``file_download_queue`` list            (is a batch still running?)
* the function ``_start_single_download(url, path, search_result_data)``

A tkinter ``after`` timer polls those. Whenever the GUI is idle and our queue
has work, we claim the next item and hand it over. That keeps the stock
download path -- including all of its per-platform quirks, rate-limit pauses
and cancellation handling -- completely untouched.

Determining whether a finished download *succeeded* is the one genuinely
awkward part: gui.py computes that inside a local closure we cannot reach. We
therefore classify by observable effect (did an audio file appear in the target
folder?) plus the cancellation flag. :meth:`HiresRuntime.report_result` lets
anything with better information override that verdict.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, List, Optional

from .models import AuthRequiredError, HiresError, QueueItem, QueueStatus
from .queue_store import QueueStore
from .quality import HIRES, build_search_result_data

#: Service name of the TIDAL module inside an Orpheus instance.
TIDAL_MODULE_NAME = "tidal"

#: Extensions OrpheusDL can produce. Used to detect that a download did something.
AUDIO_EXTENSIONS = frozenset(
    {".flac", ".m4a", ".mp3", ".opus", ".ogg", ".wav", ".aiff", ".ac3", ".mp4", ".aac"}
)

#: How often to check whether the GUI went idle.
DEFAULT_POLL_MS = 800

#: Grace period after handing a URL to the GUI before we trust the idle flag.
#: _start_single_download spins up a thread; without this we would see the
#: still-false flag and immediately think the download had finished.
DISPATCH_GRACE_SEC = 3.0


def _walk_audio_files(root: str) -> List[str]:
    """All audio files under ``root``. Empty list when the folder is absent."""
    found: List[str] = []
    if not root or not os.path.isdir(root):
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                found.append(os.path.join(dirpath, name))
    return found


def _newest_audio_mtime(root: str) -> float:
    """Most recent modification time among audio files under ``root``."""
    newest = 0.0
    for path in _walk_audio_files(root):
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest


class HiresRuntime:
    """Pumps the persistent queue into the stock GUI download loop."""

    def __init__(
        self,
        gui: Any,
        queue: QueueStore,
        *,
        poll_interval_ms: int = DEFAULT_POLL_MS,
        logger: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            gui: the gui.py module object (``sys.modules['__main__']`` when the
                app runs as a script).
            queue: the shared :class:`QueueStore`.
            logger: where to send status lines. Defaults to ``print``, which the
                GUI already captures into its log pane.
        """
        self.gui = gui
        self.queue = queue
        self.poll_interval_ms = max(200, int(poll_interval_ms))
        self._log = logger or print

        self._running = False
        self._after_id = None
        self._lock = threading.RLock()

        # State of the item we handed to the GUI.
        self._active_id: Optional[str] = None
        self._active_path: Optional[str] = None
        self._active_since: float = 0.0
        self._baseline_count: int = 0
        self._baseline_mtime: float = 0.0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> bool:
        """Begin polling. Safe to call twice."""
        if self._running:
            return True
        app = getattr(self.gui, "app", None)
        if app is None or not hasattr(app, "after"):
            self._log("[Hi-Res] Cannot start queue runner: GUI app not available.")
            return False
        self._running = True
        self._schedule()
        return True

    def stop(self) -> None:
        self._running = False
        app = getattr(self.gui, "app", None)
        if self._after_id is not None and app is not None:
            try:
                app.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = None

    def _schedule(self) -> None:
        if not self._running:
            return
        app = getattr(self.gui, "app", None)
        if app is None:
            return
        try:
            self._after_id = app.after(self.poll_interval_ms, self._tick)
        except Exception:
            # App is being torn down.
            self._running = False

    # -- polling ------------------------------------------------------------
    def _tick(self) -> None:
        try:
            self._pump()
        except Exception as exc:  # never let a bug kill the timer
            self._log(f"[Hi-Res] Queue runner error: {exc}")
        finally:
            self._schedule()

    def _pump(self) -> None:
        with self._lock:
            if self._active_id is not None:
                if self._gui_is_busy():
                    return
                if time.time() - self._active_since < DISPATCH_GRACE_SEC:
                    return  # download thread has not spun up yet
                self._finish_active()
                return

            if self.queue.is_paused or self._gui_is_busy():
                return
            item = self.queue.claim_next()
            if item is None:
                return
            self._dispatch(item)

    def _gui_is_busy(self) -> bool:
        """True while the stock GUI is downloading or has a batch pending."""
        if bool(getattr(self.gui, "download_process_active", False)):
            return True
        batch = getattr(self.gui, "file_download_queue", None)
        return bool(batch)

    def is_busy(self) -> bool:
        """True while anything is downloading -- the GUI's work or ours.

        Public because the Accounts tab needs it: re-authenticating TIDAL
        mid-transfer replaces the session the running download is using.
        """
        return self._active_id is not None or self._gui_is_busy()

    # -- dispatch / completion ---------------------------------------------
    def _dispatch(self, item: QueueItem) -> None:
        start = getattr(self.gui, "_start_single_download", None)
        if not callable(start):
            self.queue.mark_failed(item.id, "GUI download entry point not available")
            self._log("[Hi-Res] _start_single_download missing - cannot run queue.")
            return

        output_path = item.output_path or self.default_output_path()
        if output_path:
            try:
                os.makedirs(output_path, exist_ok=True)
            except OSError as exc:
                self.queue.mark_failed(item.id, f"Cannot create output folder: {exc}")
                return

        self._active_id = item.id
        self._active_path = output_path
        self._active_since = time.time()
        self._baseline_count = len(_walk_audio_files(output_path)) if output_path else 0
        self._baseline_mtime = _newest_audio_mtime(output_path) if output_path else 0.0

        search_result_data = build_search_result_data(
            quality=item.quality, media_type=item.media_kind
        )

        self._log(f"|GRAY|[Hi-Res] Queue -> {item.display_name}|RESET|")
        try:
            started = start(item.url, output_path, search_result_data)
        except Exception as exc:
            self._clear_active()
            self.queue.mark_failed(item.id, f"Download could not be started: {exc}")
            return

        if started is False:
            # GUI refused to start (missing credentials, invalid URL, busy).
            # Record it, then put it back in line if attempts remain -- claim_next
            # already counted this try, so a permanent refusal still terminates
            # after max_attempts instead of spinning forever.
            self._clear_active()
            reason = "GUI refused to start this download"
            self.queue.mark_failed(item.id, reason)
            if self.queue.retry(item.id):
                self._log(f"|GRAY|[Hi-Res] {reason}; re-queued.|RESET|")

    def _finish_active(self) -> None:
        item_id = self._active_id
        path = self._active_path
        if item_id is None:
            return
        # Read the baseline before clearing it -- _clear_active() resets the
        # counters that _produced_audio compares against.
        baseline = (self._baseline_count, self._baseline_mtime)
        self._clear_active()

        if self._was_cancelled():
            self.queue.mark_cancelled(item_id)
            self._log("|GRAY|[Hi-Res] Item cancelled.|RESET|")
            return

        produced, detail = self._produced_audio(path, baseline)
        if produced:
            self.queue.mark_done(item_id)
        else:
            self.queue.mark_failed(item_id, detail)
            self._log(f"|GRAY|[Hi-Res] Item failed: {detail}|RESET|")

    def _clear_active(self) -> None:
        self._active_id = None
        self._active_path = None
        self._active_since = 0.0
        self._baseline_count = 0
        self._baseline_mtime = 0.0

    def _was_cancelled(self) -> bool:
        stop_event = getattr(self.gui, "stop_event", None)
        try:
            return bool(stop_event is not None and stop_event.is_set())
        except Exception:
            return False

    def _produced_audio(self, path: Optional[str], baseline: tuple) -> tuple:
        """Did the finished download actually write audio?

        Compares the target folder against ``baseline`` -- the (count, newest
        mtime) pair captured when the download started. Returns
        ``(success, detail)``. Without a known target folder we have no evidence
        either way, so we give the download the benefit of the doubt.
        """
        if not path:
            return True, ""
        baseline_count, baseline_mtime = baseline
        if len(_walk_audio_files(path)) > baseline_count:
            return True, ""
        if _newest_audio_mtime(path) > baseline_mtime:
            return True, ""  # existing file was overwritten
        return False, "No audio file appeared in the target folder"

    # -- external verdict ---------------------------------------------------
    def report_result(self, success: bool, error: str = "") -> None:
        """Override the heuristic when the caller knows what happened.

        Call this from gui.py if you ever wire a real completion callback.
        """
        with self._lock:
            item_id = self._active_id
            if item_id is None:
                return
            self._clear_active()
            if success:
                self.queue.mark_done(item_id)
            else:
                self.queue.mark_failed(item_id, error or "Download failed")

    # -- settings -----------------------------------------------------------
    def default_output_path(self) -> Optional[str]:
        """The download folder configured in the GUI settings."""
        settings = getattr(self.gui, "current_settings", None)
        if not isinstance(settings, dict):
            return None
        general = (settings.get("globals") or {}).get("general") or {}
        path = general.get("output_path") or general.get("download_path")
        return path if isinstance(path, str) and path.strip() else None

    def default_quality(self) -> Optional[str]:
        settings = getattr(self.gui, "current_settings", None)
        if not isinstance(settings, dict):
            return None
        general = (settings.get("globals") or {}).get("general") or {}
        quality = general.get("quality") or general.get("download_quality")
        return quality if isinstance(quality, str) and quality.strip() else None


# ---------------------------------------------------------------------------
# Entry point used by gui.py
# ---------------------------------------------------------------------------

def default_queue_path(gui: Any = None) -> str:
    """Where the queue file lives: next to the app's other config."""
    base = None
    if gui is not None:
        base = getattr(gui, "application_path", None)
    if not base:
        base = os.getcwd()
    return os.path.join(base, "config", "hires_queue.json")


def install(gui: Any, *, queue_path: Optional[str] = None) -> Optional[HiresRuntime]:
    """Create the queue, start the runtime, and hand it back to gui.py.

    Returns ``None`` (after logging) if anything goes wrong -- a broken hi-res
    suite must never prevent the normal GUI from starting.
    """
    try:
        store = QueueStore(queue_path or default_queue_path(gui))
        runtime = HiresRuntime(gui, store)
        runtime.start()
        return runtime
    except Exception as exc:
        try:
            print(f"[Hi-Res] Disabled: {exc}")
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Service providers
#
# All of these are called lazily, every time a tab needs them, so the user can
# install a module or log in *after* the GUI has already started and things
# simply begin working.
# ---------------------------------------------------------------------------

def _spotify_credentials(gui: Any) -> tuple:
    """Client id/secret from the GUI's Spotify settings.

    Reuses what the user already entered for Spotify downloads instead of
    asking for a second set of credentials.
    """
    settings = getattr(gui, "current_settings", None)
    if not isinstance(settings, dict):
        return "", ""
    creds = (settings.get("credentials") or {}).get("Spotify") or {}
    return (creds.get("client_id") or "").strip(), (creds.get("client_secret") or "").strip()


def make_tidal_library_provider(gui: Any) -> Callable[[], Any]:
    def provider():
        from .tidal_library import TidalLibrary

        return TidalLibrary.from_orpheus(getattr(gui, "orpheus_instance", None))

    return provider


def make_matcher_provider(gui: Any) -> Callable[[], Any]:
    def provider():
        from .matcher import TrackMatcher
        from .tidal_library import TidalLibrary

        library = TidalLibrary.from_orpheus(getattr(gui, "orpheus_instance", None))
        if library is None:
            return None
        return TrackMatcher(library)

    return provider


def make_spotify_source_provider(gui: Any) -> Callable[[], Any]:
    def provider():
        from .spotify_source import SpotifySource

        client_id, client_secret = _spotify_credentials(gui)
        base = getattr(gui, "application_path", None) or os.getcwd()
        token_path = os.path.join(base, "config", "hires_spotify_tokens.json")
        # Without credentials this still works: the embed backend handles
        # public playlist links anonymously.
        return SpotifySource.from_config(
            client_id,
            client_secret,
            token_path=token_path,
            redirect_uri=spotify_redirect_uri(gui),
        )

    return provider


def spotify_redirect_uri(gui: Any) -> str:
    """Where the sign-in listens for Spotify's callback.

    Defaults to the URI the GUI's own Spotify setup panel hands out for
    registering, so anyone who followed that panel can sign in without
    configuring anything else. Overridable for people whose Spotify app is
    registered against a different one -- Spotify only redirects to URIs the
    app actually lists, so this has to be able to follow.
    """
    from .spotify_source import DEFAULT_REDIRECT_URI

    settings = getattr(gui, "current_settings", None)
    if isinstance(settings, dict):
        creds = (settings.get("credentials") or {}).get("Spotify") or {}
        configured = (creds.get("redirect_uri") or "").strip()
        if configured:
            return configured
    return DEFAULT_REDIRECT_URI


def _tidal_module(gui: Any) -> Any:
    """The loaded TIDAL ``ModuleInterface``, loading it if it is not up yet.

    Returns ``None`` when TIDAL is not installed. Never raises: every caller
    treats "no module" as a state to display, not an error.
    """
    orpheus = getattr(gui, "orpheus_instance", None)
    if orpheus is None:
        return None

    loaded = getattr(orpheus, "loaded_modules", None)
    module = loaded.get(TIDAL_MODULE_NAME) if isinstance(loaded, dict) else None
    if module is not None:
        return module

    known = getattr(orpheus, "module_list", None) or ()
    loader = getattr(orpheus, "load_module", None)
    try:
        available = TIDAL_MODULE_NAME in known
    except TypeError:
        available = False
    if not available or not callable(loader):
        return None
    try:
        return loader(TIDAL_MODULE_NAME)
    except Exception:
        return None


def make_tidal_status_provider(gui: Any) -> Callable[[], Any]:
    """Report TIDAL's sign-in state, resolved fresh on every call."""

    def provider():
        from .models import AccountState, AccountStatus
        from .tidal_library import TidalLibrary

        def status(state, detail="", account="", hint=""):
            return AccountStatus(
                service="TIDAL", state=state, detail=detail, account=account, hint=hint
            )

        if _tidal_module(gui) is None:
            return status(
                AccountState.UNAVAILABLE,
                "TIDAL module not installed.",
                hint="Install it under Settings > TIDAL.",
            )

        try:
            library = TidalLibrary.from_orpheus(getattr(gui, "orpheus_instance", None))
        except AuthRequiredError:
            # The module is there, nobody is logged in -- exactly what the
            # sign-in button is for.
            library = None
        except Exception as exc:
            return status(AccountState.UNAVAILABLE, str(exc))

        if library is None:
            return status(
                AccountState.SIGNED_OUT,
                "Browsing as guest. Sign in to download in hi-res.",
            )

        user_id = ""
        try:
            user_id = library.current_user_id() or ""
        except Exception:
            pass
        return status(
            AccountState.SIGNED_IN,
            "Ready for hi-res downloads.",
            account=f"User {user_id}" if user_id else "",
        )

    return provider


def make_tidal_sign_in(gui: Any) -> Optional[Callable[[], Any]]:
    """A blocking callable that runs TIDAL's device flow, or ``None``.

    Resolution is deferred to call time: at tab-build time the TIDAL module is
    usually not loaded yet, so deciding now would permanently disable the
    button. The returned callable blocks for the whole browser consent window,
    so callers must run it off the main thread.
    """

    def sign_in():
        module = _tidal_module(gui)
        if module is None:
            raise HiresError(
                "TIDAL module not available. Install it under Settings > TIDAL."
            )
        ensure = getattr(module, "_ensure_credentials", None)
        if not callable(ensure):
            raise HiresError(
                "This version of the TIDAL module cannot be signed in from here. "
                "Start a download to trigger its own login."
            )
        # force=True is what makes it prompt: without it the module stays in
        # guest mode on purpose whenever ORPHEUS_GUI is set. The module holds
        # its own lock, so a concurrent trigger is already serialised.
        ensure(force=True)

    return sign_in


def make_spotify_status_provider(gui: Any) -> Callable[[], Any]:
    """Report Spotify's sign-in state, resolved fresh on every call."""
    source_provider = make_spotify_source_provider(gui)

    def provider():
        from .models import AccountState, AccountStatus

        def status(state, detail="", account="", hint=""):
            return AccountStatus(
                service="Spotify", state=state, detail=detail, account=account, hint=hint
            )

        try:
            source = source_provider()
        except Exception as exc:
            return status(AccountState.UNAVAILABLE, str(exc))
        if source is None:
            return status(AccountState.UNAVAILABLE, "Spotify is not configured.")

        # No client id is a precondition, not a failed login: the sign-in
        # cannot even be attempted, and saying "not signed in" would send the
        # user to a button that can only fail.
        if not source.has_web_api():
            return status(
                AccountState.NEEDS_SETUP,
                "Client ID and Secret missing — only public playlist links work.",
                hint="Enter them under Settings > Spotify, then come back here.",
            )

        if not source.is_user_authorized():
            # Naming the redirect URI here is the difference between a sign-in
            # that hangs for five minutes and one the user can fix: Spotify
            # only redirects back to URIs their app actually lists.
            return status(
                AccountState.SIGNED_OUT,
                "Sign in to reach your own playlists and Liked Songs.",
                hint=(
                    "Your Spotify app must list this redirect URI: "
                    f"{spotify_redirect_uri(gui)}"
                ),
            )

        account = ""
        try:
            profile = source.web.current_user()
            account = (profile.get("display_name") or profile.get("id") or "").strip()
        except Exception:
            # A stored token that cannot be spent is still a token; the next
            # real call reports the problem properly.
            pass
        return status(
            AccountState.SIGNED_IN, "Your playlists and Liked Songs are readable.", account=account
        )

    return provider


def setup_tabs(gui: Any, tabview: Any, *, queue_path: Optional[str] = None) -> Optional[dict]:
    """Add the Queue / TIDAL / Spotify tabs and start the queue runner.

    This is the single entry point gui.py calls. Everything is wrapped so a
    failure here degrades to "the three extra tabs are missing" rather than
    breaking the application.

    Returns a dict with ``runtime``, ``queue`` and ``tabs``, or ``None``.
    """
    try:
        from .gui_panel import build_tabs
    except Exception as exc:
        print(f"[Hi-Res] Tabs unavailable: {exc}")
        return None

    try:
        store = QueueStore(queue_path or default_queue_path(gui))
        runtime = HiresRuntime(gui, store)

        tabs = build_tabs(
            tabview,
            queue=store,
            app=getattr(gui, "app", None),
            tidal_library_provider=make_tidal_library_provider(gui),
            spotify_source_provider=make_spotify_source_provider(gui),
            matcher_provider=make_matcher_provider(gui),
            # Deliberately NOT the GUI's global quality: the whole point of
            # this suite is hi-res, and both tabs say so on screen. Reading the
            # global setting here silently downgraded every converted playlist
            # to whatever the user had picked for normal downloads.
            quality_provider=lambda: HIRES,
            output_provider=runtime.default_output_path,
            tidal_status_provider=make_tidal_status_provider(gui),
            spotify_status_provider=make_spotify_status_provider(gui),
            tidal_sign_in=make_tidal_sign_in(gui),
            # Signing in mid-download would swap the TIDAL session out from
            # under the running transfer.
            busy_provider=runtime.is_busy,
        )
        runtime.start()

        pending = store.stats().remaining
        if pending:
            print(f"|GRAY|[Hi-Res] Restored {pending} queued item(s).|RESET|")
        return {"runtime": runtime, "queue": store, "tabs": tabs}
    except Exception as exc:
        print(f"[Hi-Res] Disabled: {exc}")
        return None
