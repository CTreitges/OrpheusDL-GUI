"""What happens right after a download wrote its files.

gui.py calls :func:`after_download` once per finished download, still on the
download thread and before the GUI flips back to idle -- so a queue item only
counts as done once its conversion is done too, and the next one cannot start
writing into the folder meanwhile. Two jobs:

1. **Actual quality.** Read what really arrived (codec, bit depth, rate or
   bitrate) *before* any conversion and hand it to the queue runtime, which
   stores it on the finished queue entry.
2. **Target format.** With *Settings → Codecs → Target format* set, convert
   the new files (see :mod:`hires.audio_convert`) and fix the ``.m3u``
   playlists written by the same download.

"New" means created since the download started; see
:func:`hires.audio_convert.find_audio`.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

from . import audio_convert as ac

#: Audio paths present when a download started, per download thread.
_SNAPSHOTS: Dict[Any, FrozenSet[str]] = {}
_SNAPSHOTS_LOCK = threading.Lock()

#: A file whose creation time lies this far before the start still counts:
#: filesystems round timestamps (FAT: 2 s) and clocks drift a little.
START_SLACK_SEC = 2.0


def target_settings(settings: Any) -> Tuple[str, bool]:
    """``(target_format, keep_original)`` from the GUI settings, safe defaults."""
    codecs = {}
    if isinstance(settings, dict):
        codecs = (settings.get("globals") or {}).get("codecs") or {}
    target = str(codecs.get("target_format") or ac.TARGET_OFF).strip().lower()
    if target not in ac.TARGET_CHOICES:
        target = ac.TARGET_OFF
    return target, bool(codecs.get("target_keep_original", False))


def snapshot(root: Optional[str]) -> FrozenSet[str]:
    """Every audio path under ``root`` right now (normalised for comparison)."""
    return frozenset(_key(p) for p in ac.scan(root)[0]) if root else frozenset()


def process_download(
    output_path: str,
    started_at: float,
    target: str,
    *,
    keep_original: bool = False,
    ffmpeg: str = "ffmpeg",
    cancel: Optional[threading.Event] = None,
    log: Callable[[str], None] = print,
    existing: Optional[FrozenSet[str]] = None,
) -> str:
    """Probe and convert the files one download produced; returns the quality label.

    A file counts as produced when it was created since the start *and*, where
    a snapshot from the start is given (``existing``), its path did not exist
    then -- so neither a missing creation time (Linux) nor a re-tag can pull a
    library file into the conversion.
    """
    since = started_at - START_SLACK_SEC
    audio, playlists = ac.scan(output_path)
    new_files = [p for p in audio
                 if (existing is None or _key(p) not in existing) and ac.created_at(p) >= since]
    if not new_files:
        return ""
    quality = ac.summarize(ac.describe(ac.probe(path)) for path in new_files)

    if target == ac.TARGET_OFF:
        return quality

    summary = ac.convert_paths(new_files, target, keep_original=keep_original,
                               ffmpeg=ffmpeg, cancel=cancel)
    renamed = ac.renamed_by(summary)
    if renamed:
        ac.rewrite_playlists([p for p in playlists if _touched_since(p, since)], renamed)
    _log_summary(summary, target, log)
    if summary.converted:
        quality = f"{quality} → {target.upper()}"
    return quality


def before_download(gui: Any, output_path: Optional[str]) -> None:
    """Entry point for gui.py at the start of a download: remember what is there."""
    try:
        path = output_path or _default_output(gui)
        taken = snapshot(path)
        with _SNAPSHOTS_LOCK:
            _SNAPSHOTS[threading.current_thread()] = taken
            # A stopped or failed download never reaches after_download.
            while len(_SNAPSHOTS) > 8:
                _SNAPSHOTS.pop(next(iter(_SNAPSHOTS)))
    except Exception as exc:
        print(f"[Hi-Res] Before-download step failed: {exc}")


def after_download(gui: Any, output_path: Optional[str], started_at: float) -> None:
    """Entry point for gui.py. Never raises: a failed extra must not fail a download."""
    with _SNAPSHOTS_LOCK:
        existing = _SNAPSHOTS.pop(threading.current_thread(), None)
    try:
        path = output_path or _default_output(gui)
        if not path:
            return
        target, keep = target_settings(getattr(gui, "current_settings", None))
        ffmpeg = "ffmpeg"
        finder = getattr(gui, "_get_ffmpeg_path", None)
        if callable(finder):
            ffmpeg = finder() or "ffmpeg"
        quality = process_download(path, started_at, target, keep_original=keep, ffmpeg=ffmpeg,
                                   cancel=getattr(gui, "stop_event", None), existing=existing)
        runtime = _runtime(gui)
        if runtime is not None and quality:
            runtime.note_download_result(threading.current_thread(), quality)
    except Exception as exc:
        print(f"[Hi-Res] After-download step failed: {exc}")


def _log_summary(summary: ac.FolderSummary, target: str, log: Callable[[str], None]) -> None:
    name = target.upper()
    parts = [f"{summary.converted} converted to {name}"]
    if summary.skipped:
        parts.append(f"{summary.skipped} skipped")
    if summary.failed:
        parts.append(f"{summary.failed} failed (original kept)")
    if summary.cancelled:
        parts.append("stopped")
    log(f"|GRAY|[Target format] {', '.join(parts)}.|RESET|")
    if any(r.lossy_source for r in summary.results if r.status == "converted"):
        log(f"|GRAY|[Target format] Note: {ac.LOSSY_WARNING}.|RESET|")
    for result in summary.results:
        name = os.path.basename(result.source)
        if result.status == "failed" or result.reason.startswith("original kept"):
            log(f"|GRAY|[Target format] {name}: {result.reason}|RESET|")
        elif result.status == "skipped" and result.reason == "target file exists":
            log(f"|GRAY|[Target format] {name}: kept as downloaded, "
                f"{os.path.basename(result.output or '')} already exists (downloaded again?)|RESET|")


def _touched_since(path: str, since: float) -> bool:
    """Created or rewritten by this download; a vanished file is simply skipped."""
    try:
        return ac.created_at(path) >= since or os.path.getmtime(path) >= since
    except OSError:
        return False


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _default_output(gui: Any) -> Optional[str]:
    settings = getattr(gui, "current_settings", None)
    if not isinstance(settings, dict):
        return None
    general = (settings.get("globals") or {}).get("general") or {}
    path = general.get("output_path")
    return path if isinstance(path, str) and path.strip() else None


def _runtime(gui: Any):
    bundle = getattr(gui, "hires_runtime", None)
    if isinstance(bundle, dict):
        return bundle.get("runtime")
    return None
