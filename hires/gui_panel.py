"""Shared widgets of the hi-res tabs, the Accounts tab and the wiring.

The Queue, Playlists and Port tabs live in ``queue_panel``, ``playlists_panel``
and ``port_panel``; this module holds what they share (playlist list, review
dialog) and :func:`build_tabs`.

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
    AccountsController,
    QueueController,
    ReviewRow,
    UiDispatcher,
)
from .models import (
    AccountState,
    FolderRef,
    PlaylistRef,
    flat_root,
)

from .queue_panel import MAX_RENDERED_ROWS, STATUS_COLORS, QueueTab  # noqa: F401  (re-exported)
from .ui_style import (  # noqa: F401  (re-exported for older imports)
    BORDER,
    BUTTON_BG,
    CONTAINER,
    ERROR,
    GRAY_TEXT,
    HOVER,
    PRIMARY_ACTION,
    ROW_HOVER,
    SECONDARY_TEXT,
    SUCCESS,
    SURFACE,
    WARNING,
    WHITE_TEXT,
)
from .ui_style import button as _button  # noqa: E402
from .account_providers import SERVICE_INFO, SETTINGS_ONLY_SERVICES
from .help_widget import attach_tooltip


def _count_label(count: int, noun: str) -> str:
    """``3 playlists`` / ``1 playlist`` -- these strings are read constantly."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _selection_label(playlists) -> str:
    """What is ticked, and how much music that is."""
    tracks = sum(p.track_count or 0 for p in playlists)
    label = _count_label(len(playlists), "playlist")
    return f"{label} · {_count_label(tracks, 'track')}" if tracks else label


def _distinct_label(label: str, taken) -> str:
    """``label``, numbered if something already claimed it.

    Dropdown entries double as dictionary keys, so two options that read alike
    must not be alike.
    """
    if label not in taken:
        return label
    suffix = 2
    while f"{label}  ·  {suffix}" in taken:
        suffix += 1
    return f"{label}  ·  {suffix}"


# ---------------------------------------------------------------------------
# Shared playlist list widget
# ---------------------------------------------------------------------------

