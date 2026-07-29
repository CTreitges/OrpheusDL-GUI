"""UI logic for the hi-res tabs, without any tkinter.

Everything the new tabs *do* lives here; ``gui_panel`` only draws widgets and
forwards clicks. That split keeps the interesting behaviour testable on a
machine that has no tkinter at all, and it keeps the widget code short enough
to read.

Long-running work (network calls, matching a 300-track playlist) runs on a
worker thread. Results come back through ``dispatch``, which marshals them onto
the tkinter main thread. Tests inject a synchronous dispatcher.
"""

from __future__ import annotations

import queue
import secrets
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from .converter import PlaylistConverter, apply_review, queue_items_for_tracks
from .models import (
    AccountState,
    AccountStatus,
    AuthRequiredError,
    ConversionReport,
    HiresError,
    MatchDecision,
    MatchResult,
    PlaylistRef,
    QueueItem,
    QueueStatus,
    TrackRef,
)
from .quality import HIRES, label_for, normalize_tier

# ---------------------------------------------------------------------------
# Threading helpers
# ---------------------------------------------------------------------------


class UiDispatcher:
    """Marshals a callable onto the tkinter main thread.

    ``widget.after()`` is **not** thread-safe: called from a worker thread it is
    silently dropped, so the callback never runs and the UI sits there showing a
    spinner forever. Everything here runs on worker threads, so instead we hand
    work over through a queue that a timer on the main thread drains.

    The timer is started from ``__init__``, which the GUI calls while building
    its widgets -- i.e. on the main thread, where scheduling is legal.

    Without an app object the callable runs immediately: that is what tests want,
    and also the right behaviour once the GUI is gone.
    """

    #: How often the main thread looks for work handed over by a worker.
    POLL_MS = 40

    def __init__(self, app: Any = None, poll_ms: int = POLL_MS):
        self.app = app
        self.poll_ms = max(10, int(poll_ms))
        self._pending: "queue.Queue" = queue.Queue()
        self._stopped = False
        if app is not None and hasattr(app, "after"):
            self._schedule()

    def __call__(self, fn: Callable, *args, **kwargs) -> None:
        app = self.app
        if app is None or not hasattr(app, "after") or self._stopped:
            self._safe(fn, *args, **kwargs)
            return
        # Safe from any thread: Queue is synchronised, tkinter is not touched.
        self._pending.put((fn, args, kwargs))

    def _schedule(self) -> None:
        if self._stopped:
            return
        try:
            self.app.after(self.poll_ms, self._drain)
        except Exception:
            # App is being torn down.
            self._stopped = True

    def _drain(self) -> None:
        while True:
            try:
                fn, args, kwargs = self._pending.get_nowait()
            except queue.Empty:
                break
            self._safe(fn, *args, **kwargs)
        self._schedule()

    def stop(self) -> None:
        self._stopped = True

    @staticmethod
    def _safe(fn: Callable, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()


def run_in_background(fn: Callable, name: str = "hires-worker") -> threading.Thread:
    """Run ``fn`` on a daemon thread so closing the app never blocks."""
    thread = threading.Thread(target=fn, name=name, daemon=True)
    thread.start()
    return thread


def _describe(exc: BaseException) -> str:
    """A message worth showing a user."""
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _open_in_browser(url: str) -> None:
    """Open the Spotify consent page. Imported lazily; webbrowser is optional."""
    import webbrowser

    webbrowser.open(url)


def _default_wait_for_code(redirect_uri: str, state: str) -> str:
    """Listen on the redirect URI for Spotify's authorization code."""
    from .spotify_source import wait_for_authorization_code

    return wait_for_authorization_code(redirect_uri, expected_state=state)


# ---------------------------------------------------------------------------
# Queue tab
# ---------------------------------------------------------------------------

#: What each status looks like in the queue list.
STATUS_SYMBOLS = {
    QueueStatus.PENDING.value: "•",
    QueueStatus.ACTIVE.value: "▶",
    QueueStatus.DONE.value: "✓",
    QueueStatus.FAILED.value: "✗",
    QueueStatus.CANCELLED.value: "–",
}


@dataclass
class QueueRow:
    """One line in the queue list, ready to render."""

    id: str
    symbol: str
    status: str
    title: str
    detail: str
    quality: str
    error: str = ""
    group_label: str = ""

    @property
    def text(self) -> str:
        parts = [f"{self.symbol} {self.title}"]
        if self.detail:
            parts.append(self.detail)
        return "  —  ".join(parts)


class QueueController:
    """Drives the queue tab."""

    def __init__(self, queue):
        self.queue = queue

    # -- display ------------------------------------------------------------
    def rows(self, status: Optional[str] = None) -> List[QueueRow]:
        return [self._row(item) for item in self.queue.list(status=status)]

    @staticmethod
    def _row(item: QueueItem) -> QueueRow:
        detail_parts = [p for p in (item.subtitle, item.group_label) if p]
        return QueueRow(
            id=item.id,
            symbol=STATUS_SYMBOLS.get(item.status, "?"),
            status=item.status,
            title=item.title or item.url,
            detail=" · ".join(detail_parts),
            quality=label_for(item.quality) if item.quality else "",
            error=item.error or "",
            group_label=item.group_label,
        )

    def summary(self) -> str:
        s = self.queue.stats()
        if s.total == 0:
            return "Queue is empty"
        bits = [f"{s.total} item{'s' if s.total != 1 else ''}"]
        if s.pending:
            bits.append(f"{s.pending} waiting")
        if s.active:
            bits.append(f"{s.active} running")
        if s.done:
            bits.append(f"{s.done} done")
        if s.failed:
            bits.append(f"{s.failed} failed")
        if self.queue.is_paused:
            bits.append("PAUSED")
        return " · ".join(bits)

    # -- actions ------------------------------------------------------------
    def toggle_pause(self) -> bool:
        """Flip the pause flag. Returns the new state."""
        new_state = not self.queue.is_paused
        self.queue.set_paused(new_state)
        return new_state

    def retry(self, item_id: str) -> bool:
        return self.queue.retry(item_id)

    def retry_all_failed(self) -> int:
        return self.queue.requeue_failed()

    def remove(self, item_id: str) -> bool:
        return self.queue.remove(item_id)

    def move_up(self, item_id: str) -> bool:
        return self.queue.move(item_id, -1)

    def move_down(self, item_id: str) -> bool:
        return self.queue.move(item_id, +1)

    def clear_finished(self) -> int:
        return self.queue.clear_finished()

    def clear_all(self) -> int:
        return self.queue.clear_all()

    def subscribe(self, callback: Callable[[], None]):
        return self.queue.subscribe(callback)


# ---------------------------------------------------------------------------
# TIDAL playlist browser tab
# ---------------------------------------------------------------------------

class TidalBrowserController:
    """Lists the user's TIDAL playlists and queues them for download."""

    def __init__(
        self,
        library_provider: Callable[[], Any],
        queue,
        *,
        dispatch: Optional[UiDispatcher] = None,
        quality_provider: Optional[Callable[[], str]] = None,
        output_provider: Optional[Callable[[], Optional[str]]] = None,
        folder_per_playlist: bool = True,
    ):
        """
        Args:
            library_provider: returns a ``TidalLibrary`` (or raises). Called
                lazily so the tab works even if TIDAL is set up after startup.
            quality_provider: returns the tier to download at. Defaults to hi-res.
            output_provider: returns the base download folder.
        """
        self.library_provider = library_provider
        self.queue = queue
        self.dispatch = dispatch or UiDispatcher()
        self.quality_provider = quality_provider or (lambda: HIRES)
        self.output_provider = output_provider or (lambda: None)
        self.folder_per_playlist = folder_per_playlist
        self.playlists: List[PlaylistRef] = []

    # -- loading ------------------------------------------------------------
    def load_playlists(
        self,
        on_done: Callable[[List[PlaylistRef]], None],
        on_error: Callable[[str], None],
        *,
        include_favorites: bool = True,
    ) -> threading.Thread:
        def work():
            try:
                library = self.library_provider()
                if library is None:
                    raise HiresError(
                        "TIDAL module not available. Install it and log in under Settings > TIDAL."
                    )
                playlists = library.list_playlists(include_favorites=include_favorites)
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            self.playlists = list(playlists)
            self.dispatch(on_done, self.playlists)

        return run_in_background(work, name="hires-tidal-playlists")

    def load_tracks(
        self,
        playlist: PlaylistRef,
        on_done: Callable[[List[TrackRef]], None],
        on_error: Callable[[str], None],
    ) -> threading.Thread:
        def work():
            try:
                library = self.library_provider()
                if library is None:
                    raise HiresError("TIDAL module not available.")
                tracks = library.get_playlist_tracks(playlist.id)
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            self.dispatch(on_done, list(tracks))

        return run_in_background(work, name="hires-tidal-tracks")

    # -- queueing -----------------------------------------------------------
    def enqueue_playlist_as_one(self, playlist: PlaylistRef) -> QueueItem:
        """Queue the playlist URL itself and let OrpheusDL expand it.

        Cheaper than resolving every track, and it keeps OrpheusDL's own
        playlist handling (m3u writing, folder naming) intact.
        """
        from .tidal_library import playlist_url

        item = QueueItem(
            url=playlist.url or playlist_url(playlist.id),
            title=playlist.name or playlist.id,
            subtitle=playlist.owner,
            platform="TIDAL",
            media_kind="playlist",
            output_path=self.output_provider(),
            quality=normalize_tier(self.quality_provider()),
            group_label=playlist.name or playlist.id,
            source={"kind": "tidal_playlist", "tidal_playlist_id": playlist.id},
        )
        return self.queue.add(item)

    def enqueue_tracks(
        self, playlist: PlaylistRef, tracks: Sequence[TrackRef]
    ) -> List[QueueItem]:
        """Queue individual tracks, e.g. after the user picked a subset."""
        import os

        from .converter import sanitize_folder_name

        base = self.output_provider()
        target = base
        if base and self.folder_per_playlist:
            target = os.path.join(
                base, sanitize_folder_name(playlist.name or playlist.id, fallback="TIDAL Playlist")
            )

        items = queue_items_for_tracks(
            tracks,
            quality=normalize_tier(self.quality_provider()),
            output_path=target,
            group_id=f"tidal-{playlist.id}-{int(time.time())}",
            group_label=playlist.name or playlist.id,
        )
        return self.queue.add_many(items)


# ---------------------------------------------------------------------------
# Spotify import tab
# ---------------------------------------------------------------------------

@dataclass
class ReviewRow:
    """One uncertain match, ready to show in the review list."""

    source_id: str
    source_label: str
    match_label: str
    score: float
    reasons: str
    alternatives: List[tuple]  # (tidal_id, label, score)

    @property
    def score_text(self) -> str:
        return f"{self.score * 100:.0f}%"


class SpotifyImportController:
    """Converts a Spotify playlist to TIDAL and queues it at hi-res."""

    def __init__(
        self,
        source_provider: Callable[[], Any],
        matcher_provider: Callable[[], Any],
        queue,
        *,
        dispatch: Optional[UiDispatcher] = None,
        quality_provider: Optional[Callable[[], str]] = None,
        output_provider: Optional[Callable[[], Optional[str]]] = None,
        open_url: Optional[Callable[[str], Any]] = None,
        wait_for_code: Optional[Callable[..., str]] = None,
    ):
        self.source_provider = source_provider
        self.matcher_provider = matcher_provider
        self.queue = queue
        self.dispatch = dispatch or UiDispatcher()
        self.quality_provider = quality_provider or (lambda: HIRES)
        self.output_provider = output_provider or (lambda: None)
        # Injectable so the sign-in flow can be tested without a browser.
        self._open_url = open_url or _open_in_browser
        self._wait_for_code = wait_for_code or _default_wait_for_code

        self.playlists: List[PlaylistRef] = []
        self.report: Optional[ConversionReport] = None
        self._cancel = threading.Event()

    # -- sign-in ------------------------------------------------------------
    def _authorize_blocking(self, source: Any, on_status: Optional[Callable[[str], None]]) -> None:
        """Run the PKCE dance. Blocking; call from a worker thread.

        Spotify only hands out private playlists and Liked Songs to a
        user-authorized token, so this has to happen before the first listing.
        """
        backend = getattr(source, "web", None)
        if backend is None or not getattr(backend, "client_id", ""):
            raise AuthRequiredError(
                "Signing in to Spotify needs a Client ID and Secret. "
                "Enter them under Settings > Spotify, then try again."
            )

        state = secrets.token_urlsafe(16)
        url, verifier = backend.begin_authorization(state=state)
        if on_status:
            self.dispatch(on_status, "Opening your browser to sign in to Spotify…")
        self._open_url(url)
        code = self._wait_for_code(backend.redirect_uri, state)
        backend.exchange_code(code, verifier)

    def sign_in(
        self,
        on_done: Callable[[], None],
        on_error: Callable[[str], None],
        on_status: Optional[Callable[[str], None]] = None,
    ) -> threading.Thread:
        """Authorize without listing anything (explicit "Sign in" button)."""

        def work():
            try:
                source = self.source_provider()
                if source is None:
                    raise HiresError("Spotify is not configured.")
                self._authorize_blocking(source, on_status)
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            self.dispatch(on_done)

        return run_in_background(work, name="hires-spotify-signin")

    # -- loading ------------------------------------------------------------
    def load_playlists(
        self,
        on_done: Callable[[List[PlaylistRef]], None],
        on_error: Callable[[str], None],
        on_status: Optional[Callable[[str], None]] = None,
    ) -> threading.Thread:
        """List the user's playlists, signing in first if that has not happened."""

        def work():
            try:
                source = self.source_provider()
                if source is None:
                    raise HiresError("Spotify is not configured.")
                # A source that cannot report its auth state is left alone: let
                # list_user_playlists raise AuthRequiredError on its own terms.
                is_authorized = getattr(source, "is_user_authorized", None)
                if callable(is_authorized) and not is_authorized():
                    self._authorize_blocking(source, on_status)
                    if on_status:
                        self.dispatch(on_status, "Loading your playlists…")
                playlists = source.list_user_playlists()
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            self.playlists = list(playlists)
            self.dispatch(on_done, self.playlists)

        return run_in_background(work, name="hires-spotify-playlists")

    # -- conversion ---------------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()

    def convert(
        self,
        playlist: Any,
        on_progress: Callable[[int, int, Optional[MatchResult]], None],
        on_done: Callable[[ConversionReport], None],
        on_error: Callable[[str], None],
    ) -> threading.Thread:
        """Match a Spotify playlist against TIDAL. Queues nothing yet."""
        self._cancel.clear()

        def work():
            try:
                converter = self._build_converter()

                def progress(done, total, result):
                    if self._cancel.is_set():
                        raise HiresError("Conversion cancelled")
                    self.dispatch(on_progress, done, total, result)

                report = converter.convert(playlist, progress_callback=progress)
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            self.report = report
            self.dispatch(on_done, report)

        return run_in_background(work, name="hires-spotify-convert")

    def _build_converter(self) -> PlaylistConverter:
        source = self.source_provider()
        if source is None:
            raise HiresError("Spotify is not configured.")
        matcher = self.matcher_provider()
        if matcher is None:
            raise HiresError(
                "TIDAL module not available. Install it and log in under Settings > TIDAL."
            )
        return PlaylistConverter(
            source,
            matcher,
            self.queue,
            quality=normalize_tier(self.quality_provider()),
            output_root=self.output_provider(),
        )

    # -- review -------------------------------------------------------------
    def review_rows(self, report: Optional[ConversionReport] = None) -> List[ReviewRow]:
        report = report or self.report
        if report is None:
            return []
        rows = []
        for result in report.needs_review:
            best = result.best
            rows.append(
                ReviewRow(
                    source_id=result.source.id,
                    source_label=result.source.display_name,
                    match_label=best.track.display_name if best else "(nothing found)",
                    score=float(best.score) if best else 0.0,
                    reasons="; ".join(best.reasons) if best and best.reasons else "",
                    alternatives=[
                        (c.track.id, c.track.display_name, float(c.score))
                        for c in result.alternatives
                    ],
                )
            )
        return rows

    def apply_review_and_enqueue(
        self,
        decisions: Dict[str, Any],
        *,
        report: Optional[ConversionReport] = None,
        include_unreviewed: bool = False,
    ) -> List[QueueItem]:
        """Apply the user's choices, then queue everything accepted."""
        report = report or self.report
        if report is None:
            return []
        apply_review(report, decisions)
        converter = self._build_converter()
        return converter.enqueue(report, include_review=include_unreviewed)

    def enqueue_confident_only(
        self, report: Optional[ConversionReport] = None
    ) -> List[QueueItem]:
        """Queue the certain matches and leave the rest for review."""
        report = report or self.report
        if report is None:
            return []
        return self._build_converter().enqueue(report, include_review=False)

    # -- display ------------------------------------------------------------
    @staticmethod
    def progress_text(done: int, total: int) -> str:
        if total <= 0:
            return "Matching…"
        return f"Matching {done}/{total} ({done * 100 // total}%)"

    @staticmethod
    def result_summary(report: ConversionReport) -> str:
        c = report.counts
        return (
            f"{c.get(MatchDecision.AUTO_ACCEPT.value, 0)} matched · "
            f"{c.get(MatchDecision.NEEDS_REVIEW.value, 0)} to review · "
            f"{c.get(MatchDecision.NO_MATCH.value, 0)} not found"
        )


# ---------------------------------------------------------------------------
# Accounts tab
# ---------------------------------------------------------------------------

#: Shown while TIDAL's device flow waits for the user to finish in the browser.
TIDAL_SIGN_IN_HINT = (
    "Finish the sign-in in your browser. This window keeps waiting until you do."
)


class AccountsController:
    """Sign in to TIDAL and Spotify before queueing anything.

    Both services can also be signed into implicitly -- TIDAL when a download
    needs credentials, Spotify when "My playlists" is first clicked. This
    controller only makes that step visible and movable to the front, so the
    first download is not the thing that suddenly opens a browser window.

    Reading a status hits the network -- Spotify asks ``/me`` for the account
    name, TIDAL may have to load its module first -- so it happens on a worker
    thread and the result is pushed back through ``dispatch``. Calling the
    providers straight from a click handler froze the window for the length of
    an HTTP round trip, which is what made the Refresh button feel broken.

    The last result is kept only so the tab has something to paint while the
    next read is in flight; it is never used *instead* of reading. The TIDAL
    module is usually not loaded when the tabs are built, so a status decided
    once at startup would say "unavailable" for the rest of the session.
    """

    #: Services in the order they are shown.
    SERVICES = ("TIDAL", "Spotify")

    #: What to show before the first read comes back.
    UNKNOWN_DETAIL = "Checking…"

    def __init__(
        self,
        *,
        tidal_status_provider: Callable[[], AccountStatus],
        spotify_status_provider: Callable[[], AccountStatus],
        tidal_sign_in: Optional[Callable[[], Any]] = None,
        spotify_controller: Optional["SpotifyImportController"] = None,
        dispatch: Optional[UiDispatcher] = None,
        busy_provider: Optional[Callable[[], bool]] = None,
    ):
        """
        Args:
            tidal_sign_in: blocking callable that drives TIDAL's device flow.
                ``None`` when the TIDAL module is not installed.
            spotify_controller: reused so both tabs share one Spotify source and
                one set of tokens.
            busy_provider: True while a download is running. Signing in mid-
                download would swap the session out from under it.
        """
        self.tidal_status_provider = tidal_status_provider
        self.spotify_status_provider = spotify_status_provider
        self.tidal_sign_in_callable = tidal_sign_in
        self.spotify_controller = spotify_controller
        self.dispatch = dispatch or UiDispatcher()
        self.busy_provider = busy_provider or (lambda: False)

        # Guards against a second click while a browser flow is still open.
        self._in_flight: Dict[str, bool] = {}

        # Last known status per service, for painting while a read is running.
        self._last: Dict[str, AccountStatus] = {}
        self._reading = False

    # -- status -------------------------------------------------------------
    def statuses(self) -> List[AccountStatus]:
        """Read every service's state. **Blocking** -- call from a worker.

        Kept public because tests and the sign-in worker use it directly. UI
        code wants :meth:`refresh` instead.
        """
        return [self.status_for(service) for service in self.SERVICES]

    def status_for(self, service: str) -> AccountStatus:
        """Read one service's state. **Blocking** -- this can do HTTP."""
        provider = (
            self.tidal_status_provider
            if service == "TIDAL"
            else self.spotify_status_provider
        )
        try:
            status = provider()
        except Exception as exc:
            # A broken provider must not blank the whole tab.
            status = AccountStatus(
                service=service,
                state=AccountState.UNAVAILABLE,
                detail=_describe(exc),
            )
        self._last[service] = status
        return status

    def known_statuses(self) -> List[AccountStatus]:
        """What was last read, without touching the network.

        Safe from the UI thread; that is the whole point of it.
        """
        return [
            self._last.get(service) or self._placeholder(service)
            for service in self.SERVICES
        ]

    def _placeholder(self, service: str) -> AccountStatus:
        return AccountStatus(
            service=service,
            state=AccountState.UNAVAILABLE,
            detail=self.UNKNOWN_DETAIL,
        )

    @property
    def is_reading(self) -> bool:
        return self._reading

    def refresh(
        self,
        on_done: Callable[[List[AccountStatus]], None],
        on_error: Optional[Callable[[str], None]] = None,
    ) -> Optional[threading.Thread]:
        """Re-read every status off the UI thread.

        Returns ``None`` when a read is already running -- the answer would be
        the same, and two concurrent reads would just race to repaint.
        """
        if self._reading:
            return None
        self._reading = True

        def work():
            try:
                statuses = self.statuses()
            except Exception as exc:
                self._reading = False
                if on_error is not None:
                    self.dispatch(on_error, _describe(exc))
                return
            self._reading = False
            self.dispatch(on_done, statuses)

        return run_in_background(work, name="hires-accounts-status")

    def is_busy(self, service: str) -> bool:
        return bool(self._in_flight.get(service))

    def summary(self) -> str:
        """One line about what is signed in. Uses the last read, never the network."""
        if self._reading and not self._last:
            return self.UNKNOWN_DETAIL
        signed_in = [s.service for s in self.known_statuses() if s.is_signed_in]
        if not signed_in:
            return "Not signed in to any service."
        return "Signed in: " + ", ".join(signed_in)

    # -- sign in ------------------------------------------------------------
    def sign_in(
        self,
        service: str,
        on_done: Callable[[], None],
        on_error: Callable[[str], None],
        on_status: Optional[Callable[[str], None]] = None,
    ) -> Optional[threading.Thread]:
        """Start the sign-in flow for one service.

        Returns the worker thread, or ``None`` when the click was refused (a
        flow is already open, or a download is running).
        """
        if self.is_busy(service):
            self.dispatch(on_error, "A sign-in is already in progress.")
            return None
        if self.busy_provider():
            self.dispatch(
                on_error,
                "A download is running. Wait for it to finish before signing in.",
            )
            return None

        if service == "TIDAL":
            return self._sign_in_tidal(on_done, on_error, on_status)
        if service == "Spotify":
            return self._sign_in_spotify(on_done, on_error, on_status)
        self.dispatch(on_error, f"Unknown service: {service}")
        return None

    def _sign_in_tidal(
        self,
        on_done: Callable[[], None],
        on_error: Callable[[str], None],
        on_status: Optional[Callable[[str], None]],
    ) -> Optional[threading.Thread]:
        """Drive the TIDAL device flow on a worker thread.

        The module's own call blocks for as long as the user takes in the
        browser -- there is no timeout and no way to cancel it -- so this must
        never touch the main thread. ``run_in_background`` uses daemon threads,
        so an abandoned sign-in cannot keep the app from closing.
        """
        if self.tidal_sign_in_callable is None:
            self.dispatch(
                on_error,
                "TIDAL module not available. Install it under Settings > TIDAL.",
            )
            return None

        self._in_flight["TIDAL"] = True

        def work():
            try:
                if on_status:
                    self.dispatch(on_status, TIDAL_SIGN_IN_HINT)
                self.tidal_sign_in_callable()
            except Exception as exc:
                self._finish("TIDAL", on_error, _describe(exc))
                return
            # The module reports no result -- ask the status provider whether
            # the session actually took.
            status = self.status_for("TIDAL")
            if not status.is_signed_in:
                self._finish(
                    "TIDAL",
                    on_error,
                    status.detail or "TIDAL sign-in did not complete.",
                )
                return
            self._finish("TIDAL", None, None)
            self.dispatch(on_done)

        return run_in_background(work, name="hires-tidal-signin")

    def _sign_in_spotify(
        self,
        on_done: Callable[[], None],
        on_error: Callable[[str], None],
        on_status: Optional[Callable[[str], None]],
    ) -> Optional[threading.Thread]:
        """Reuse the PKCE flow the Spotify tab already owns.

        The "is a client id configured?" check reads settings and can touch the
        network, so it runs inside the worker rather than in the click handler.
        Doing it here froze the window before the browser even opened.
        """
        controller = self.spotify_controller
        if controller is None:
            self.dispatch(on_error, "Spotify is not configured.")
            return None

        self._in_flight["Spotify"] = True

        def done():
            self._finish("Spotify", None, None)
            on_done()

        def failed(message: str):
            self._finish("Spotify", on_error, message)

        def work():
            try:
                status = self.status_for("Spotify")
            except Exception as exc:
                failed(_describe(exc))
                return
            if status.state is AccountState.NEEDS_SETUP:
                # Sending the user to a consent page that can only be rejected
                # is worse than saying what is missing.
                failed(status.hint or status.detail)
                return
            # sign_in() spawns its own worker; this one has done its job.
            controller.sign_in(done, failed, on_status)

        return run_in_background(work, name="hires-spotify-signin-precheck")

    def _finish(
        self,
        service: str,
        on_error: Optional[Callable[[str], None]],
        message: Optional[str],
    ) -> None:
        self._in_flight.pop(service, None)
        if on_error is not None and message:
            self.dispatch(on_error, message)

    # -- sign out -----------------------------------------------------------
    def sign_out(self, service: str) -> bool:
        """Drop a stored login. Only Spotify's tokens are ours to discard.

        TIDAL's sessions live in OrpheusDL's own settings store, so signing out
        there belongs in the stock GUI, not here.
        """
        if service != "Spotify":
            return False
        controller = self.spotify_controller
        source = controller.source_provider() if controller is not None else None
        backend = getattr(source, "web", None) if source is not None else None
        signer = getattr(backend, "sign_out", None)
        if not callable(signer):
            return False
        try:
            signer()
        except Exception:
            return False
        return True
