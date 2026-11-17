"""The "Convert folder…" job: one worker thread, progress, cancel.

Tkinter-free; :mod:`hires.convert_panel` only draws what this reports. All
callbacks go through ``dispatch`` (a :class:`hires.controllers.UiDispatcher`
in the app), because they fire on the worker thread.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from . import audio_convert as ac


@dataclass
class Progress:
    done: int
    total: int
    converted: int
    skipped: int
    failed: int
    current: str = ""

    @property
    def text(self) -> str:
        parts = [f"{self.done} of {self.total}", f"{self.converted} converted"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
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

    @staticmethod
    def scan(folder: str) -> List[str]:
        return ac.find_audio(folder)

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
    ) -> bool:
        """Start converting ``paths``; False if a job is already running."""
        if self.running:
            return False
        self._cancel.clear()
        counts = {"converted": 0, "skipped": 0, "failed": 0}

        def progress(done: int, total: int, result: ac.ConvertResult) -> None:
            if result.status in counts:
                counts[result.status] += 1
            self.dispatch(on_progress, Progress(done, total, current=os.path.basename(result.source),
                                                **counts))

        def work() -> None:
            try:
                summary = self._convert(paths, target, keep_original=keep_original,
                                        ffmpeg=self.ffmpeg, cancel=self._cancel, progress=progress)
            except Exception as exc:  # report, never leave the dialog hanging
                summary = ac.FolderSummary(found=len(paths), failed=len(paths))
                summary.results.append(ac.ConvertResult("", "failed", reason=str(exc)))
            self.dispatch(on_done, summary)

        self._thread = threading.Thread(target=work, name="hires-convert-folder", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
