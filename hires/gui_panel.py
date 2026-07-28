"""The three hi-res tabs: Queue, TIDAL Playlists, Spotify -> TIDAL.

Widget code only. Every decision lives in :mod:`hires.controllers`, which is
importable without tkinter and covered by tests; this module just draws things
and forwards clicks. Keep it that way -- it is the one part of the suite that
cannot be tested on a headless machine.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import customtkinter
import tkinter

from .controllers import (
    QueueController,
    ReviewRow,
    SpotifyImportController,
    TidalBrowserController,
    UiDispatcher,
)
from .models import ConversionReport, PlaylistRef, QueueStatus
from .quality import HIRES, label_for

# Mirrors the palette in gui.py. Duplicated rather than imported because gui.py
# is the __main__ module and importing it back would be circular.
WHITE_TEXT = "#E4E5E6"
SECONDARY_TEXT = "#A8A8AA"
GRAY_TEXT = "#8A8C8F"
SURFACE = "#1D1E1E"
CONTAINER = "#2B2B2B"
BORDER = "#565B5E"
HOVER = "#474747"
BUTTON_BG = "#35373b"
PRIMARY_ACTION = "#2D5A88"
SUCCESS = "#00C851"
WARNING = "#F2C94C"
ERROR = "#E53935"
ROW_HOVER = "#272828"

STATUS_COLORS = {
    QueueStatus.PENDING.value: SECONDARY_TEXT,
    QueueStatus.ACTIVE.value: WARNING,
    QueueStatus.DONE.value: SUCCESS,
    QueueStatus.FAILED.value: ERROR,
    QueueStatus.CANCELLED.value: GRAY_TEXT,
}


def _button(parent, text, command, width=110, fg=BUTTON_BG, **kwargs):
    return customtkinter.CTkButton(
        parent,
        text=text,
        command=command,
        width=width,
        height=28,
        fg_color=fg,
        hover_color=HOVER,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Queue tab
# ---------------------------------------------------------------------------

class QueueTab:
    """Shows the persistent download queue and lets the user reorder it."""

    def __init__(self, parent, controller: QueueController, dispatch: Optional[UiDispatcher] = None):
        self.controller = controller
        # Queue mutations arrive from worker threads, and widget.after() is not
        # thread-safe -- everything has to go through the dispatcher.
        self.dispatch = dispatch or UiDispatcher(parent.winfo_toplevel())
        self.selected_id: Optional[str] = None
        self._row_widgets: Dict[str, Any] = {}

        self.frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=9, pady=10)

        # -- toolbar --------------------------------------------------------
        bar = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))

        self.pause_button = _button(bar, "⏸  Pause", self._toggle_pause, fg=PRIMARY_ACTION)
        self.pause_button.pack(side="left", padx=(0, 6))

        _button(bar, "↑", self._move_up, width=36).pack(side="left", padx=(0, 3))
        _button(bar, "↓", self._move_down, width=36).pack(side="left", padx=(0, 6))
        _button(bar, "Remove", self._remove, width=80).pack(side="left", padx=(0, 6))
        _button(bar, "Retry failed", self._retry_failed, width=110).pack(side="left", padx=(0, 6))
        _button(bar, "Clear finished", self._clear_finished, width=120).pack(side="left")

        self.summary_label = customtkinter.CTkLabel(
            bar, text="", text_color=SECONDARY_TEXT, anchor="e"
        )
        self.summary_label.pack(side="right")

        # -- list -----------------------------------------------------------
        self.list_frame = customtkinter.CTkScrollableFrame(
            self.frame, fg_color=SURFACE, border_color=BORDER, border_width=1
        )
        self.list_frame.pack(fill="both", expand=True)

        self._unsubscribe = controller.subscribe(self._on_queue_changed)
        self.refresh()

    # -- events -------------------------------------------------------------
    def _on_queue_changed(self) -> None:
        """Queue mutated, possibly from a worker thread -> hop to the UI thread."""
        self.dispatch(self.refresh)

    def _toggle_pause(self) -> None:
        paused = self.controller.toggle_pause()
        self.pause_button.configure(
            text="▶  Resume" if paused else "⏸  Pause",
            fg_color=WARNING if paused else PRIMARY_ACTION,
        )
        self.refresh()

    def _move_up(self) -> None:
        if self.selected_id:
            self.controller.move_up(self.selected_id)

    def _move_down(self) -> None:
        if self.selected_id:
            self.controller.move_down(self.selected_id)

    def _remove(self) -> None:
        if self.selected_id:
            self.controller.remove(self.selected_id)
            self.selected_id = None

    def _retry_failed(self) -> None:
        self.controller.retry_all_failed()

    def _clear_finished(self) -> None:
        self.controller.clear_finished()

    def _select(self, item_id: str) -> None:
        self.selected_id = item_id
        self.refresh()

    # -- rendering ----------------------------------------------------------
    def refresh(self) -> None:
        try:
            if not self.frame.winfo_exists():
                return
        except Exception:
            return

        for child in self.list_frame.winfo_children():
            child.destroy()
        self._row_widgets.clear()

        rows = self.controller.rows()
        self.summary_label.configure(text=self.controller.summary())

        if not rows:
            customtkinter.CTkLabel(
                self.list_frame,
                text="Queue is empty.\n\nAdd playlists from the TIDAL or Spotify tab.",
                text_color=GRAY_TEXT,
                justify="center",
            ).pack(pady=30)
            return

        for row in rows:
            selected = row.id == self.selected_id
            container = customtkinter.CTkFrame(
                self.list_frame,
                fg_color=ROW_HOVER if selected else "transparent",
                corner_radius=4,
            )
            container.pack(fill="x", padx=4, pady=1)
            container.bind("<Button-1>", lambda _e, i=row.id: self._select(i))

            status = customtkinter.CTkLabel(
                container,
                text=row.symbol,
                width=24,
                text_color=STATUS_COLORS.get(row.status, SECONDARY_TEXT),
            )
            status.pack(side="left", padx=(6, 2))

            text_frame = customtkinter.CTkFrame(container, fg_color="transparent")
            text_frame.pack(side="left", fill="x", expand=True, pady=3)

            title = customtkinter.CTkLabel(
                text_frame, text=row.title, text_color=WHITE_TEXT, anchor="w"
            )
            title.pack(fill="x")

            detail_text = row.detail
            if row.error:
                detail_text = f"{detail_text} · {row.error}" if detail_text else row.error
            if detail_text:
                customtkinter.CTkLabel(
                    text_frame,
                    text=detail_text,
                    text_color=ERROR if row.error else GRAY_TEXT,
                    anchor="w",
                    font=("", 11),
                ).pack(fill="x")

            if row.quality:
                customtkinter.CTkLabel(
                    container, text=row.quality, text_color=GRAY_TEXT, font=("", 11)
                ).pack(side="right", padx=8)

            for widget in (status, text_frame, title):
                widget.bind("<Button-1>", lambda _e, i=row.id: self._select(i))

    def destroy(self) -> None:
        try:
            self._unsubscribe()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared playlist list widget
# ---------------------------------------------------------------------------

class PlaylistList:
    """A scrollable, single-select list of playlists."""

    def __init__(self, parent, on_select: Callable[[PlaylistRef], None]):
        self.on_select = on_select
        self.selected: Optional[PlaylistRef] = None
        self.playlists: List[PlaylistRef] = []

        self.frame = customtkinter.CTkScrollableFrame(
            parent, fg_color=SURFACE, border_color=BORDER, border_width=1
        )

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    def set_message(self, text: str, color: str = GRAY_TEXT) -> None:
        for child in self.frame.winfo_children():
            child.destroy()
        customtkinter.CTkLabel(
            self.frame, text=text, text_color=color, justify="center", wraplength=520
        ).pack(pady=30)

    def set_playlists(self, playlists: List[PlaylistRef]) -> None:
        self.playlists = list(playlists)
        self.selected = None
        for child in self.frame.winfo_children():
            child.destroy()

        if not playlists:
            self.set_message("No playlists found.")
            return

        for playlist in playlists:
            row = customtkinter.CTkFrame(self.frame, fg_color="transparent", corner_radius=4)
            row.pack(fill="x", padx=4, pady=1)

            name = customtkinter.CTkLabel(
                row, text=playlist.name or playlist.id, text_color=WHITE_TEXT, anchor="w"
            )
            name.pack(side="left", padx=8, pady=4, fill="x", expand=True)

            meta = f"{playlist.track_count} tracks" if playlist.track_count else ""
            if playlist.owner:
                meta = f"{meta} · {playlist.owner}" if meta else playlist.owner
            if meta:
                customtkinter.CTkLabel(
                    row, text=meta, text_color=GRAY_TEXT, font=("", 11)
                ).pack(side="right", padx=8)

            for widget in (row, name):
                widget.bind("<Button-1>", lambda _e, p=playlist, r=row: self._select(p, r))

    def _select(self, playlist: PlaylistRef, row) -> None:
        self.selected = playlist
        for child in self.frame.winfo_children():
            try:
                child.configure(fg_color="transparent")
            except Exception:
                pass
        try:
            row.configure(fg_color=ROW_HOVER)
        except Exception:
            pass
        self.on_select(playlist)


# ---------------------------------------------------------------------------
# TIDAL playlists tab
# ---------------------------------------------------------------------------

class TidalPlaylistTab:
    """Browse your own TIDAL playlists and send them to the queue."""

    def __init__(self, parent, controller: TidalBrowserController):
        self.controller = controller

        self.frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=9, pady=10)

        bar = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))

        _button(bar, "⟳  Load playlists", self.load, width=140, fg=PRIMARY_ACTION).pack(
            side="left", padx=(0, 6)
        )

        self.favorites_var = tkinter.BooleanVar(value=True)
        customtkinter.CTkCheckBox(
            bar,
            text="Include favorites",
            variable=self.favorites_var,
            text_color=SECONDARY_TEXT,
            checkbox_width=18,
            checkbox_height=18,
        ).pack(side="left", padx=(0, 12))

        self.download_button = _button(
            bar, "⬇  Add to queue", self.enqueue_selected, width=130, state="disabled"
        )
        self.download_button.pack(side="left")

        self.status_label = customtkinter.CTkLabel(bar, text="", text_color=SECONDARY_TEXT)
        self.status_label.pack(side="right")

        self.list = PlaylistList(self.frame, self._on_select)
        self.list.pack(fill="both", expand=True)
        self.list.set_message(
            "Click “Load playlists” to show your TIDAL playlists.\n"
            "Requires the TIDAL module to be installed and logged in."
        )

    def _on_select(self, playlist: PlaylistRef) -> None:
        self.download_button.configure(state="normal")
        self.status_label.configure(text=f"Selected: {playlist.name}", text_color=SECONDARY_TEXT)

    def load(self) -> None:
        self.status_label.configure(text="Loading…", text_color=SECONDARY_TEXT)
        self.list.set_message("Loading your TIDAL playlists…")
        self.download_button.configure(state="disabled")
        self.controller.load_playlists(
            self._on_loaded, self._on_error, include_favorites=self.favorites_var.get()
        )

    def _on_loaded(self, playlists: List[PlaylistRef]) -> None:
        self.list.set_playlists(playlists)
        self.status_label.configure(
            text=f"{len(playlists)} playlists", text_color=SECONDARY_TEXT
        )

    def _on_error(self, message: str) -> None:
        self.list.set_message(message, color=ERROR)
        self.status_label.configure(text="Failed", text_color=ERROR)

    def enqueue_selected(self) -> None:
        playlist = self.list.selected
        if playlist is None:
            return
        try:
            self.controller.enqueue_playlist_as_one(playlist)
        except Exception as exc:
            self.status_label.configure(text=str(exc), text_color=ERROR)
            return
        self.status_label.configure(
            text=f"Queued: {playlist.name}", text_color=SUCCESS
        )


# ---------------------------------------------------------------------------
# Spotify -> TIDAL tab
# ---------------------------------------------------------------------------

class SpotifyConvertTab:
    """Convert a Spotify playlist to TIDAL and download it in hi-res."""

    def __init__(self, parent, controller: SpotifyImportController):
        self.controller = controller
        self.report: Optional[ConversionReport] = None
        self.review_choices: Dict[str, Any] = {}

        self.frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=9, pady=10)

        # -- source row -----------------------------------------------------
        top = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        top.pack(fill="x", pady=(0, 8))

        self.url_entry = customtkinter.CTkEntry(
            top,
            placeholder_text="Paste a Spotify playlist link — or load your own playlists →",
            height=30,
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        _button(top, "Convert", self.convert_from_url, width=90, fg=PRIMARY_ACTION).pack(
            side="left", padx=(0, 6)
        )
        _button(top, "⟳  My playlists", self.load_playlists, width=130).pack(side="left")

        # -- playlist list --------------------------------------------------
        self.list = PlaylistList(self.frame, self._on_select)
        self.list.pack(fill="both", expand=True, pady=(0, 8))
        self.list.set_message(
            "Paste a public Spotify playlist link above, or click “My playlists”\n"
            "to sign in and list your own playlists and Liked Songs."
        )

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

        self.review_button = _button(
            actions, "Review uncertain…", self.open_review, width=150, state="disabled"
        )
        self.review_button.pack(side="left")

        customtkinter.CTkLabel(
            actions,
            text=f"Downloads at {label_for(HIRES)}",
            text_color=GRAY_TEXT,
            font=("", 11),
        ).pack(side="right")

    # -- loading ------------------------------------------------------------
    def load_playlists(self) -> None:
        """List the user's own playlists, signing in to Spotify if needed."""
        self.list.set_message("Loading your Spotify playlists…")
        self.status_label.configure(text="", text_color=SECONDARY_TEXT)
        self.controller.load_playlists(
            self._on_playlists, self._on_error, on_status=self._on_status
        )

    def _on_status(self, message: str) -> None:
        """Progress from the sign-in flow (browser consent can take a while)."""
        self.list.set_message(message)
        self.status_label.configure(text=message, text_color=SECONDARY_TEXT)

    def _on_playlists(self, playlists: List[PlaylistRef]) -> None:
        self.list.set_playlists(playlists)
        self.status_label.configure(
            text=f"{len(playlists)} playlists", text_color=SECONDARY_TEXT
        )

    def _on_select(self, playlist: PlaylistRef) -> None:
        self.status_label.configure(
            text=f"Selected: {playlist.name}", text_color=SECONDARY_TEXT
        )
        self.start_conversion(playlist)

    # -- conversion ---------------------------------------------------------
    def convert_from_url(self) -> None:
        url = self.url_entry.get().strip()
        if not url:
            self.status_label.configure(text="Paste a Spotify playlist link first.", text_color=WARNING)
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
        self.review_button.configure(
            state="normal" if report.needs_review else "disabled"
        )

    def _on_error(self, message: str) -> None:
        self.progress.set(0)
        self.status_label.configure(text=message, text_color=ERROR)

    # -- queueing -----------------------------------------------------------
    def queue_confident(self) -> None:
        if self.report is None:
            return
        try:
            items = self.controller.enqueue_confident_only(self.report)
        except Exception as exc:
            self.status_label.configure(text=str(exc), text_color=ERROR)
            return
        self.status_label.configure(
            text=f"Queued {len(items)} tracks in hi-res.", text_color=SUCCESS
        )

    def open_review(self) -> None:
        if self.report is None:
            return
        rows = self.controller.review_rows(self.report)
        if not rows:
            return
        ReviewDialog(self.frame, rows, self._apply_review)

    def _apply_review(self, decisions: Dict[str, Any]) -> None:
        try:
            items = self.controller.apply_review_and_enqueue(
                decisions, report=self.report
            )
        except Exception as exc:
            self.status_label.configure(text=str(exc), text_color=ERROR)
            return
        self.status_label.configure(
            text=f"Queued {len(items)} tracks in hi-res.", text_color=SUCCESS
        )
        self.review_button.configure(state="disabled")


