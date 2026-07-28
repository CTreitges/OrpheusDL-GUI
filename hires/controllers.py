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
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from .converter import PlaylistConverter, apply_review, queue_items_for_tracks
from .models import (
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
    ):
        self.source_provider = source_provider
        self.matcher_provider = matcher_provider
        self.queue = queue
        self.dispatch = dispatch or UiDispatcher()
        self.quality_provider = quality_provider or (lambda: HIRES)
        self.output_provider = output_provider or (lambda: None)

        self.playlists: List[PlaylistRef] = []
        self.report: Optional[ConversionReport] = None
        self._cancel = threading.Event()

    # -- loading ------------------------------------------------------------
    def load_playlists(
        self,
        on_done: Callable[[List[PlaylistRef]], None],
        on_error: Callable[[str], None],
    ) -> threading.Thread:
        def work():
            try:
                source = self.source_provider()
                if source is None:
                    raise HiresError("Spotify is not configured.")
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
