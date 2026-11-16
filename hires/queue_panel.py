"""The Queue tab: the download queue, grouped by playlist.

Widget code only; the rules (what is a group, what moving a group means, what
"Clear all" stops) live in :mod:`hires.controllers` and
:mod:`hires.queue_groups` and are tested headlessly.

Layout: one header per playlist (name, progress, "To front", remove) with the
playlist's tracks below it. Every line is a pooled widget that is retargeted
rather than rebuilt -- rebuilding ~14 CTk widgets per item on every queue
change is what once froze the app for tens of seconds -- and all lines sit in
one grid column, so moving a line costs a single ``grid(row=…)``.
"""

from __future__ import annotations

from typing import Callable, List, Optional

import customtkinter

from .controllers import QueueController, UiDispatcher
from .dialogs import confirm_destructive
from .help_widget import attach_tooltip
from .models import QueueStatus
from .queue_groups import QueueGroup
from .ui_style import (
    BORDER,
    BUTTON_BG,
    CONTAINER,
    DANGER_BG,
    DANGER_HOVER,
    ERROR,
    GRAY_TEXT,
    PRIMARY_ACTION,
    ROW_HOVER,
    SECONDARY_TEXT,
    SUCCESS,
    SURFACE,
    WARNING,
    WHITE_TEXT,
    button,
)

STATUS_COLORS = {
    QueueStatus.PENDING.value: SECONDARY_TEXT,
    QueueStatus.ACTIVE.value: WARNING,
    QueueStatus.DONE.value: SUCCESS,
    QueueStatus.FAILED.value: ERROR,
    QueueStatus.CANCELLED.value: GRAY_TEXT,
}

#: How many track rows we are willing to have on screen at once.
#:
#: Queueing whole folders puts one item per *track* in the queue, so a handful
#: of playlists is easily two thousand entries. Nobody scrolls through two
#: thousand rows, so we draw what fits this budget -- group by group -- and
#: say how much is behind it. Playlist headers do not count against it.
MAX_RENDERED_ROWS = 150


class _Line:
    """Shared bits of a pooled line: one container gridded at a row."""

    def __init__(self):
        self.container = None
        self._grid_row: Optional[int] = None
        self._shown: dict = {}

    def _set(self, widget, name: str, **options) -> None:
        """``configure`` only what changed -- a CTk configure redraws."""
        key = (name, tuple(sorted(options.items())))
        if self._shown.get(name) == key:
            return
        self._shown[name] = key
        widget.configure(**options)

    def place_at(self, row: int) -> None:
        if self._grid_row != row or not self.container.winfo_ismapped():
            self.container.grid(row=row, column=0, sticky="ew", padx=4, pady=1)
            self._grid_row = row

    def hide(self) -> None:
        if self._grid_row is not None:
            self.container.grid_remove()
            self._grid_row = None

    def destroy(self) -> None:
        self.container.destroy()


