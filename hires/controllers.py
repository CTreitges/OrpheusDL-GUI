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

from .auto_refresh import AutoRefresh
from .converter import PlaylistConverter, apply_review, queue_items_for_tracks
from .models import (
    AccountState,
    AccountStatus,
    AuthRequiredError,
    ConversionReport,
    FolderRef,
    HiresError,
    MatchDecision,
    MatchResult,
    PlaylistRef,
    QueueItem,
    QueueStatus,
    RateLimitedError,
    RunCancelled,
    TrackRef,
    flat_root,
)
from .quality import HIRES, label_for, normalize_tier
from .queue_groups import QueueGroup, build_groups, group_key

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
    #: The detail without the playlist name -- what a line inside its own
    #: playlist group shows.
    subtitle: str = ""

    @property
    def text(self) -> str:
        parts = [f"{self.symbol} {self.title}"]
        if self.detail:
            parts.append(self.detail)
        return "  —  ".join(parts)


#: Which rows earn a place on screen when the queue is too long to draw whole.
#: Finished items are kept last on purpose: the queue keeps them in the order
#: they were added, so after a couple of hundred downloads they sit at the very
#: front -- and a list capped from the top would show nothing but ticks while
#: the download actually running scrolled off the end.
_DISPLAY_PRIORITY = {
    QueueStatus.ACTIVE.value: 0,
    QueueStatus.PENDING.value: 1,
    QueueStatus.FAILED.value: 2,
    QueueStatus.CANCELLED.value: 3,
    QueueStatus.DONE.value: 4,
}


