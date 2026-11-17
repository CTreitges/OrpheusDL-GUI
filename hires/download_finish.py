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
from typing import Any, Callable, List, Optional, Tuple

from . import audio_convert as ac

PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")

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


def process_download(
    output_path: str,
    started_at: float,
    target: str,
    *,
    keep_original: bool = False,
    ffmpeg: str = "ffmpeg",
    cancel: Optional[threading.Event] = None,
    log: Callable[[str], None] = print,
) -> str:
    """Probe and convert the files one download produced; returns the quality label."""
    since = started_at - START_SLACK_SEC
    new_files = ac.find_audio(output_path, since=since)
    if not new_files:
        return ""
    quality = ac.summarize(ac.describe(ac.probe(path)) for path in new_files)

    if target == ac.TARGET_OFF:
        return quality

    summary = ac.convert_paths(new_files, target, keep_original=keep_original,
                               ffmpeg=ffmpeg, cancel=cancel)
    renamed = {r.source: r.output for r in summary.results
               if r.status == "converted" and r.removed_original and r.output}
    if renamed:
        ac.rewrite_playlists(_new_playlists(output_path, since), renamed)
    _log_summary(summary, target, log)
    if summary.converted:
        quality = f"{quality} → {target.upper()}"
    return quality


def after_download(gui: Any, output_path: Optional[str], started_at: float) -> None:
    """Entry point for gui.py. Never raises: a failed extra must not fail a download."""
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
                                   cancel=getattr(gui, "stop_event", None))
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
        if result.status == "failed":
            log(f"|GRAY|[Target format] {os.path.basename(result.source)}: {result.reason}|RESET|")


def _new_playlists(root: str, since: float) -> List[str]:
    found = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(PLAYLIST_EXTENSIONS):
                path = os.path.join(folder, name)
                if ac.created_at(path) >= since or os.path.getmtime(path) >= since:
                    found.append(path)
    return found


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
