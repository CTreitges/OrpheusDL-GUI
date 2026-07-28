"""Spotify -> TIDAL playlist conversion.

Ties the pieces together: read a playlist from Spotify, match every track
against TIDAL, then turn the accepted matches into queue items that download at
hi-res quality.

The conversion is deliberately split into two steps so the GUI can put a review
in between:

    report = converter.convert(playlist)      # match everything, decide nothing
    ...user confirms/rejects the uncertain ones...
    items  = converter.enqueue(report)        # only accepted matches are queued

Nothing here talks to tkinter, so it can be driven from a worker thread or a
test.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from .models import (
    ConversionReport,
    MatchDecision,
    MatchResult,
    MediaKind,
    PlaylistRef,
    QueueItem,
    SourceUnavailableError,
    TrackRef,
)
from .quality import HIRES, normalize_tier

ProgressCallback = Callable[[int, int, Optional[MatchResult]], None]

#: Characters no mainstream filesystem accepts in a path segment.
_ILLEGAL_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows refuses these as file/folder names regardless of extension.
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_folder_name(name: str, fallback: str = "Playlist", max_length: int = 120) -> str:
    """Make ``name`` safe to use as a single path segment on Windows/macOS/Linux."""
    # Collapse whitespace first: tabs and newlines are control characters, but
    # they are whitespace rather than something worth turning into "_".
    cleaned = re.sub(r"\s+", " ", (name or "")).strip()
    cleaned = _ILLEGAL_PATH_CHARS.sub("_", cleaned).strip(" .")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    if not cleaned or cleaned.lower() in _RESERVED_NAMES:
        return fallback
    return cleaned


class PlaylistConverter:
    """Converts one Spotify playlist into hi-res TIDAL downloads."""

    def __init__(
        self,
        spotify,
        matcher,
        queue,
        *,
        quality: str = HIRES,
        output_root: Optional[str] = None,
        folder_per_playlist: bool = True,
    ):
        """
        Args:
            spotify: object exposing ``get_playlist`` / ``get_playlist_tracks``
                (i.e. :class:`hires.spotify_source.SpotifySource`).
            matcher: object exposing ``match_many`` (:class:`hires.matcher.TrackMatcher`).
            queue: object exposing ``add_many`` (:class:`hires.queue_store.QueueStore`).
            quality: tier every queued item is pinned to. Defaults to hi-res.
            output_root: base download folder. When set together with
                ``folder_per_playlist``, each playlist gets its own subfolder.
        """
        self.spotify = spotify
        self.matcher = matcher
        self.queue = queue
        self.quality = normalize_tier(quality)
        self.output_root = output_root
        self.folder_per_playlist = folder_per_playlist

    # -- step 1: match ------------------------------------------------------
    def convert(
        self,
        playlist: Any,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> ConversionReport:
        """Read the playlist from Spotify and match every track against TIDAL.

        Makes no decision the user cannot undo: uncertain matches come back as
        ``NEEDS_REVIEW`` and are not queued until :meth:`enqueue` is called.
        """
        ref = self._resolve_playlist(playlist)
        tracks = self.spotify.get_playlist_tracks(playlist)
        if not tracks:
            raise SourceUnavailableError(f"Playlist '{ref.name or ref.id}' has no readable tracks")

        results = self.matcher.match_many(tracks, progress_callback=progress_callback)
        report = ConversionReport(playlist=ref, results=list(results))
        report.finished_at = time.time()
        return report

    def _resolve_playlist(self, playlist: Any) -> PlaylistRef:
        if isinstance(playlist, PlaylistRef):
            return playlist
        ref = self.spotify.get_playlist(playlist)
        if not isinstance(ref, PlaylistRef):
            raise SourceUnavailableError(f"Could not resolve playlist: {playlist!r}")
        return ref

    # -- step 2: queue ------------------------------------------------------
    def enqueue(
        self,
        report: ConversionReport,
        *,
        include_review: bool = False,
        group_id: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> List[QueueItem]:
        """Turn accepted matches into queue items.

        Args:
            include_review: also queue matches still sitting at
                ``NEEDS_REVIEW``. Off by default -- the whole point of the
                review step is that unconfirmed matches do not download.
            group_id: reuse an existing batch id (e.g. when queueing the
                reviewed remainder of a playlist that was partly queued before).
        """
        accepted = list(report.auto_accepted)
        if include_review:
            accepted += list(report.needs_review)
        accepted = [r for r in accepted if r.best is not None]
        if not accepted:
            return []

        group_id = group_id or f"spotify-{report.playlist.id}-{int(report.started_at)}"
        label = report.playlist.name or report.playlist.id
        target = output_path or self.target_folder(report.playlist)

        items = [
            self._item_for(result, group_id=group_id, group_label=label, output_path=target)
            for result in accepted
        ]
        return self.queue.add_many(items)

    def _item_for(
        self,
        result: MatchResult,
        *,
        group_id: str,
        group_label: str,
        output_path: Optional[str],
    ) -> QueueItem:
        candidate = result.best
        assert candidate is not None  # guarded by caller
        target: TrackRef = candidate.track
        return QueueItem(
            url=target.url,
            title=target.title or result.source.title,
            subtitle=target.artist_string or result.source.artist_string,
            platform="TIDAL",
            media_kind=MediaKind.TRACK.value,
            output_path=output_path,
            quality=self.quality,
            group_id=group_id,
            group_label=group_label,
            source={
                "kind": "spotify_convert",
                "spotify_track_id": result.source.id,
                "spotify_title": result.source.display_name,
                "tidal_track_id": target.id,
                "match_method": candidate.method,
                "match_score": round(float(candidate.score), 4),
                "match_reasons": list(candidate.reasons),
            },
        )

    def target_folder(self, playlist: PlaylistRef) -> Optional[str]:
        """Where a converted playlist should land on disk."""
        if not self.output_root:
            return None
        if not self.folder_per_playlist:
            return self.output_root
        folder = sanitize_folder_name(playlist.name or playlist.id, fallback="Spotify Playlist")
        return os.path.join(self.output_root, folder)

    # -- convenience --------------------------------------------------------
    def convert_and_enqueue(
        self,
        playlist: Any,
        *,
        include_review: bool = False,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> tuple:
        """One-shot conversion. Returns ``(report, queued_items)``."""
        report = self.convert(playlist, progress_callback=progress_callback)
        return report, self.enqueue(report, include_review=include_review)


# ---------------------------------------------------------------------------
# Review helpers
# ---------------------------------------------------------------------------

def apply_review(
    report: ConversionReport,
    decisions: Dict[str, Any],
) -> ConversionReport:
    """Apply the user's review choices to a report, in place.

    ``decisions`` maps a source track id to either:
      * ``True``  -- accept the current best match
      * ``False`` -- reject this track (it will not be downloaded)
      * a TIDAL track id (str) -- accept that alternative instead
    """
    by_id = {r.source.id: r for r in report.results}
    for source_id, choice in (decisions or {}).items():
        result = by_id.get(source_id)
        if result is None:
            continue
        if choice is False:
            result.reject("rejected during review")
        elif choice is True:
            result.accept()
        elif isinstance(choice, str):
            picked = next(
                (c for c in [result.best, *result.alternatives] if c and c.track.id == choice),
                None,
            )
            if picked is not None:
                picked.method = "manual"
                result.accept(picked)
    return report


def report_summary(report: ConversionReport) -> str:
    """One-line human summary, used in the GUI log."""
    c = report.counts
    return (
        f"{report.playlist.name or report.playlist.id}: {c.get('total', 0)} tracks - "
        f"{c.get(MatchDecision.AUTO_ACCEPT.value, 0)} matched, "
        f"{c.get(MatchDecision.NEEDS_REVIEW.value, 0)} need review, "
        f"{c.get(MatchDecision.NO_MATCH.value, 0)} not found"
    )


def unmatched_lines(report: ConversionReport) -> List[str]:
    """Human-readable list of everything that will not be downloaded."""
    lines = []
    for result in report.results:
        if result.decision in (MatchDecision.NO_MATCH.value, MatchDecision.REJECTED.value):
            note = f"  ({result.note})" if result.note else ""
            lines.append(f"{result.source.display_name}{note}")
    return lines


def save_report(report: ConversionReport, path: str) -> str:
    """Write the full conversion report to disk (atomically).

    Worth keeping around: it is the only record of which tracks a playlist lost
    on the way to TIDAL.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    payload = report.to_dict()
    payload["unmatched"] = unmatched_lines(report)

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".report-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def queue_items_for_tracks(
    tracks: Sequence[TrackRef],
    *,
    quality: str = HIRES,
    output_path: Optional[str] = None,
    group_id: Optional[str] = None,
    group_label: str = "",
    platform: str = "TIDAL",
) -> List[QueueItem]:
    """Build queue items straight from TIDAL tracks.

    Used by the TIDAL playlist browser, which has no matching step -- the
    tracks are already on the target service.
    """
    tier = normalize_tier(quality)
    return [
        QueueItem(
            url=track.url,
            title=track.title,
            subtitle=track.artist_string,
            platform=platform,
            media_kind=MediaKind.TRACK.value,
            output_path=output_path,
            quality=tier,
            group_id=group_id,
            group_label=group_label,
            source={"kind": "tidal_playlist", "tidal_track_id": track.id},
        )
        for track in tracks
        if track.url
    ]