class _QueueRowWidgets(_Line):
    """The widgets of one track line, kept alive so they can be reused."""

    def __init__(self, parent, on_click: Callable[[str], None]):
        super().__init__()
        self._on_click = on_click
        self.item_id: str = ""

        self.container = customtkinter.CTkFrame(parent, corner_radius=4, fg_color="transparent")
        self.status = customtkinter.CTkLabel(self.container, text="", width=24)
        self.status.pack(side="left", padx=(22, 2))

        self.text_frame = customtkinter.CTkFrame(self.container, fg_color="transparent")
        self.text_frame.pack(side="left", fill="x", expand=True, pady=2)

        self.title = customtkinter.CTkLabel(self.text_frame, text="", text_color=WHITE_TEXT, anchor="w")
        self.title.pack(fill="x")

        self.detail = customtkinter.CTkLabel(
            self.text_frame, text="", text_color=GRAY_TEXT, anchor="w", font=("", 11)
        )
        self.quality = customtkinter.CTkLabel(self.container, text="", text_color=GRAY_TEXT, font=("", 11))

        for widget in (self.container, self.status, self.text_frame, self.title, self.detail):
            widget.bind("<Button-1>", self._clicked)

    def _clicked(self, _event=None) -> None:
        if self.item_id:
            self._on_click(self.item_id)

    def update(self, row, selected: bool) -> None:
        """Point this line at ``row``. Only touches what actually changed."""
        self.item_id = row.id
        self._set(self.container, "bg", fg_color=ROW_HOVER if selected else "transparent")
        self._set(
            self.status,
            "status",
            text=row.symbol,
            text_color=STATUS_COLORS.get(row.status, SECONDARY_TEXT),
        )
        self._set(self.title, "title", text=row.title)

        # Inside a playlist group the playlist name would repeat on every line.
        detail_text = row.subtitle
        if row.error:
            detail_text = f"{detail_text} · {row.error}" if detail_text else row.error
        if detail_text:
            self._set(self.detail, "detail", text=detail_text, text_color=ERROR if row.error else GRAY_TEXT)
            if not self.detail.winfo_ismapped():
                self.detail.pack(fill="x")
        elif self.detail.winfo_ismapped():
            self.detail.pack_forget()

        if row.quality:
            self._set(self.quality, "quality", text=row.quality)
            if not self.quality.winfo_ismapped():
                self.quality.pack(side="right", padx=8)
        elif self.quality.winfo_ismapped():
            self.quality.pack_forget()


class _GroupHeaderWidgets(_Line):
    """A playlist header: name, progress and the two group actions."""

    def __init__(self, parent, on_toggle, on_front, on_remove):
        super().__init__()
        self.key: str = ""
        self.container = customtkinter.CTkFrame(parent, fg_color=CONTAINER, corner_radius=5)

        self.arrow = customtkinter.CTkLabel(self.container, text="▾", width=18, text_color=SECONDARY_TEXT)
        self.arrow.pack(side="left", padx=(6, 0))
        self.name = customtkinter.CTkLabel(self.container, text="", text_color=WHITE_TEXT, anchor="w")
        self.name.pack(side="left", padx=(2, 10), pady=4)
        self.progress = customtkinter.CTkLabel(
            self.container, text="", text_color=SECONDARY_TEXT, font=("", 11), anchor="w"
        )
        self.progress.pack(side="left", fill="x", expand=True)

        self.remove_button = button(
            self.container, "✕", lambda: on_remove(self.key), width=30, fg=BUTTON_BG
        )
        self.remove_button.pack(side="right", padx=(4, 6), pady=3)
        attach_tooltip(self.remove_button, "hires.queue.group_remove")
        self.front_button = button(
            self.container, "⇧  To front", lambda: on_front(self.key), width=96, fg=BUTTON_BG
        )
        self.front_button.pack(side="right", pady=3)
        attach_tooltip(self.front_button, "hires.queue.group_to_front")

        for widget in (self.container, self.arrow, self.name, self.progress):
            widget.bind("<Button-1>", lambda _e: on_toggle(self.key))
        attach_tooltip(self.name, "hires.queue.group_toggle")

    def update(self, group: QueueGroup, rows_shown: bool, is_first: bool) -> None:
        self.key = group.key
        self._set(self.arrow, "arrow", text="▾" if rows_shown else "▸")
        self._set(self.name, "name", text=group.label)
        color = WARNING if group.is_running else SECONDARY_TEXT
        self._set(self.progress, "progress", text=group.progress_text, text_color=color)
        # Already first in line, or nothing left to download: nothing to move.
        can_move = not group.is_finished and not is_first
        self._set(self.front_button, "front", state="normal" if can_move else "disabled")


