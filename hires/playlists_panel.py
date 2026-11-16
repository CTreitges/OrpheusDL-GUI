"""The Playlists tab: browse playlists of Spotify, TIDAL, YouTube and SoundCloud.

Widgets only; the decisions live in :mod:`hires.playlists_controller`.
Replaces the former "TIDAL Playlists" tab -- with TIDAL picked it behaves as
that tab did (folders, favourites, ticking several), and every service adds a
link field and a track list for queueing only part of a playlist.
"""

from __future__ import annotations

from typing import List, Optional

import customtkinter
import tkinter

from .gui_panel import PlaylistList, _count_label, _selection_label
from .help_widget import attach_tooltip
from .models import FolderRef, PlaylistRef, TrackRef
from .playlist_providers import ProviderStatus
from .playlists_controller import PlaylistsController
from .ui_style import (
    ERROR,
    PRIMARY_ACTION,
    SECONDARY_TEXT,
    SUCCESS,
    SURFACE,
    WARNING,
    WHITE_TEXT,
)
from .ui_style import button as _button


def _duration(seconds: Optional[int]) -> str:
    if not seconds:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def track_line(index: int, track: TrackRef) -> str:
    """One row of the track list: number, artist - title, length."""
    length = _duration(track.duration_sec)
    return f"{index:>4}   {track.display_name}" + (f"   {length}" if length else "")


