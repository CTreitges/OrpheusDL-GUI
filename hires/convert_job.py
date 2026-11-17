"""The "Convert folder…" job: one worker thread, progress, cancel.

Tkinter-free; :mod:`hires.convert_panel` only draws what this reports. All
callbacks go through ``dispatch`` (a :class:`hires.controllers.UiDispatcher`
in the app), because they fire on the worker thread.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from . import audio_convert as ac

#: Set while a "Convert folder…" job runs. The queue runner starts no new item
#: meanwhile, so a download never writes into a folder that is being converted.
_ACTIVE = threading.Event()


def folder_job_running() -> bool:
    return _ACTIVE.is_set()


@dataclass
class Progress:
    done: int
    total: int
    converted: int
    skipped: int
    failed: int
    current: str = ""
    #: Seconds since the job started; drives the time-left estimate.
    elapsed: float = 0.0

    @property
    def seconds_left(self) -> Optional[float]:
        """Estimate from the pace so far; ``None`` until two files are done."""
        if self.done < 2 or self.done >= self.total or self.elapsed <= 0:
            return None
        return self.elapsed / self.done * (self.total - self.done)

    @property
    def text(self) -> str:
        parts = [f"{self.done} of {self.total}", f"{self.converted} converted"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        left = self.seconds_left
        if left is not None:
            parts.append(f"about {round(left / 60)} min left" if left >= 90 else f"about {max(1, round(left))} s left")
        return " · ".join(parts)


def summary_text(summary: ac.FolderSummary, target: str) -> str:
    """The closing message: counts, then why something did not convert."""
    lines = [f"{summary.converted} of {summary.found} files converted to {target.upper()}."]
    if summary.skipped:
        lines.append(f"{summary.skipped} skipped (already {target.upper()}, target file exists, or spatial audio).")
    if summary.failed:
        lines.append(f"{summary.failed} failed — their originals are untouched.")
    if summary.cancelled:
        lines.append("Stopped before the end; the rest is unchanged.")
    lossy = sum(1 for r in summary.results if r.status == "converted" and r.lossy_source)
    if lossy:
        lines.append(f"{lossy} came from a lossy source and do not sound better than before.")
    failures = [r for r in summary.results if r.status == "failed"][:5]
    for result in failures:
        lines.append(f"• {os.path.basename(result.source)}: {result.reason}")
    return "\n".join(lines)


class FolderConvertJob:
    """Runs :func:`hires.audio_convert.convert_paths` off the main thread."""

    def __init__(
        self,
        dispatch: Callable[..., Any],
        *,
        ffmpeg: str = "ffmpeg",
        convert: Callable[..., ac.FolderSummary] = ac.convert_paths,
    ):
        self.dispatch = dispatch
        self.ffmpeg = ffmpeg
        self._convert = convert
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def scan(self, folder: str, on_done: Callable[[List[str], List[str]], None]) -> bool:
        """Count the folder's audio files and playlists off the main thread.

        A whole library can take seconds to walk; ``on_done(audio, playlists)``
        arrives through ``dispatch``. False if a scan or job is running.
        """
        if self.running:
            return False

        def work() -> None:
            try:
                audio, playlists = ac.scan(folder)
            except Exception:
                audio, playlists = [], []
            self.dispatch(on_done, audio, playlists)

        self._thread = threading.Thread(target=work, name="hires-convert-scan", daemon=True)
        self._thread.start()
        return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        paths: List[str],
        target: str,
        keep_original: bool,
        on_progress: Callable[[Progress], None],
        on_done: Callable[[ac.FolderSummary], None],
        playlists: Sequence[str] = (),
    ) -> bool:
        """Start converting ``paths``; False if a job is already running.

        ``playlists`` (.m3u/.m3u8 of the folder) are pointed at the new files
        afterwards, as the download hook does for a fresh download.
        """
        if self.running:
            return False
        self._cancel.clear()
        counts = {"converted": 0, "skipped": 0, "failed": 0}
        started = time.monotonic()

        def progress(done: int, total: int, result: ac.ConvertResult) -> None:
            if result.status in counts:
                counts[result.status] += 1
            self.dispatch(on_progress, Progress(done, total, current=os.path.basename(result.source),
                                                elapsed=time.monotonic() - started, **counts))

        def work() -> None:
            _ACTIVE.set()
            try:
                summary = self._convert(paths, target, keep_original=keep_original,
                                        ffmpeg=self.ffmpeg, cancel=self._cancel, progress=progress)
                ac.rewrite_playlists(playlists, ac.renamed_by(summary))
            except Exception as exc:  # report, never leave the dialog hanging
                summary = ac.FolderSummary(found=len(paths), failed=len(paths))
                summary.results.append(ac.ConvertResult("", "failed", reason=str(exc)))
            finally:
                _ACTIVE.clear()
            self.dispatch(on_done, summary)

        self._thread = threading.Thread(target=work, name="hires-convert-folder", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