class PlaylistList:
    """A scrollable list of playlists, optionally grouped into folders.

    Two selections coexist on purpose. Clicking a row *highlights* it, which is
    what drives the existing single-playlist actions. The checkboxes are a
    separate, additive selection used for queueing several at once -- ticking a
    folder ticks everything in it. Neither disturbs the other.
    """

    def __init__(
        self,
        parent,
        on_select: Callable[[PlaylistRef], None],
        on_check: Optional[Callable[[], None]] = None,
    ):
        self.on_select = on_select
        self.on_check = on_check or (lambda: None)
        self.selected: Optional[PlaylistRef] = None
        self.playlists: List[PlaylistRef] = []
        self.root: Optional[FolderRef] = None

        self._checked: Dict[str, PlaylistRef] = {}
        self._rows: Dict[str, Any] = {}
        self._vars: Dict[str, Any] = {}
        self._folder_vars: List[tuple] = []

        self.frame = customtkinter.CTkScrollableFrame(
            parent, fg_color=SURFACE, border_color=BORDER, border_width=1
        )

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    # -- state --------------------------------------------------------------
    @property
    def checked(self) -> List[PlaylistRef]:
        """Ticked playlists, in the order they are shown."""
        order = {p.id: i for i, p in enumerate(self.playlists)}
        return sorted(self._checked.values(), key=lambda p: order.get(p.id, 0))

    @property
    def has_folders(self) -> bool:
        return bool(self.root and self.root.folders)

    def _clear(self) -> None:
        for child in self.frame.winfo_children():
            child.destroy()
        self._rows.clear()
        self._vars.clear()
        self._folder_vars.clear()
        self._checked.clear()

    def _sync_folder_boxes(self) -> None:
        """A folder reads as ticked exactly when everything in it is."""
        for folder, var in self._folder_vars:
            playlists = folder.all_playlists()
            try:
                var.set(bool(playlists) and all(p.id in self._checked for p in playlists))
            except Exception:
                pass  # widget already gone

    def set_message(self, text: str, color: str = GRAY_TEXT) -> None:
        self._clear()
        self.playlists = []
        self.root = None
        customtkinter.CTkLabel(
            self.frame, text=text, text_color=color, justify="center", wraplength=520
        ).pack(pady=30)

    # -- building -----------------------------------------------------------
    def set_playlists(self, playlists: List[PlaylistRef]) -> None:
        """Show a flat list. Kept so callers without folders stay unchanged."""
        self.set_root(flat_root(playlists))

    def set_root(self, root: FolderRef) -> None:
        self._clear()
        self.root = root
        self.playlists = root.all_playlists()
        self.selected = None

        if not self.playlists:
            self.set_message("No playlists found.")
            return

        for folder in root.folders:
            self._build_folder(folder, depth=0)
        for playlist in root.playlists:
            self._build_playlist(playlist, depth=0)

        self.on_check()

    def _build_folder(self, folder: FolderRef, depth: int) -> None:
        header = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        header.pack(fill="x", padx=(4 + depth * 16, 4), pady=(6, 1))

        var = tkinter.BooleanVar(value=False)
        customtkinter.CTkCheckBox(
            header,
            text="",
            variable=var,
            width=18,
            checkbox_width=17,
            checkbox_height=17,
            command=lambda f=folder, v=var: self._toggle_folder(f, v),
        ).pack(side="left")
        # Kept so ticking the last playlist inside a folder also ticks the
        # folder itself, rather than leaving the two disagreeing on screen.
        self._folder_vars.append((folder, var))

        customtkinter.CTkLabel(
            header,
            text=f"📁  {folder.name}",
            text_color=WHITE_TEXT,
            anchor="w",
        ).pack(side="left", padx=(4, 8))

        customtkinter.CTkLabel(
            header,
            text=_count_label(folder.total_playlists, "playlist"),
            text_color=GRAY_TEXT,
            font=("", 11),
        ).pack(side="right", padx=8)

        for child in folder.folders:
            self._build_folder(child, depth + 1)
        for playlist in folder.playlists:
            self._build_playlist(playlist, depth + 1)

    def _build_playlist(self, playlist: PlaylistRef, depth: int) -> None:
        row = customtkinter.CTkFrame(self.frame, fg_color="transparent", corner_radius=4)
        row.pack(fill="x", padx=(4 + depth * 16, 4), pady=1)

        var = tkinter.BooleanVar(value=playlist.id in self._checked)
        customtkinter.CTkCheckBox(
            row,
            text="",
            variable=var,
            width=18,
            checkbox_width=17,
            checkbox_height=17,
            command=lambda p=playlist, v=var: self._toggle_playlist(p, v.get()),
        ).pack(side="left", padx=(6, 0))

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

        self._rows[playlist.id] = row
        self._vars[playlist.id] = var

        for widget in (row, name):
            widget.bind("<Button-1>", lambda _e, p=playlist, r=row: self._select(p, r))

    # -- checking -----------------------------------------------------------
    def _apply(self, playlists, checked: bool) -> None:
        for playlist in playlists:
            if checked:
                self._checked[playlist.id] = playlist
            else:
                self._checked.pop(playlist.id, None)
            box = self._vars.get(playlist.id)
            if box is not None:
                box.set(checked)

    def _toggle_playlist(self, playlist: PlaylistRef, checked: bool) -> None:
        self._apply([playlist], checked)
        self._sync_folder_boxes()
        self.on_check()

    def _toggle_folder(self, folder: FolderRef, var) -> None:
        """A folder's box drives every playlist under it."""
        self._apply(folder.all_playlists(), bool(var.get()))
        self._sync_folder_boxes()
        self.on_check()

    def check_all(self, checked: bool = True) -> None:
        self._apply(self.playlists, checked)
        self._sync_folder_boxes()
        self.on_check()

    # -- highlighting -------------------------------------------------------
    def _select(self, playlist: PlaylistRef, row) -> None:
        self.selected = playlist
        for other in self._rows.values():
            try:
                other.configure(fg_color="transparent")
            except Exception:
                pass
        try:
            row.configure(fg_color=ROW_HOVER)
        except Exception:
            pass
        self.on_select(playlist)


