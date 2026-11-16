"""UI logic of the Playlists tab, without tkinter.

One :class:`~hires.controllers.TidalBrowserController` per service does the
listing and queueing -- it was written for TIDAL and takes the service name
since the Playlists tab replaced the TIDAL one. This class adds what is new:
picking the service, reading what it can do (and why not), and opening a
playlist by link.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Sequence

from .controllers import TidalBrowserController, UiDispatcher, _describe, run_in_background
from .models import HiresError, PlaylistRef, QueueItem, TrackRef
from .playlist_providers import SERVICES, ProviderStatus
from .quality import HIRES

ProviderFactory = Callable[[], Any]


def provider_status(provider: Any, service: str) -> ProviderStatus:
    """A provider's status; duck-typed providers without one count as ready."""
    if provider is None:
        return ProviderStatus(service, available=False, can_search=False, hint=f"{service} is not available.")
    reader = getattr(provider, "status", None)
    if not callable(reader):
        return ProviderStatus(service, can_list_own=True)
    try:
        return reader()
    except Exception as exc:
        return ProviderStatus(service, available=False, can_search=False, hint=_describe(exc))


class PlaylistsController:
    """Lists, opens and queues playlists of the four services."""

    def __init__(
        self,
        providers: Dict[str, ProviderFactory],
        queue,
        *,
        dispatch: Optional[UiDispatcher] = None,
        output_provider: Optional[Callable[[], Optional[str]]] = None,
        quality_provider: Optional[Callable[[], Optional[str]]] = None,
    ):
        """
        Args:
            providers: service name -> factory returning its provider. Only
                the services given here are offered.
            quality_provider: tier for TIDAL items, hi-res by default -- the
                suite pins TIDAL to hi-res whatever the global setting says.
                Other services leave the tier to the GUI.
        """
        self.providers = dict(providers)
        self.queue = queue
        self.dispatch = dispatch or UiDispatcher()
        tidal_quality = quality_provider or (lambda: HIRES)
        self.services: List[str] = [s for s in SERVICES if s in self.providers] + [
            s for s in self.providers if s not in SERVICES
        ]
        self.browsers: Dict[str, TidalBrowserController] = {
            service: TidalBrowserController(
                factory,
                queue,
                dispatch=self.dispatch,
                quality_provider=tidal_quality if service == "TIDAL" else (lambda: None),
                output_provider=output_provider,
                service=service,
            )
            for service, factory in self.providers.items()
        }

    # -- what the service can do --------------------------------------------
    def status(self, service: str) -> ProviderStatus:
        """Blocking: may load a module. Use :meth:`read_status` from the UI."""
        factory = self.providers.get(service)
        if factory is None:
            return ProviderStatus(service, available=False, can_search=False, hint=f"{service} is not offered.")
        try:
            provider = factory()
        except Exception as exc:
            return ProviderStatus(service, available=False, can_search=False, hint=_describe(exc))
        return provider_status(provider, service)

    def read_status(self, service: str, on_done: Callable[[ProviderStatus], None]) -> threading.Thread:
        def work():
            self.dispatch(on_done, self.status(service))

        return run_in_background(work, name="hires-playlists-status")

    # -- reading ------------------------------------------------------------
    def load_playlists(self, service: str, on_done, on_error, *, include_favorites: bool = True):
        return self.browsers[service].load_playlists(
            on_done, on_error, include_favorites=include_favorites
        )

    def load_tracks(self, service: str, playlist: PlaylistRef, on_done, on_error):
        return self.browsers[service].load_tracks(playlist, on_done, on_error)

    def open_link(
        self,
        service: str,
        url: str,
        on_done: Callable[[PlaylistRef], None],
        on_error: Callable[[str], None],
    ) -> threading.Thread:
        """Resolve a pasted playlist link to a playlist of ``service``."""
        url = (url or "").strip()

        def work():
            try:
                if not url:
                    raise HiresError(f"Paste a {service} playlist link first.")
                provider = self.providers[service]()
                owns = getattr(provider, "owns_url", None)
                if callable(owns) and not owns(url):
                    raise HiresError(f"That is not a {service} link.")
                playlist = provider.get_playlist(url)
                if not playlist.url:
                    playlist.url = url
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            self.dispatch(on_done, playlist)

        return run_in_background(work, name="hires-playlists-link")

    # -- queueing -----------------------------------------------------------
    def enqueue_playlist(self, service: str, playlist: PlaylistRef) -> QueueItem:
        """The whole playlist as one item; OrpheusDL expands it (folder, m3u)."""
        return self.browsers[service].enqueue_playlist_as_one(playlist)

    def enqueue_many(self, service: str, playlists: Sequence[PlaylistRef]) -> List[QueueItem]:
        return self.browsers[service].enqueue_many(playlists)

    def enqueue_tracks(
        self, service: str, playlist: PlaylistRef, tracks: Sequence[TrackRef]
    ) -> List[QueueItem]:
        """Some tracks of a playlist, as one group in the queue."""
        return self.browsers[service].enqueue_tracks(playlist, tracks)
