"""The Port tab: read a playlist on one service, find its tracks on another.

Widgets only; the decisions live in :mod:`hires.port_controller`. Replaces the
former "Spotify -> TIDAL" tab, whose layout and behaviour it keeps: with the
default direction Spotify -> TIDAL nothing changed for the user.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import customtkinter

from .gui_panel import PlaylistList, ReviewDialog, _selection_label
from .help_widget import attach_tooltip
from .models import ConversionReport, FolderRef, MatchDecision, PlaylistRef
from .playlist_providers import SERVICES, ProviderStatus
from .port_controller import PortController
from .quality import HIRES, label_for
from .ui_style import (
    ERROR,
    GRAY_TEXT,
    PRIMARY_ACTION,
    SECONDARY_TEXT,
    SUCCESS,
    WARNING,
    WHITE_TEXT,
)
from .ui_style import button as _button


#: Least time between two redraws of the progress bar and its line.
PROGRESS_REDRAW_S = 0.1


def quality_note(target: str) -> str:
    if target == "TIDAL":
        return f"Downloads at {label_for(HIRES)}"
    return f"Downloads from {target} at the quality set in Settings"


def queued_text(count: int, target: str, playlists: int = 0) -> str:
    where = "in hi-res" if target == "TIDAL" else f"from {target}"
    if playlists:
        return f"Queued {count} tracks from {playlists} playlists {where}."
    return f"Queued {count} tracks {where}."


class PortTab:
    """Port a playlist in any of the 12 directions and queue the matches."""

    def __init__(self, parent, controller: PortController):
        self.controller = controller
        self.report: Optional[ConversionReport] = None
        self.review_choices: Dict[str, Any] = {}
        self._progress_drawn_at = 0.0

        self.frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=9, pady=10)

        # -- direction ------------------------------------------------------
        direction = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        direction.pack(fill="x", pady=(0, 6))
        services = [s for s in SERVICES if s in controller.providers]
        source, target = controller.direction

        customtkinter.CTkLabel(direction, text="From", text_color=SECONDARY_TEXT).pack(side="left", padx=(0, 6))
        self.source_menu = customtkinter.CTkOptionMenu(
            direction, values=services, command=lambda v: self.set_direction(v, None), width=130
        )
        self.source_menu.set(source)
        self.source_menu.pack(side="left", padx=(0, 6))
        attach_tooltip(self.source_menu, "hires.port.source")

        # Text, not "⇄": the UI font has no such glyph and drew "=".
        self.swap_button = swap = _button(direction, "Swap", self.swap, width=60)
        swap.pack(side="left", padx=(0, 6))
        attach_tooltip(swap, "hires.port.swap")

        customtkinter.CTkLabel(direction, text="To", text_color=SECONDARY_TEXT).pack(side="left", padx=(0, 6))
        self.target_menu = customtkinter.CTkOptionMenu(
            direction, values=services, command=lambda v: self.set_direction(None, v), width=130
        )
        self.target_menu.set(target)
        self.target_menu.pack(side="left")
        attach_tooltip(self.target_menu, "hires.port.target")

        self.hint_label = customtkinter.CTkLabel(
            self.frame, text="", text_color=WARNING, anchor="w", justify="left", wraplength=900
        )
        self.hint_label.pack(fill="x", pady=(0, 4))

        # -- source row -----------------------------------------------------
        top = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        top.pack(fill="x", pady=(0, 8))

        self.url_entry = customtkinter.CTkEntry(top, height=30)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        attach_tooltip(self.url_entry, "hires.port.url")

        convert_button = _button(top, "Port", self.convert_from_url, width=90, fg=PRIMARY_ACTION)
        convert_button.pack(side="left", padx=(0, 6))
        attach_tooltip(convert_button, "hires.port.convert")
        self.mine_button = _button(top, "⟳  My playlists", self.load_playlists, width=130)
        self.mine_button.pack(side="left")
        attach_tooltip(self.mine_button, "hires.port.my_playlists")

        # -- playlist list --------------------------------------------------
        self.list = PlaylistList(self.frame, self._on_select, on_check=self._on_check)
        self.list.pack(fill="both", expand=True, pady=(0, 8))

        # -- progress -------------------------------------------------------
        self.progress = customtkinter.CTkProgressBar(self.frame, height=6)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(0, 4))

        self.status_label = customtkinter.CTkLabel(
            self.frame, text="", text_color=SECONDARY_TEXT, anchor="w"
        )
        self.status_label.pack(fill="x", pady=(0, 6))

        # -- actions --------------------------------------------------------
        actions = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        actions.pack(fill="x")

        self.queue_button = _button(
            actions, "⬇  Queue matches", self.queue_confident, width=140, state="disabled"
        )
        self.queue_button.pack(side="left", padx=(0, 6))
        attach_tooltip(self.queue_button, "hires.port.queue_matches")

        self.review_button = _button(
            actions, "Review uncertain…", self.open_review, width=150, state="disabled"
        )
        self.review_button.pack(side="left", padx=(0, 6))
        attach_tooltip(self.review_button, "hires.port.review")

        self.select_all_button = _button(
            actions, "Select all", self._toggle_all, width=90, state="disabled"
        )
        self.select_all_button.pack(side="left")
        attach_tooltip(self.select_all_button, "hires.port.select_all")

        self.quality_label = customtkinter.CTkLabel(
            actions, text="", text_color=GRAY_TEXT, font=("", 11)
        )
        self.quality_label.pack(side="right")

        self._apply_direction()

    # -- direction ----------------------------------------------------------
    @property
    def source(self) -> str:
        return self.controller.source_service

    @property
    def target(self) -> str:
        return self.controller.target_service

    def set_direction(self, source: Optional[str], target: Optional[str]) -> None:
        """Change one end; picking the other end's service swaps the two."""
        old_source, old_target = self.controller.direction
        source = source or old_source
        target = target or old_target
        if source == target:
            if source != old_source:
                target = old_source
            else:
                source = old_target
        if (source, target) == (old_source, old_target):
            self.source_menu.set(source)
            self.target_menu.set(target)
            return
        self.controller.set_direction(source, target)
        self._apply_direction()

    def swap(self) -> None:
        self.set_direction(self.target, self.source)

    def _apply_direction(self) -> None:
        source, target = self.controller.direction
        self.source_menu.set(source)
        self.target_menu.set(target)
        self.report = None
        self.review_choices = {}
        self.progress.set(0)
        self.queue_button.configure(state="disabled", text="⬇  Queue matches")
        self.review_button.configure(state="disabled")
        self.url_entry.configure(
            placeholder_text=f"Paste a {source} playlist link — or load your own playlists →"
        )
        self.list.set_playlists([])
        self.list.set_message(
            f"Paste a public {source} playlist link above, or click “My playlists”\n"
            f"to list your own. Every track is then searched on {target}.\n"
            "Tick several to port and queue them in one go.\n"
            "Nothing is ever changed in your accounts."
        )
        self.status_label.configure(text="", text_color=SECONDARY_TEXT)
        self.quality_label.configure(text=quality_note(target))
        self.hint_label.configure(text="")
        self.controller.read_statuses(self._on_statuses)

    def _on_statuses(self, source: ProviderStatus, target: ProviderStatus) -> None:
        if (source.service, target.service) != self.controller.direction:
            return  # the direction changed while this was being read
        hints = []
        if source.hint:
            hints.append(f"{source.service}: {source.hint}")
        if not target.available or not target.can_search:
            hints.append(f"{target.service}: {target.hint or 'cannot be searched.'}")
        self.hint_label.configure(text="\n".join(hints))
        # "My playlists" stays usable: a module that loads late would leave a
        # disabled button dead. The hint says what is missing.

    # -- loading ------------------------------------------------------------
    def load_playlists(self) -> None:
        """List the source's own playlists (Spotify signs in on demand)."""
        self.list.set_message(f"Loading your {self.source} playlists…")
        self.status_label.configure(text="", text_color=SECONDARY_TEXT)
        self.controller.load_playlists(self._on_playlists, self._on_error, on_status=self._on_status)

    def _on_status(self, message: str) -> None:
        """Progress from the sign-in flow (browser consent can take a while)."""
        self.list.set_message(message)
        self.status_label.configure(text=message, text_color=SECONDARY_TEXT)

    def _on_playlists(self, root: FolderRef) -> None:
        self.list.set_root(root)
        self.status_label.configure(text=f"{root.total_playlists} playlists", text_color=SECONDARY_TEXT)
        self._on_check()

    def _on_select(self, playlist: PlaylistRef) -> None:
        # With boxes ticked the click is being used to browse, not to start a
        # conversion the user did not ask for.
        if self.list.checked:
            return
        self.status_label.configure(text=f"Selected: {playlist.name}", text_color=SECONDARY_TEXT)
        self.start_conversion(playlist)

    def _on_check(self) -> None:
        checked = self.list.checked
        self.select_all_button.configure(
            text="Select none" if checked else "Select all",
            state="normal" if self.list.playlists else "disabled",
        )
        if checked:
            self.queue_button.configure(text=f"⬇  Port & queue {len(checked)}", state="normal")
            self.review_button.configure(state="disabled")
            self.status_label.configure(text=_selection_label(checked), text_color=SECONDARY_TEXT)
        else:
            self.queue_button.configure(
                text="⬇  Queue matches", state="normal" if self.report else "disabled"
            )
            self.review_button.configure(
                state="normal" if (self.report and self.report.needs_review) else "disabled"
            )

    def _toggle_all(self) -> None:
        self.list.check_all(not self.list.checked)

    # -- conversion ---------------------------------------------------------
    def convert_from_url(self) -> None:
        url = self.url_entry.get().strip()
        if not url:
            self.status_label.configure(
                text=f"Paste a {self.source} playlist link first.", text_color=WARNING
            )
            return
        self.start_conversion(url)

    def start_conversion(self, playlist: Any) -> None:
        self.report = None
        self.review_choices = {}
        self.progress.set(0)
        self.queue_button.configure(state="disabled")
        self.review_button.configure(state="disabled")
        self.status_label.configure(text="Reading playlist…", text_color=SECONDARY_TEXT)
        self.controller.convert(playlist, self._on_progress, self._on_converted, self._on_error)

    def _on_progress(self, done: int, total: int, _result) -> None:
        # A fast port reports hundreds of tracks between two dispatcher polls,
        # and redrawing for each one froze the window for half a second
        # (measure_playlist.py: 540 ms with 500 tracks). The last one always draws.
        now = time.monotonic()
        if done < total and now - self._progress_drawn_at < PROGRESS_REDRAW_S:
            return
        self._progress_drawn_at = now
        if total > 0:
            self.progress.set(done / total)
        self.status_label.configure(
            text=self.controller.progress_text(done, total), text_color=SECONDARY_TEXT
        )

    def _on_converted(self, report: ConversionReport) -> None:
        self.report = report
        self.progress.set(1)
        self.status_label.configure(
            text=self.controller.result_summary(report), text_color=WHITE_TEXT
        )
        self.queue_button.configure(state="normal")
        self.review_button.configure(state="normal" if report.needs_review else "disabled")

    def _on_error(self, message: str) -> None:
        self.progress.set(0)
        self.status_label.configure(text=message, text_color=ERROR)
        self.controller.read_statuses(self._on_statuses)  # the hint may be stale

    # -- queueing -----------------------------------------------------------
    def queue_confident(self) -> None:
        """Queue the current report, or port every ticked playlist in turn."""
        checked = self.list.checked
        if checked:
            self.convert_checked(checked)
            return
        if self.report is None:
            return
        try:
            items = self.controller.enqueue_confident_only(self.report)
        except Exception as exc:
            self.status_label.configure(text=str(exc), text_color=ERROR)
            return
        self.status_label.configure(
            text=queued_text(len(items), self.report.target_service), text_color=SUCCESS
        )

    def convert_checked(self, playlists) -> None:
        """Port several playlists back to back, queueing each as it finishes."""
        self.report = None
        self.progress.set(0)
        self.queue_button.configure(state="disabled")
        self.review_button.configure(state="disabled")
        self._batch_total = len(playlists)
        self.status_label.configure(
            text=f"Porting {self._batch_total} playlists…", text_color=SECONDARY_TEXT
        )
        self.controller.convert_many(
            playlists,
            self._on_batch_playlist,
            self._on_progress,
            self._on_batch_done,
            self._on_error,
        )

    def _on_batch_playlist(self, index: int, total: int, playlist) -> None:
        name = getattr(playlist, "name", "") or getattr(playlist, "id", "")
        self.progress.set(index / total if total else 0)
        self.status_label.configure(text=f"[{index + 1}/{total}] {name}", text_color=SECONDARY_TEXT)

    def _on_batch_done(self, reports) -> None:
        self.progress.set(1)
        queued = sum(
            len([r for r in report.results if r.decision == MatchDecision.AUTO_ACCEPT.value])
            for report in reports
        )
        target = reports[0].target_service if reports else self.target
        self.status_label.configure(
            text=queued_text(queued, target, playlists=len(reports)), text_color=SUCCESS
        )
        self.list.check_all(False)

    def open_review(self) -> None:
        if self.report is None:
            return
        rows = self.controller.review_rows(self.report)
        if not rows:
            return
        ReviewDialog(self.frame, rows, self._apply_review)

    def _apply_review(self, decisions: Dict[str, Any]) -> None:
        try:
            items = self.controller.apply_review_and_enqueue(decisions, report=self.report)
        except Exception as exc:
            self.status_label.configure(text=str(exc), text_color=ERROR)
            return
        target = self.report.target_service if self.report else self.target
        self.status_label.configure(text=queued_text(len(items), target), text_color=SUCCESS)
        self.review_button.configure(state="disabled")