class QueueTab:
    """Shows the persistent download queue, grouped by playlist."""

    def __init__(
        self,
        parent,
        controller: QueueController,
        dispatch: Optional[UiDispatcher] = None,
        confirm: Optional[Callable[[str, str, str], bool]] = None,
    ):
        """
        Args:
            confirm: ``confirm(title, message, button_label) -> bool``. Defaults
                to :func:`hires.dialogs.confirm_destructive`; tests swap it.
        """
        self.controller = controller
        # Queue mutations arrive from worker threads, and widget.after() is not
        # thread-safe -- everything has to go through the dispatcher.
        self.dispatch = dispatch or UiDispatcher(parent.winfo_toplevel())
        self._confirm = confirm or (
            lambda title, message, label: confirm_destructive(self.frame, title, message, label)
        )
        self.selected_id: Optional[str] = None
        #: Playlist groups the user folded away.
        self.collapsed: set = set()
        #: Pooled lines -- grown and shrunk, never rebuilt on refresh.
        self._rows: List[_QueueRowWidgets] = []
        self._headers: List[_GroupHeaderWidgets] = []
        self._groups: List[QueueGroup] = []
        self._shown_keys: set = set()
        #: Set while a refresh is already on its way to the UI thread. A batch
        #: of queue mutations otherwise queues one full redraw per item.
        self._refresh_pending = False

        self.frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=9, pady=10)

        # -- toolbar --------------------------------------------------------
        bar = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))

        self.pause_button = button(bar, "⏸  Pause", self._toggle_pause, fg=PRIMARY_ACTION)
        self.pause_button.pack(side="left", padx=(0, 6))
        attach_tooltip(self.pause_button, "hires.queue.pause")

        for text, command, width, key in (
            ("↑", self._move_up, 36, "hires.queue.move_up"),
            ("↓", self._move_down, 36, "hires.queue.move_down"),
            ("Remove", self._remove, 80, "hires.queue.remove"),
            ("Retry failed", self._retry_failed, 110, "hires.queue.retry_failed"),
            ("Clear finished", self._clear_finished, 120, "hires.queue.clear_finished"),
        ):
            widget = button(bar, text, command, width=width)
            widget.pack(side="left", padx=(0, 3 if text == "↑" else 6))
            attach_tooltip(widget, key)

        self.clear_all_button = button(
            bar, "Clear all…", self.clear_all, width=96, fg=DANGER_BG, hover_color=DANGER_HOVER
        )
        self.clear_all_button.pack(side="left")
        attach_tooltip(self.clear_all_button, "hires.queue.clear_all")

        self.summary_label = customtkinter.CTkLabel(bar, text="", text_color=SECONDARY_TEXT, anchor="e")
        self.summary_label.pack(side="right")

        # -- list -----------------------------------------------------------
        self.list_frame = customtkinter.CTkScrollableFrame(
            self.frame, fg_color=SURFACE, border_color=BORDER, border_width=1
        )
        self.list_frame.pack(fill="both", expand=True)
        self.list_frame.grid_columnconfigure(0, weight=1)

        self.empty_label = customtkinter.CTkLabel(
            self.list_frame, text="", text_color=GRAY_TEXT, justify="center"
        )
        self.overflow_label = customtkinter.CTkLabel(
            self.list_frame, text="", text_color=SECONDARY_TEXT, justify="center"
        )

        self._unsubscribe = controller.subscribe(self._on_queue_changed)
        self.refresh()

    # -- events -------------------------------------------------------------
    def _on_queue_changed(self) -> None:
        """Queue mutated, possibly from a worker thread -> hop to the UI thread."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self.dispatch(self._refresh_from_queue)

    def _refresh_from_queue(self) -> None:
        self._refresh_pending = False
        self.refresh()

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

    def clear_all(self) -> bool:
        """The "Clear all" button: ask, then empty. True when it cleared."""
        question = self.controller.clear_all_question()
        if question is None:
            return False
        if not self._confirm("Clear the whole queue?", question, "Clear all"):
            return False
        self.controller.clear_all()
        self.selected_id = None
        self.refresh()
        return True

    def move_group_to_front(self, key: str) -> None:
        self.controller.move_group_to_front(key)
        self.collapsed.discard(key)

    def remove_group(self, key: str) -> bool:
        group = next((g for g in self._groups if g.key == key), None)
        if group is None:
            return False
        removable = group.total - group.active
        if removable <= 0:
            return False
        message = f"Remove {removable} {'entry' if removable == 1 else 'entries'} of “{group.label}” from the queue?"
        if group.active:
            message += "\n\nThe track that is downloading finishes first."
        if not self._confirm("Remove playlist?", message, "Remove"):
            return False
        self.controller.remove_group(key)
        self.refresh()
        return True

    def toggle_group(self, key: str) -> None:
        """Fold a playlist away -- or bring one that did not fit into view.

        A header whose tracks are not shown because the row budget ran out
        reads as "click to see this one": every other playlist is folded so
        this one gets the space.
        """
        if key in self.collapsed:
            self.collapsed.discard(key)
        elif key in self._shown_keys:
            self.collapsed.add(key)
        else:
            self.collapsed = {g.key for g in self._groups if g.key != key}
        self.refresh()

    def _select(self, item_id: str) -> None:
        self.selected_id = item_id
        self.refresh()

    def visible_row_count(self) -> int:
        """How many track lines are on screen -- at most ``MAX_RENDERED_ROWS``."""
        return len(self._rows)

    def visible_group_count(self) -> int:
        return len(self._headers)

    # -- rendering ----------------------------------------------------------
    def refresh(self) -> None:
        try:
            if not self.frame.winfo_exists():
                return
        except Exception:
            return

        self.summary_label.configure(text=self.controller.summary())
        groups, rows_by_key, hidden = self.controller.grouped_rows(
            MAX_RENDERED_ROWS, collapsed=self.collapsed
        )
        self._groups = groups
        self._shown_keys = {key for key, rows in rows_by_key.items() if rows}
        total_rows = sum(len(rows) for rows in rows_by_key.values())

        # Grow or shrink the pools; lines are only destroyed when the queue
        # actually got shorter.
        self._resize(self._headers, len(groups), self._new_header)
        self._resize(self._rows, total_rows, lambda: _QueueRowWidgets(self.list_frame, self._select))

        next_key = self.controller.next_group_key()
        grid_row = 0
        row_index = 0
        for index, group in enumerate(groups):
            header = self._headers[index]
            group_rows = rows_by_key.get(group.key, [])
            header.update(group, rows_shown=bool(group_rows), is_first=group.key == next_key)
            header.place_at(grid_row)
            grid_row += 1
            for row in group_rows:
                line = self._rows[row_index]
                line.update(row, selected=row.id == self.selected_id)
                line.place_at(grid_row)
                grid_row += 1
                row_index += 1

        self.clear_all_button.configure(state="normal" if groups else "disabled")
        self.empty_label.configure(
            text="" if groups else "Queue is empty.\n\nAdd playlists from the TIDAL or Spotify tab."
        )
        self._set_notice(self.empty_label, not groups, grid_row, pady=30)
        self.overflow_label.configure(
            text=f"… and {hidden} more tracks not shown. Click a playlist to show it, "
            "or use “Clear finished” to shorten the list."
            if hidden > 0
            else ""
        )
        self._set_notice(self.overflow_label, hidden > 0, grid_row + 1, pady=8)

    def _new_header(self) -> _GroupHeaderWidgets:
        return _GroupHeaderWidgets(
            self.list_frame, self.toggle_group, self.move_group_to_front, self.remove_group
        )

    @staticmethod
    def _resize(pool: list, size: int, factory) -> None:
        while len(pool) < size:
            pool.append(factory())
        while len(pool) > size:
            pool.pop().destroy()

    @staticmethod
    def _set_notice(label, wanted: bool, row: int, *, pady: int) -> None:
        if wanted:
            label.grid(row=row, column=0, pady=pady)
        elif label.winfo_ismapped():
            label.grid_remove()

    def destroy(self) -> None:
        try:
            self._unsubscribe()
        except Exception:
            pass