# ---------------------------------------------------------------------------
# Review dialog
# ---------------------------------------------------------------------------

class ReviewDialog:
    """Lets the user confirm, swap or drop each uncertain match."""

    def __init__(self, parent, rows: List[ReviewRow], on_apply: Callable[[Dict[str, Any]], None]):
        self.rows = rows
        self.on_apply = on_apply
        self.choices: Dict[str, Any] = {row.source_id: True for row in rows}
        self._option_vars: Dict[str, Any] = {}

        self.window = customtkinter.CTkToplevel(parent)
        self.window.title("Review uncertain matches")
        self.window.geometry("820x560")
        self.window.transient(parent.winfo_toplevel())
        self.window.after(150, self.window.grab_set)  # after() so it works on Linux

        customtkinter.CTkLabel(
            self.window,
            text=(
                f"{len(rows)} tracks could not be matched with confidence. "
                "Confirm, pick another version, or skip."
            ),
            text_color=SECONDARY_TEXT,
            wraplength=780,
            justify="left",
        ).pack(fill="x", padx=14, pady=(12, 8))

        body = customtkinter.CTkScrollableFrame(self.window, fg_color=SURFACE)
        body.pack(fill="both", expand=True, padx=14)

        for row in rows:
            self._build_row(body, row)

        footer = customtkinter.CTkFrame(self.window, fg_color="transparent")
        footer.pack(fill="x", padx=14, pady=10)
        _button(footer, "Skip all", self._skip_all, width=90).pack(side="left")
        _button(footer, "Accept all", self._accept_all, width=100).pack(side="left", padx=6)
        _button(footer, "Cancel", self.window.destroy, width=90).pack(side="right")
        _button(
            footer, "Queue selected", self._apply, width=130, fg=PRIMARY_ACTION
        ).pack(side="right", padx=6)

    def _build_row(self, parent, row: ReviewRow) -> None:
        card = customtkinter.CTkFrame(parent, fg_color=CONTAINER, corner_radius=6)
        card.pack(fill="x", padx=4, pady=4)

        header = customtkinter.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(8, 2))

        customtkinter.CTkLabel(
            header, text=row.source_label, text_color=WHITE_TEXT, anchor="w"
        ).pack(side="left", fill="x", expand=True)
        customtkinter.CTkLabel(
            header, text=row.score_text, text_color=WARNING, font=("", 11)
        ).pack(side="right")

        # Choice: the best match, any alternative, or skip.
        options = [f"✓  {row.match_label}"]
        mapping = {options[0]: True}
        for tidal_id, label, score in row.alternatives:
            option = f"↳  {label}  ({score * 100:.0f}%)"
            options.append(option)
            mapping[option] = tidal_id
        skip_option = "✗  Skip this track"
        options.append(skip_option)
        mapping[skip_option] = False

        var = tkinter.StringVar(value=options[0])
        self._option_vars[row.source_id] = (var, mapping)

        customtkinter.CTkOptionMenu(
            card,
            values=options,
            variable=var,
            width=760,
            fg_color=BUTTON_BG,
            button_color=BUTTON_BG,
            button_hover_color=HOVER,
            command=lambda _v, sid=row.source_id: self._on_choice(sid),
        ).pack(fill="x", padx=10, pady=(2, 4))

        if row.reasons:
            customtkinter.CTkLabel(
                card, text=row.reasons, text_color=GRAY_TEXT, font=("", 11), anchor="w"
            ).pack(fill="x", padx=10, pady=(0, 8))

    def _on_choice(self, source_id: str) -> None:
        var, mapping = self._option_vars[source_id]
        self.choices[source_id] = mapping.get(var.get(), True)

    def _accept_all(self) -> None:
        for source_id, (var, mapping) in self._option_vars.items():
            first = next(iter(mapping))
            var.set(first)
            self.choices[source_id] = mapping[first]

    def _skip_all(self) -> None:
        for source_id, (var, mapping) in self._option_vars.items():
            skip = next((k for k, v in mapping.items() if v is False), None)
            if skip:
                var.set(skip)
                self.choices[source_id] = False

    def _apply(self) -> None:
        decisions = dict(self.choices)
        self.window.destroy()
        self.on_apply(decisions)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def build_tabs(
    tabview,
    *,
    queue,
    app=None,
    tidal_library_provider: Callable[[], Any],
    spotify_source_provider: Callable[[], Any],
    matcher_provider: Callable[[], Any],
    quality_provider: Optional[Callable[[], str]] = None,
    output_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Add the three hi-res tabs to an existing CTkTabview.

    Returns the tab objects so gui.py can refresh them later.
    """
    dispatch = UiDispatcher(app)

    queue_tab = QueueTab(tabview.add("Queue"), QueueController(queue), dispatch=dispatch)

    tidal_tab = TidalPlaylistTab(
        tabview.add("TIDAL Playlists"),
        TidalBrowserController(
            tidal_library_provider,
            queue,
            dispatch=dispatch,
            quality_provider=quality_provider,
            output_provider=output_provider,
        ),
    )

    spotify_tab = SpotifyConvertTab(
        tabview.add("Spotify → TIDAL"),
        SpotifyImportController(
            spotify_source_provider,
            matcher_provider,
            queue,
            dispatch=dispatch,
            quality_provider=quality_provider,
            output_provider=output_provider,
        ),
    )

    return {"queue": queue_tab, "tidal": tidal_tab, "spotify": spotify_tab}