class PlaylistsTab:
    """Pick a service, list or open playlists, queue them whole or in part."""

    def __init__(self, parent, controller: PlaylistsController, service: str = ""):
        self.controller = controller
        self.service = service or (controller.services[0] if controller.services else "TIDAL")
        self.tracks: List[TrackRef] = []
        self.tracks_of: Optional[PlaylistRef] = None

        self.frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=9, pady=10)

        # -- service and own playlists --------------------------------------
        bar = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 6))

        self.service_picker = customtkinter.CTkSegmentedButton(
            bar, values=list(controller.services), command=self.set_service
        )
        self.service_picker.set(self.service)
        self.service_picker.pack(side="left", padx=(0, 10))
        attach_tooltip(self.service_picker, "hires.playlists.service")

        self.load_button = _button(bar, "⟳  My playlists", self.load, width=130, fg=PRIMARY_ACTION)
        self.load_button.pack(side="left", padx=(0, 6))
        attach_tooltip(self.load_button, "hires.playlists.load")

        self.favorites_var = tkinter.BooleanVar(value=True)
        self.favorites = customtkinter.CTkCheckBox(
            bar,
            text="Include favorites",
            variable=self.favorites_var,
            text_color=SECONDARY_TEXT,
            checkbox_width=18,
            checkbox_height=18,
        )
        attach_tooltip(self.favorites, "hires.playlists.include_favorites")

        self.status_label = customtkinter.CTkLabel(bar, text="", text_color=SECONDARY_TEXT)
        self.status_label.pack(side="right")

        # -- link -----------------------------------------------------------
        link = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        link.pack(fill="x", pady=(0, 4))
        self.url_entry = customtkinter.CTkEntry(link, height=30)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.url_entry.bind("<Return>", lambda _e: self.open_link())
        attach_tooltip(self.url_entry, "hires.playlists.link")
        open_button = _button(link, "Open link", self.open_link, width=100)
        open_button.pack(side="left")
        attach_tooltip(open_button, "hires.playlists.open_link")

        # N3: what the service cannot do right now, and how to change that.
        self.hint_label = customtkinter.CTkLabel(
            self.frame, text="", text_color=WARNING, anchor="w", justify="left", wraplength=900
        )
        self.hint_label.pack(fill="x", pady=(0, 4))

        # -- playlists | tracks ---------------------------------------------
        body = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1, uniform="cols")
        body.grid_columnconfigure(1, weight=1, uniform="cols")
        body.grid_rowconfigure(0, weight=1)

        self.list = PlaylistList(body, self._on_select, on_check=self._on_check)
        self.list.frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        tracks_box = customtkinter.CTkFrame(body, fg_color=SURFACE)
        tracks_box.grid(row=0, column=1, sticky="nsew")
        self.tracks_title = customtkinter.CTkLabel(
            tracks_box, text="Tracks", text_color=SECONDARY_TEXT, anchor="w"
        )
        self.tracks_title.pack(fill="x", padx=8, pady=(4, 0))
        # A plain Listbox: 500 rows build in milliseconds, and its extended
        # selection (Shift/Ctrl+click) is exactly "pick some tracks".
        self.track_list = tkinter.Listbox(
            tracks_box,
            selectmode="extended",
            activestyle="none",
            bg=SURFACE,
            fg=WHITE_TEXT,
            selectbackground=PRIMARY_ACTION,
            selectforeground=WHITE_TEXT,
            highlightthickness=0,
            borderwidth=0,
            exportselection=False,
        )
        scroll = customtkinter.CTkScrollbar(tracks_box, command=self.track_list.yview)
        self.track_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.track_list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(2, 6))
        self.track_list.bind("<<ListboxSelect>>", lambda _e: self._on_track_pick())
        attach_tooltip(self.track_list, "hires.playlists.tracks")

        # -- actions --------------------------------------------------------
        actions = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        actions.pack(fill="x", pady=(8, 0))
        self.select_all_button = _button(actions, "Select all", self._toggle_all, width=90, state="disabled")
        self.select_all_button.pack(side="left", padx=(0, 6))
        attach_tooltip(self.select_all_button, "hires.playlists.select_all")
        self.download_button = _button(
            actions, "⬇  Add to queue", self.enqueue_selected, width=140, state="disabled"
        )
        self.download_button.pack(side="left", padx=(0, 6))
        attach_tooltip(self.download_button, "hires.playlists.add_to_queue")
        self.tracks_button = _button(
            actions, "⬇  Queue picked tracks", self.enqueue_picked_tracks, width=170, state="disabled"
        )
        self.tracks_button.pack(side="left")
        attach_tooltip(self.tracks_button, "hires.playlists.queue_tracks")

        self.set_service(self.service)

    # -- service ------------------------------------------------------------
    def set_service(self, service: str) -> None:
        self.service = service
        self.service_picker.set(service)
        if service == "TIDAL":
            self.favorites.pack(side="left", padx=(0, 12), after=self.load_button)
        else:
            self.favorites.pack_forget()
        self.url_entry.configure(placeholder_text=f"Paste a {service} playlist link")
        self._show_tracks(None, [])
        self.list.set_playlists([])
        self.list.set_message(
            f"Click “My playlists” to show your {service} playlists,\n"
            "or paste a playlist link above."
        )
        self.status_label.configure(text="", text_color=SECONDARY_TEXT)
        self.download_button.configure(state="disabled", text="⬇  Add to queue")
        self.hint_label.configure(text="")
        self.controller.read_status(service, self._on_status)

    def _on_status(self, status: ProviderStatus) -> None:
        if status.service != self.service:
            return  # the user has moved on to another service
        self.hint_label.configure(text=status.hint or "")
        self.load_button.configure(state="normal" if status.can_list_own else "disabled")

    # -- selection ----------------------------------------------------------
    def _on_select(self, playlist: PlaylistRef) -> None:
        """A row was highlighted: show its tracks. Ticks take precedence for queueing."""
        if not self.list.checked:
            self.download_button.configure(state="normal")
            self.status_label.configure(text=f"Selected: {playlist.name}", text_color=SECONDARY_TEXT)
        self.load_tracks(playlist)

    def _on_check(self) -> None:
        checked = self.list.checked
        if checked:
            self.download_button.configure(text=f"⬇  Queue {len(checked)}", state="normal")
            self.status_label.configure(text=_selection_label(checked), text_color=SECONDARY_TEXT)
        else:
            self.download_button.configure(
                text="⬇  Add to queue", state="normal" if self.list.selected else "disabled"
            )
        self.select_all_button.configure(
            text="Select none" if checked else "Select all",
            state="normal" if self.list.playlists else "disabled",
        )

    def _toggle_all(self) -> None:
        self.list.check_all(not self.list.checked)

    # -- loading ------------------------------------------------------------
    def load(self) -> None:
        service = self.service
        self.status_label.configure(text="Loading…", text_color=SECONDARY_TEXT)
        self.list.set_message(f"Loading your {service} playlists…")
        self.download_button.configure(state="disabled")
        self.controller.load_playlists(
            service,
            lambda root: self._on_loaded(service, root),
            lambda message: self._on_error(service, message),
            include_favorites=self.favorites_var.get(),
        )

    def _on_loaded(self, service: str, root: FolderRef) -> None:
        if service != self.service:
            return
        self.list.set_root(root)
        summary = _count_label(root.total_playlists, "playlist")
        if root.folders:
            summary = f"{_count_label(len(root.folders), 'folder')} · {summary}"
        self.status_label.configure(text=summary, text_color=SECONDARY_TEXT)
        self._on_check()

    def _on_error(self, service: str, message: str) -> None:
        if service != self.service:
            return
        self.list.set_message(message, color=ERROR)
        self.status_label.configure(text="Failed", text_color=ERROR)

    def open_link(self) -> None:
        service = self.service
        self.status_label.configure(text="Opening…", text_color=SECONDARY_TEXT)
        self.controller.open_link(
            service,
            self.url_entry.get(),
            lambda playlist: self._on_link(service, playlist),
            lambda message: self._on_link_error(service, message),
        )

    def _on_link(self, service: str, playlist: PlaylistRef) -> None:
        if service != self.service:
            return
        self.list.set_playlists([playlist])
        self.list.selected = playlist
        self._on_select(playlist)
        self._on_check()

    def _on_link_error(self, service: str, message: str) -> None:
        if service == self.service:
            self.status_label.configure(text=message, text_color=ERROR)

    def load_tracks(self, playlist: PlaylistRef) -> None:
        service = self.service
        self.tracks_of = playlist
        self._show_tracks(playlist, [], note="Loading tracks…")
        self.controller.load_tracks(
            service,
            playlist,
            lambda tracks: self._on_tracks(service, playlist, tracks),
            lambda message: self._on_tracks_error(service, playlist, message),
        )

    def _on_tracks(self, service: str, playlist: PlaylistRef, tracks: List[TrackRef]) -> None:
        if service != self.service or playlist is not self.tracks_of:
            return  # a newer click owns the pane
        self._show_tracks(playlist, tracks)

    def _on_tracks_error(self, service: str, playlist: PlaylistRef, message: str) -> None:
        if service == self.service and playlist is self.tracks_of:
            self._show_tracks(playlist, [], note=message)

    def _show_tracks(self, playlist: Optional[PlaylistRef], tracks: List[TrackRef], note: str = "") -> None:
        """Fill the track pane in one go (one insert call, not one per row)."""
        self.tracks = list(tracks)
        self.track_list.delete(0, "end")
        if self.tracks:
            self.track_list.insert("end", *(track_line(i, t) for i, t in enumerate(self.tracks, 1)))
        if playlist is None:
            self.tracks_title.configure(text="Tracks")
        elif note:
            self.tracks_title.configure(text=f"{playlist.name} — {note}")
        else:
            self.tracks_title.configure(
                text=f"{playlist.name} — {_count_label(len(self.tracks), 'track')}"
            )
        self._on_track_pick()

    def _on_track_pick(self) -> None:
        picked = len(self.track_list.curselection())
        self.tracks_button.configure(
            text=f"⬇  Queue {picked} picked" if picked else "⬇  Queue picked tracks",
            state="normal" if picked else "disabled",
        )

    @property
    def picked_tracks(self) -> List[TrackRef]:
        return [self.tracks[i] for i in self.track_list.curselection() if i < len(self.tracks)]

    # -- queueing -----------------------------------------------------------
    def enqueue_selected(self) -> None:
        """Queue the ticked playlists, or the highlighted one if none are ticked."""
        checked = self.list.checked
        try:
            if checked:
                items = self.controller.enqueue_many(self.service, checked)
                self.list.check_all(False)
                self.status_label.configure(text=f"Queued {len(items)} playlists.", text_color=SUCCESS)
                return
            playlist = self.list.selected
            if playlist is None:
                return
            self.controller.enqueue_playlist(self.service, playlist)
        except Exception as exc:
            self.status_label.configure(text=str(exc), text_color=ERROR)
            return
        self.status_label.configure(text=f"Queued: {playlist.name}", text_color=SUCCESS)

    def enqueue_picked_tracks(self) -> None:
        tracks = self.picked_tracks
        if not tracks or self.tracks_of is None:
            return
        try:
            items = self.controller.enqueue_tracks(self.service, self.tracks_of, tracks)
        except Exception as exc:
            self.status_label.configure(text=str(exc), text_color=ERROR)
            return
        self.track_list.selection_clear(0, "end")
        self._on_track_pick()
        self.status_label.configure(
            text=f"Queued {_count_label(len(items), 'track')} of {self.tracks_of.name}.",
            text_color=SUCCESS,
        )