class QueueController:
    """Drives the queue tab."""

    def __init__(self, queue, stop_active: Optional[Callable[[], Any]] = None):
        """
        Args:
            stop_active: stops the download the queue runner is running right
                now. "Clear all" calls it so the dropped item does not keep
                downloading. ``None`` when there is no runner (tests, no GUI).
        """
        self.queue = queue
        self.stop_active = stop_active

    # -- display ------------------------------------------------------------
    def rows(self, status: Optional[str] = None) -> List[QueueRow]:
        return [self._row(item) for item in self.queue.list(status=status)]

    def display_rows(self, limit: int) -> tuple:
        """``(rows to draw, how many were left out)``.

        Below ``limit`` this is simply the queue in its own order -- which is
        what the reorder buttons act on, so it must not be rearranged for no
        reason. Only once the list has to be cut short do we choose *what* to
        cut: running and waiting items stay, finished ones go. The sort is
        stable, so queue order survives within each group.
        """
        rows = self.rows()
        if limit <= 0 or len(rows) <= limit:
            return rows, 0
        ordered = sorted(rows, key=lambda r: _DISPLAY_PRIORITY.get(r.status, 9))
        return ordered[:limit], len(rows) - limit

    @staticmethod
    def _row(item: QueueItem) -> QueueRow:
        detail_parts = [p for p in (item.subtitle, item.group_label) if p]
        return QueueRow(
            id=item.id,
            symbol=STATUS_SYMBOLS.get(item.status, "?"),
            status=item.status,
            title=item.title or item.url,
            detail=" · ".join(detail_parts),
            # A finished entry shows what really arrived, not what was asked for.
            quality=(item.actual_quality if item.status == QueueStatus.DONE.value
                     and item.actual_quality else
                     label_for(item.quality) if item.quality else ""),
            error=item.error or "",
            group_label=item.group_label,
            subtitle=item.subtitle,
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
        """Empty the queue, stopping the running item first.

        Stop before clearing: the other way round the runner would see its
        item vanish while the download carries on into a cleared queue.
        """
        if self.queue.stats().active and self.stop_active is not None:
            try:
                self.stop_active()
            except Exception:
                # Clearing must still happen; the runner copes with an item
                # that disappeared under it.
                traceback.print_exc()
        return self.queue.clear_all()

    def clear_all_question(self) -> Optional[str]:
        """The confirmation text for "Clear all", or ``None`` when empty."""
        stats = self.queue.stats()
        if stats.total == 0:
            return None
        noun = "entry" if stats.total == 1 else "entries"
        text = f"Remove all {stats.total} {noun} from the queue?"
        if stats.active:
            text += "\n\nThe track that is downloading right now will be stopped and removed."
        text += "\n\nThis cannot be undone."
        return text

    # -- groups -------------------------------------------------------------
    def groups(self) -> List[QueueGroup]:
        return build_groups(self.queue.list())

    def move_group_to_front(self, key: str) -> int:
        return self.queue.move_group_to_front(key)

    def next_group_key(self) -> Optional[str]:
        """The group whose track is downloaded next, ``None`` when none waits."""
        waiting = self.queue.list(status=QueueStatus.PENDING.value)
        return group_key(waiting[0]) if waiting else None

    def remove_group(self, key: str) -> int:
        return self.queue.remove_group(key)

    def grouped_rows(self, limit: int, collapsed: Optional[set] = None) -> tuple:
        """``(groups, rows per group key, rows left out)`` for drawing.

        Every group gets a header. Item rows are handed out group by group in
        display order until ``limit`` rows are used, so a queue of thousands
        never puts more than ``limit`` rows on screen. Within a group that
        does not fit, running and waiting items win over finished ones -- the
        same rule as :meth:`display_rows`, for the same reason. Collapsed
        groups get no rows and cost nothing.
        """
        collapsed = collapsed or set()
        items = {item.id: item for item in self.queue.list()}
        groups = build_groups(items.values())
        budget = max(0, int(limit))
        rows: Dict[str, List[QueueRow]] = {}
        hidden = 0
        for group in groups:
            if group.key in collapsed:
                rows[group.key] = []
                continue
            if budget == 0:
                # Nothing more fits; do not build rows only to throw them away.
                hidden += group.total
                rows[group.key] = []
                continue
            group_rows = [self._row(items[item_id]) for item_id in group.item_ids]
            if len(group_rows) > budget:
                group_rows.sort(key=lambda r: _DISPLAY_PRIORITY.get(r.status, 9))
                hidden += len(group_rows) - budget
                group_rows = group_rows[:budget]
            budget -= len(group_rows)
            rows[group.key] = group_rows
        return groups, rows, hidden

    def subscribe(self, callback: Callable[[], None]):
        return self.queue.subscribe(callback)


# ---------------------------------------------------------------------------
# TIDAL playlist browser tab
# ---------------------------------------------------------------------------

class TidalBrowserController:
    """Lists a service's playlists and queues them for download.

    Written for TIDAL, used by the Playlists tab for every service: ``service``
    names the platform on queue items and in messages. With the default
    ``"TIDAL"`` everything behaves exactly as before.
    """

    def __init__(
        self,
        library_provider: Callable[[], Any],
        queue,
        *,
        dispatch: Optional[UiDispatcher] = None,
        quality_provider: Optional[Callable[[], Optional[str]]] = None,
        output_provider: Optional[Callable[[], Optional[str]]] = None,
        folder_per_playlist: bool = True,
        service: str = "TIDAL",
    ):
        """
        Args:
            library_provider: returns a ``TidalLibrary`` (or raises). Called
                lazily so the tab works even if TIDAL is set up after startup.
            quality_provider: returns the tier to download at. Defaults to hi-res;
                ``None`` leaves the tier to the GUI (services other than TIDAL).
            output_provider: returns the base download folder.
        """
        self.service = service
        self.library_provider = library_provider
        self.queue = queue
        self.dispatch = dispatch or UiDispatcher()
        self.quality_provider = quality_provider or (lambda: HIRES)
        self.output_provider = output_provider or (lambda: None)
        self.folder_per_playlist = folder_per_playlist
        self.playlists: List[PlaylistRef] = []
        self.root: Optional[FolderRef] = None

    # -- loading ------------------------------------------------------------
    def load_playlists(
        self,
        on_done: Callable[[FolderRef], None],
        on_error: Callable[[str], None],
        *,
        include_favorites: bool = True,
    ) -> threading.Thread:
        """Load the library, arranged in folders where TIDAL exposes them.

        ``on_done`` always receives a :class:`FolderRef`: an unnamed root when
        there are no folders, or the folder view when there are. That keeps the
        widget code from having to know which happened.
        """

        def work():
            try:
                library = self.library_provider()
                if library is None:
                    raise HiresError(
                        f"{self.service} module not available. "
                        f"Install it and log in under Settings > {self.service}."
                    )
                lister = getattr(library, "list_folders", None)
                if callable(lister):
                    root = lister(include_favorites=include_favorites)
                else:
                    # A library that predates folders is still perfectly usable.
                    root = flat_root(
                        library.list_playlists(include_favorites=include_favorites),
                        platform=self.service,
                    )
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            self.root = root
            self.playlists = root.all_playlists()
            self.dispatch(on_done, root)

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
                    raise HiresError(f"{self.service} module not available.")
                # TIDAL's library wants the id; the other providers read a
                # playlist through its link (SoundCloud answers playlists/<id>
                # with 401 without a login).
                tracks = library.get_playlist_tracks(
                    playlist.id if self.service == "TIDAL" else playlist
                )
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            self.dispatch(on_done, list(tracks))

        return run_in_background(work, name="hires-playlist-tracks")

    # -- queueing -----------------------------------------------------------
    def enqueue_many(
        self, playlists: Sequence[PlaylistRef], group_label: str = ""
    ) -> List[QueueItem]:
        """Queue several playlists at once -- a whole folder, or a selection.

        Each playlist stays its own queue item, so one that fails can be
        retried without touching the others. ``group_label`` ties them together
        in the queue display; it is the folder name when a folder was picked.
        """
        items = []
        for playlist in playlists:
            try:
                items.append(self.enqueue_playlist_as_one(playlist, group_label=group_label))
            except Exception:
                # One bad playlist must not cost the user the rest of the folder.
                continue
        return items

    def enqueue_folder(self, folder: FolderRef) -> List[QueueItem]:
        """Queue every playlist in a folder, including its sub-folders."""
        return self.enqueue_many(folder.all_playlists(), group_label=folder.name)

    def enqueue_playlist_as_one(
        self, playlist: PlaylistRef, group_label: str = ""
    ) -> QueueItem:
        """Queue the playlist URL itself and let OrpheusDL expand it.

        Cheaper than resolving every track, and it keeps OrpheusDL's own
        playlist handling (m3u writing, folder naming) intact.
        """
        from .tidal_library import playlist_url

        url = playlist.url
        if not url and self.service == "TIDAL":
            url = playlist_url(playlist.id)
        if not url:
            raise HiresError(f"{playlist.name or playlist.id} has no link to download from.")
        svc = self.service.lower()
        item = QueueItem(
            url=url,
            title=playlist.name or playlist.id,
            subtitle=playlist.owner,
            platform=self.service,
            media_kind="playlist",
            output_path=self.output_provider(),
            quality=self._tier(),
            # The folder name when one was queued, so the queue shows what the
            # item belonged to rather than repeating its own title.
            group_label=group_label or playlist.name or playlist.id,
            source={"kind": f"{svc}_playlist", f"{svc}_playlist_id": playlist.id},
        )
        return self.queue.add(item)

    def _tier(self) -> Optional[str]:
        tier = self.quality_provider()
        return normalize_tier(tier) if tier else None

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
                base,
                sanitize_folder_name(playlist.name or playlist.id, fallback=f"{self.service} Playlist"),
            )

        items = queue_items_for_tracks(
            tracks,
            quality=self._tier(),
            output_path=target,
            group_id=f"{self.service.lower()}-{playlist.id}-{int(time.time())}",
            group_label=playlist.name or playlist.id,
            platform=self.service,
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
        #: Results of the last multi-playlist run.
        self.reports: List[ConversionReport] = []
        self._cancel = threading.Event()
        #: Bumped by every new run. A worker whose generation is no longer the
        #: current one has been superseded and must not touch shared state:
        #: clicking playlist B while A was still matching used to let the slower
        #: run overwrite ``self.report``, so "Queue matches" queued A's tracks
        #: under B's name.
        self._generation = 0
        self._run_lock = threading.Lock()

    def _begin_run(self) -> tuple:
        """Supersede whatever is running. Returns ``(generation, cancel)``.

        The new run gets its own event rather than clearing the shared one --
        clearing it told the *previous* worker to carry on, which is exactly how
        two conversions ended up racing.
        """
        with self._run_lock:
            self._cancel.set()
            self._cancel = threading.Event()
            self._generation += 1
            return self._generation, self._cancel

    def _is_current(self, generation: int) -> bool:
        """False once a newer run has taken over."""
        return generation == self._generation

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
        is_current: Optional[Callable[[], bool]] = None,
    ) -> threading.Thread:
        """List the user's playlists, signing in first if that has not happened.

        ``is_current`` lets a caller that can switch away (the Port tab) keep a
        late answer from replacing the list it shows now.
        """

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
            playlists = list(playlists)
            if is_current is None or is_current():
                self.playlists = playlists
            # Same contract as the TIDAL tab, so one widget renders both.
            # Spotify's Web API does not expose folders at all, so this root is
            # always unnamed -- see HIRES.md.
            self.dispatch(on_done, flat_root(playlists, platform="Spotify"))

        return run_in_background(work, name="hires-spotify-playlists")

    # -- conversion ---------------------------------------------------------
    def cancel(self) -> None:
        """Stop the running conversion.

        Deliberately leaves the generation alone: the user asked for this, so
        the run stays "current" long enough to report back that it stopped.
        Being *superseded* by a new selection is the silent case -- see
        :meth:`_begin_run`.
        """
        with self._run_lock:
            self._cancel.set()

    def convert_many(
        self,
        playlists: Sequence[Any],
        on_playlist: Callable[[int, int, Any], None],
        on_progress: Callable[[int, int, Optional[MatchResult]], None],
        on_done: Callable[[List[ConversionReport]], None],
        on_error: Callable[[str], None],
        *,
        enqueue: bool = True,
    ) -> threading.Thread:
        """Convert several playlists back to back, queueing as it goes.

        Queueing per playlist rather than at the end means a long run is useful
        even if a later playlist fails -- the downloads for the earlier ones
        have already started.

        ``on_playlist(done, total, playlist)`` fires as each one begins.
        """
        generation, cancel = self._begin_run()
        wanted = list(playlists)

        def work():
            reports: List[ConversionReport] = []
            failures: List[str] = []
            try:
                converter = self._build_converter()
            except Exception as exc:
                if self._is_current(generation):
                    self.dispatch(on_error, _describe(exc))
                return

            for index, playlist in enumerate(wanted):
                if cancel.is_set() or not self._is_current(generation):
                    break
                self.dispatch(on_playlist, index, len(wanted), playlist)

                def progress(done, total, result, _i=index):
                    if cancel.is_set() or not self._is_current(generation):
                        raise RunCancelled("Conversion cancelled")
                    self.dispatch(on_progress, done, total, result)

                try:
                    report = converter.convert(playlist, progress_callback=progress)
                    if enqueue:
                        converter.enqueue(report, include_review=False)
                except RateLimitedError as exc:
                    # The service said stop: the next playlist would only
                    # make a block likelier. Reports done so far stay queued.
                    failures.append(_describe(exc))
                    break
                except HiresError as exc:
                    if cancel.is_set():
                        break
                    failures.append(_describe(exc))
                    continue
                except Exception as exc:
                    failures.append(_describe(exc))
                    continue
                reports.append(report)

            if not self._is_current(generation):
                return  # a newer run owns the tab now
            self.reports = reports
            if not reports and failures:
                self.dispatch(on_error, failures[0])
                return
            if failures:
                self.dispatch(
                    on_error,
                    f"{len(failures)} of {len(wanted)} playlists failed: {failures[0]}",
                )
            self.dispatch(on_done, reports)

        return run_in_background(work, name="hires-spotify-convert-many")

    def convert(
        self,
        playlist: Any,
        on_progress: Callable[[int, int, Optional[MatchResult]], None],
        on_done: Callable[[ConversionReport], None],
        on_error: Callable[[str], None],
    ) -> threading.Thread:
        """Match a Spotify playlist against TIDAL. Queues nothing yet.

        Starting this supersedes any conversion already in flight.
        """
        generation, cancel = self._begin_run()

        def work():
            try:
                converter = self._build_converter()

                def progress(done, total, result):
                    if cancel.is_set() or not self._is_current(generation):
                        raise RunCancelled("Conversion cancelled")
                    self.dispatch(on_progress, done, total, result)

                report = converter.convert(playlist, progress_callback=progress)
            except Exception as exc:
                if self._is_current(generation):
                    self.dispatch(on_error, _describe(exc))
                return
            if not self._is_current(generation):
                return  # a newer selection owns the tab now
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

    #: Every service the tab knows, in the order they are shown. An instance
    #: shows the ones it got a status provider for (see ``self.SERVICES``).
    ALL_SERVICES = ("TIDAL", "Spotify", "SoundCloud", "YouTube")

    #: What to show before the first read comes back.
    UNKNOWN_DETAIL = "Checking…"

    def __init__(
        self,
        *,
        tidal_status_provider: Callable[[], AccountStatus],
        spotify_status_provider: Callable[[], AccountStatus],
        soundcloud_status_provider: Optional[Callable[[], AccountStatus]] = None,
        youtube_status_provider: Optional[Callable[[], AccountStatus]] = None,
        tidal_sign_in: Optional[Callable[[], Any]] = None,
        spotify_controller: Optional["SpotifyImportController"] = None,
        dispatch: Optional[UiDispatcher] = None,
        busy_provider: Optional[Callable[[], bool]] = None,
        auto_wait: Optional[Callable[[threading.Event, float], bool]] = None,
        auto_refresh_delays: Optional[Sequence[float]] = None,
    ):
        """
        Args:
            tidal_sign_in: blocking callable that drives TIDAL's device flow.
                ``None`` when the TIDAL module is not installed.
            spotify_controller: reused so both tabs share one Spotify source and
                one set of tokens.
            busy_provider: True while a download is running. Signing in mid-
                download would swap the session out from under it.
            auto_wait: ``wait(cancel_event, seconds) -> cancelled``. The clock of
                the automatic re-checks; tests pass one that does not sleep.
        """
        self.tidal_status_provider = tidal_status_provider
        self.spotify_status_provider = spotify_status_provider
        self._providers: Dict[str, Callable[[], AccountStatus]] = {
            "TIDAL": tidal_status_provider,
            "Spotify": spotify_status_provider,
        }
        if soundcloud_status_provider is not None:
            self._providers["SoundCloud"] = soundcloud_status_provider
        if youtube_status_provider is not None:
            self._providers["YouTube"] = youtube_status_provider
        self.SERVICES = tuple(s for s in self.ALL_SERVICES if s in self._providers)

        self.tidal_sign_in_callable = tidal_sign_in
        self.spotify_controller = spotify_controller
        self.dispatch = dispatch or UiDispatcher()
        self.busy_provider = busy_provider or (lambda: False)

        # Guards against a second click while a browser flow is still open.
        self._in_flight: Dict[str, bool] = {}

        # Last known status per service, for painting while a read is running.
        self._last: Dict[str, AccountStatus] = {}
        # Reads in flight. A counter under a lock rather than a bool: the
        # automatic re-check and a Refresh can overlap, and the first one to
        # finish must not report "idle" while the other is still reading.
        self._reads = 0
        self._reads_lock = threading.Lock()

        self.auto_refresh = AutoRefresh(
            self._read_for_auto_refresh,
            lambda statuses: all(s.is_signed_in for s in statuses),
            self.dispatch,
            delays=auto_refresh_delays,
            wait=auto_wait,
        )

    # -- status -------------------------------------------------------------
    def statuses(self) -> List[AccountStatus]:
        """Read every service's state. **Blocking** -- call from a worker.

        Kept public because tests and the sign-in worker use it directly. UI
        code wants :meth:`refresh` instead.
        """
        return [self.status_for(service) for service in self.SERVICES]

    def status_for(self, service: str, remember: bool = True) -> AccountStatus:
        """Read one service's state. **Blocking** -- this can do HTTP.

        ``remember=False`` leaves the cached state alone: the automatic
        re-check stores its result only once it is known to be current, so
        a superseded, slow read cannot overwrite a newer sign-in result.
        """
        provider = self._providers.get(service)
        try:
            if provider is None:
                raise HiresError(f"Unknown service: {service}")
            status = provider()
        except Exception as exc:
            # A broken provider must not blank the whole tab.
            status = AccountStatus(
                service=service,
                state=AccountState.UNAVAILABLE,
                detail=_describe(exc),
            )
        if remember:
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
        with self._reads_lock:
            return self._reads > 0

    def _begin_read(self, exclusive: bool) -> bool:
        """Count a read in. ``exclusive`` refuses while another one runs."""
        with self._reads_lock:
            if exclusive and self._reads:
                return False
            self._reads += 1
            return True

    def _end_read(self) -> None:
        with self._reads_lock:
            self._reads = max(0, self._reads - 1)

    def refresh(
        self,
        on_done: Callable[[List[AccountStatus]], None],
        on_error: Optional[Callable[[str], None]] = None,
    ) -> Optional[threading.Thread]:
        """Re-read every status off the UI thread.

        Returns ``None`` when a read is already running -- the answer would be
        the same, and two concurrent reads would just race to repaint.
        """
        if not self._begin_read(exclusive=True):
            return None

        def work():
            try:
                statuses = self.statuses()
            except Exception as exc:
                self._end_read()
                if on_error is not None:
                    self.dispatch(on_error, _describe(exc))
                return
            self._end_read()
            self.dispatch(on_done, statuses)

        return run_in_background(work, name="hires-accounts-status")

    # -- automatic re-checks ------------------------------------------------
    def _read_for_auto_refresh(self, recheck: bool) -> Optional[List[AccountStatus]]:
        """All statuses; a re-check only asks the services not signed in.

        A missing module or cookie file never turns into "Signed in" on its
        own, so most starts run all five re-checks. Re-reading the signed-in
        services each time would repeat their HTTP calls (Spotify's /me,
        SoundCloud's /me) for nothing (review part 1, finding 2).
        """
        self._begin_read(exclusive=False)
        try:
            out = []
            for service in self.SERVICES:
                last = self._last.get(service)
                if recheck and last is not None and last.is_signed_in:
                    out.append(last)
                else:
                    out.append(self.status_for(service, remember=False))
            return out
        except Exception:
            return None
        finally:
            self._end_read()

    @property
    def auto_refresh_limit(self) -> int:
        return self.auto_refresh.limit

    def start_auto_refresh(self, on_update, **callbacks) -> threading.Thread:
        """Read now, then re-check what is not signed in -- see :class:`AutoRefresh`.

        Results are cached here, in the update callback, because AutoRefresh
        only delivers it while its run is still the current one.
        """

        def update(statuses: List[AccountStatus]) -> None:
            for status in statuses:
                self._last[status.service] = status
            on_update(statuses)

        return self.auto_refresh.start(update, **callbacks)

    def stop_auto_refresh(self) -> None:
        self.auto_refresh.stop()

    def is_busy(self, service: str) -> bool:
        return bool(self._in_flight.get(service))

    def summary(self) -> str:
        """One line about what is signed in. Uses the last read, never the network."""
        if self.is_reading and not self._last:
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

        The returned thread covers the whole sign-in, not just the precheck --
        the flow runs on a worker of its own, and this one waits for it. Without
        that, joining the returned thread proved nothing: it was finished the
        moment the browser opened, so the caller could look at the result before
        there was one.
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
            # sign_in() runs on a worker of its own; stay alive until it is
            # done so the thread we handed back means what it says.
            inner = controller.sign_in(done, failed, on_status)
            if inner is not None:
                inner.join()

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