# ---------------------------------------------------------------------------
# Accounts tab
# ---------------------------------------------------------------------------

#: Dot colour per account state.
ACCOUNT_COLORS = {
    AccountState.SIGNED_IN.value: SUCCESS,
    AccountState.SIGNED_OUT.value: WARNING,
    AccountState.NEEDS_SETUP.value: WARNING,
    AccountState.UNAVAILABLE.value: GRAY_TEXT,
}

#: Short state label next to the service name.
ACCOUNT_LABELS = {
    AccountState.SIGNED_IN.value: "Signed in",
    AccountState.SIGNED_OUT.value: "Not signed in",
    AccountState.NEEDS_SETUP.value: "Setup needed",
    AccountState.UNAVAILABLE.value: "Unavailable",
}


class AccountsTab:
    """Sign in to TIDAL and Spotify up front, before queueing anything.

    Nothing here gates the other tabs. TIDAL deliberately allows guest
    browsing, and public Spotify links need no login at all -- this tab only
    moves the sign-in to a place the user can find before the first download
    surprises them with a browser window.
    """

    def __init__(
        self,
        parent,
        controller: AccountsController,
        open_settings: Optional[Callable[[str], Any]] = None,
    ):
        """
        Args:
            open_settings: switches the window to Settings > <service>. The
                button of SoundCloud and YouTube uses it: neither has a sign-in
                flow, both need something entered in their settings.
        """
        self.controller = controller
        self.open_settings = open_settings
        self._cards: Dict[str, Dict[str, Any]] = {}
        # Sign-in outcomes, shown instead of the generic description until the
        # user asks for a fresh read.
        self._notes: Dict[str, tuple] = {}
        #: Line about the automatic re-checks, shown instead of the summary.
        self._auto_note = ""

        self.frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=9, pady=10)

        customtkinter.CTkLabel(
            self.frame,
            text=(
                "Sign in here before you queue your first playlist — otherwise the "
                "first download is what opens a browser window."
            ),
            text_color=SECONDARY_TEXT,
            anchor="w",
            justify="left",
            wraplength=720,
        ).pack(fill="x", pady=(0, 10))

        self.cards_frame = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        self.cards_frame.pack(fill="x")

        for service in self.controller.SERVICES:
            self._build_card(service)

        bar = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        bar.pack(fill="x", pady=(10, 0))
        self.refresh_button = _button(bar, "⟳  Refresh", self.manual_refresh, width=110)
        self.refresh_button.pack(side="left")
        attach_tooltip(self.refresh_button, "hires.accounts.refresh")

        self.summary_label = customtkinter.CTkLabel(
            bar, text="", text_color=SECONDARY_TEXT, anchor="e"
        )
        self.summary_label.pack(side="right")

        # The re-check thread must not outlive the tab.
        self.frame.bind("<Destroy>", self._on_destroy, add="+")
        self.start_auto_refresh()

    # -- construction -------------------------------------------------------
    def _build_card(self, service: str) -> None:
        card = customtkinter.CTkFrame(self.cards_frame, fg_color=CONTAINER, corner_radius=6)
        card.pack(fill="x", pady=(0, 8))

        header = customtkinter.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(10, 2))

        dot = customtkinter.CTkLabel(header, text="●", width=16, text_color=GRAY_TEXT)
        dot.pack(side="left")

        customtkinter.CTkLabel(
            header, text=service, text_color=WHITE_TEXT, anchor="w"
        ).pack(side="left", padx=(4, 10))

        state_label = customtkinter.CTkLabel(
            header, text="", text_color=SECONDARY_TEXT, font=("", 11), anchor="w"
        )
        state_label.pack(side="left", fill="x", expand=True)

        settings_only = service in SETTINGS_ONLY_SERVICES
        if settings_only:
            button = _button(
                header, "Open settings", lambda s=service: self.go_to_settings(s), width=110
            )
            attach_tooltip(button, "hires.accounts.open_settings")
        else:
            button = _button(header, "Sign in", lambda s=service: self.sign_in(s), width=110)
            attach_tooltip(button, "hires.accounts.sign_in")
        button.pack(side="right")

        detail = customtkinter.CTkLabel(
            card,
            text="",
            text_color=GRAY_TEXT,
            font=("", 11),
            anchor="w",
            justify="left",
            wraplength=700,
        )
        detail.pack(fill="x", padx=12, pady=(0, 2))

        # What the service needs and what it gives -- the part that does not
        # change with the status, so it is set once.
        info = customtkinter.CTkLabel(
            card,
            text=SERVICE_INFO.get(service, ""),
            text_color=SECONDARY_TEXT,
            font=("", 11),
            anchor="w",
            justify="left",
            wraplength=700,
        )
        info.pack(fill="x", padx=12, pady=(0, 10))

        self._cards[service] = {
            "dot": dot,
            "state": state_label,
            "button": button,
            "detail": detail,
            "info": info,
            "settings_only": settings_only,
        }

    # -- rendering ----------------------------------------------------------
    def manual_refresh(self) -> None:
        """The Refresh button: drop stale outcomes, re-read, restart the count."""
        self._notes.clear()
        self.start_auto_refresh()

    def start_auto_refresh(self) -> None:
        """Read now and re-check what is not signed in, up to the limit.

        Runs on the controller's worker; this tab only repaints from the
        callbacks, which the dispatcher delivers on the UI thread.
        """
        if not self._alive():
            return
        self._auto_note = ""
        self.repaint()
        self._set_reading(True)
        self.controller.start_auto_refresh(
            self._on_statuses,
            on_waiting=self._on_auto_waiting,
            on_checking=self._on_auto_checking,
            on_finished=self._on_auto_finished,
        )

    def _on_auto_waiting(self, attempt: int, limit: int, seconds: float) -> None:
        self._set_reading(False)
        self._show_auto_note(f"Not everything is signed in — checking again in {seconds:.0f} s ({attempt}/{limit})")

    def _on_auto_checking(self, attempt: int, limit: int) -> None:
        self._set_reading(True)
        self._show_auto_note(f"Checking… ({attempt}/{limit})")

    def _on_auto_finished(self, done: bool, rechecks: int) -> None:
        self._set_reading(False)
        if done or rechecks == 0:
            self._show_auto_note("")
        else:
            self._show_auto_note(f"Stopped after {rechecks} checks — press Refresh to check again.")

    def _show_auto_note(self, text: str) -> None:
        self._auto_note = text
        if self._alive():
            self.summary_label.configure(text=text or self.controller.summary())

    def go_to_settings(self, service: str) -> None:
        """SoundCloud/YouTube button: their credential lives in the settings."""
        opened = bool(self.open_settings and self.open_settings(service))
        if not opened:
            self._note(service, f"Open Settings > {service} to enter it.", SECONDARY_TEXT)

    def _on_destroy(self, _event=None) -> None:
        # Idempotent, so it does not matter which of the frame's inner
        # widgets reported the destroy.
        self.controller.stop_auto_refresh()

    def refresh(self) -> None:
        """Re-read every service's state on a worker, then repaint.

        Reading a status does HTTP (Spotify's ``/me``) and can load the TIDAL
        module, so it must not happen here -- doing it inline froze the window
        for the length of a round trip and made this button look broken.
        """
        if not self._alive():
            return

        self.repaint()  # show what we know while the read runs
        started = self.controller.refresh(self._on_statuses, self._on_read_failed)
        if started is not None:
            self._set_reading(True)

    def repaint(self) -> None:
        """Draw the last known state. Never touches the network."""
        if not self._alive():
            return
        for status in self.controller.known_statuses():
            self._render(status)
        self.summary_label.configure(text=self._auto_note or self.controller.summary())

    def _alive(self) -> bool:
        try:
            return bool(self.frame.winfo_exists())
        except Exception:
            return False

    def _set_reading(self, reading: bool) -> None:
        try:
            self.refresh_button.configure(
                text="Checking…" if reading else "⟳  Refresh",
                state="disabled" if reading else "normal",
            )
        except Exception:
            pass

    def _on_statuses(self, _statuses) -> None:
        self._set_reading(False)
        self.repaint()

    def _on_read_failed(self, message: str) -> None:
        if not self._alive():
            return
        self._set_reading(False)
        self.repaint()
        self.summary_label.configure(text=message)

    def _render(self, status) -> None:
        card = self._cards.get(status.service)
        if card is None:
            return

        state = status.state.value
        card["dot"].configure(text_color=ACCOUNT_COLORS.get(state, GRAY_TEXT))

        label = ACCOUNT_LABELS.get(state, state)
        if status.is_signed_in and status.account:
            label = f"{label} as {status.account}"
        card["state"].configure(text=label)

        note = self._notes.get(status.service)
        if note is not None:
            # A sign-in outcome outranks the generic description until the user
            # starts something new.
            card["detail"].configure(text=note[0], text_color=note[1])
        else:
            detail = status.detail
            if status.hint:
                detail = f"{detail}  {status.hint}" if detail else status.hint
            card["detail"].configure(text=detail, text_color=GRAY_TEXT)

        busy = self.controller.is_busy(status.service)
        if card.get("settings_only"):
            # Nothing to sign in to; the settings page is always the way.
            card["button"].configure(
                state="disabled" if status.state is AccountState.UNAVAILABLE else "normal"
            )
        elif busy:
            card["button"].configure(text="Waiting…", state="disabled")
        elif status.is_signed_in:
            # Re-authenticating is legitimate (expired session, wrong account),
            # so the button stays live rather than vanishing.
            card["button"].configure(text="Sign in again", state="normal")
        elif status.can_sign_in:
            card["button"].configure(text="Sign in", state="normal")
        else:
            card["button"].configure(text="Sign in", state="disabled")

    # -- actions ------------------------------------------------------------
    def sign_in(self, service: str) -> None:
        card = self._cards.get(service)
        if card is None:
            return
        card["button"].configure(text="Waiting…", state="disabled")
        card["detail"].configure(text="Starting sign-in…", text_color=SECONDARY_TEXT)

        self.controller.sign_in(
            service,
            on_done=lambda s=service: self._on_done(s),
            on_error=lambda message, s=service: self._on_error(s, message),
            on_status=lambda message, s=service: self._on_status(s, message),
        )
        # A refused click never reaches a callback, so restore the button from
        # what we already know rather than leaving it stuck on "Waiting…".
        if not self.controller.is_busy(service):
            self.repaint()

    def _note(self, service: str, message: str, color: str) -> None:
        """Show a message on a card and keep it there across repaints.

        Without this the refresh triggered by the same event would race in and
        overwrite the outcome the user needs to read -- an error message that
        vanishes half a second later is worse than none.
        """
        self._notes[service] = (message, color)
        card = self._cards.get(service)
        if card is not None:
            card["detail"].configure(text=message, text_color=color)

    def _on_status(self, service: str, message: str) -> None:
        self._note(service, message, SECONDARY_TEXT)

    def _on_done(self, service: str) -> None:
        self._note(service, f"Signed in to {service}.", SUCCESS)
        # Restart the schedule rather than a single refresh: a refresh is
        # refused while an automatic re-check reads, and that read may have
        # started before the sign-in finished (review part 1, finding 1).
        self.start_auto_refresh()

    def _on_error(self, service: str, message: str) -> None:
        self._note(service, message, ERROR)
        self.start_auto_refresh()


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
        #
        # The option label is the dict key, so two alternatives that read the
        # same -- the same recording on two albums scores identically often
        # enough -- would collapse into one entry and hand back the *other*
        # track's id. _distinct_label keeps every option addressable.
        options: List[str] = []
        mapping: Dict[str, Any] = {}

        def add(label: str, value: Any) -> None:
            label = _distinct_label(label, mapping)
            options.append(label)
            mapping[label] = value

        add(f"✓  {row.match_label}", True)
        for tidal_id, label, score in row.alternatives:
            add(f"↳  {label}  ({score * 100:.0f}%)", tidal_id)
        add("✗  Skip this track", False)

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
    playlist_providers: Dict[str, Callable[[], Any]],
    spotify_source_provider: Callable[[], Any],
    quality_provider: Optional[Callable[[], str]] = None,
    output_provider: Optional[Callable[[], Optional[str]]] = None,
    tidal_status_provider: Optional[Callable[[], Any]] = None,
    spotify_status_provider: Optional[Callable[[], Any]] = None,
    tidal_sign_in: Optional[Callable[[], Any]] = None,
    busy_provider: Optional[Callable[[], bool]] = None,
    soundcloud_status_provider: Optional[Callable[[], Any]] = None,
    youtube_status_provider: Optional[Callable[[], Any]] = None,
    open_settings: Optional[Callable[[str], Any]] = None,
    stop_active: Optional[Callable[[], Any]] = None,
    matcher_factory: Optional[Callable[[Any, str], Any]] = None,
) -> Dict[str, Any]:
    """Add the hi-res tabs to an existing CTkTabview.

    Returns the tab objects so gui.py can refresh them later. The Accounts tab
    is only built when both status providers are supplied -- without them there
    is nothing truthful to show.

    ``playlist_providers`` maps a service name to its provider factory (see
    :mod:`hires.provider_registry`); the Playlists and Port tabs offer exactly
    those services.
    """
    from .playlists_controller import PlaylistsController
    from .playlists_panel import PlaylistsTab
    from .port_controller import PortController
    from .port_panel import PortTab

    dispatch = UiDispatcher(app)

    port_controller = PortController(
        playlist_providers,
        queue,
        spotify_source_provider=spotify_source_provider,
        dispatch=dispatch,
        output_provider=output_provider,
        matcher_factory=matcher_factory,
    )

    tabs: Dict[str, Any] = {}

    # Accounts comes first: it is the step that belongs before the others.
    if tidal_status_provider is not None and spotify_status_provider is not None:
        tabs["accounts"] = AccountsTab(
            tabview.add("Accounts"),
            AccountsController(
                tidal_status_provider=tidal_status_provider,
                spotify_status_provider=spotify_status_provider,
                soundcloud_status_provider=soundcloud_status_provider,
                youtube_status_provider=youtube_status_provider,
                tidal_sign_in=tidal_sign_in,
                # Shared so a sign-in here is immediately visible on the Port
                # tab: one controller, one Spotify source, one set of tokens.
                spotify_controller=port_controller,
                dispatch=dispatch,
                busy_provider=busy_provider,
            ),
            open_settings=open_settings,
        )

    tabs["queue"] = QueueTab(
        tabview.add("Queue"),
        QueueController(queue, stop_active=stop_active),
        dispatch=dispatch,
    )

    tabs["playlists"] = PlaylistsTab(
        tabview.add("Playlists"),
        PlaylistsController(
            playlist_providers,
            queue,
            dispatch=dispatch,
            output_provider=output_provider,
            quality_provider=quality_provider,
        ),
        service="TIDAL" if "TIDAL" in playlist_providers else "",
    )

    tabs["port"] = PortTab(tabview.add("Port"), port_controller)

    return tabs
