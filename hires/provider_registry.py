"""Builds the four playlist providers from the running GUI.

Each entry is a factory, called whenever a tab needs the service, so a module
installed or a login made after start-up simply starts working. A factory
never raises: a missing module or login becomes an
:class:`~hires.playlist_providers.UnavailableProvider` whose hint the tab shows.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Callable, Dict

from .account_providers import (
    SOUNDCLOUD_MODULE_NAME,
    _credentials,
    loaded_module,
    module_installed,
    resolve_cookie_path,
)
from .models import AuthRequiredError
from .playlist_providers import (
    PlaylistProvider,
    SpotifyProvider,
    TidalProvider,
    UnavailableProvider,
)

ProviderFactory = Callable[[], PlaylistProvider]

TIDAL_MISSING = "TIDAL module not available yet. Install it under Settings > TIDAL, or wait until it has loaded."
TIDAL_SIGN_IN = "Sign in to TIDAL on the Accounts tab first."
SOUNDCLOUD_MISSING = "SoundCloud module not installed (Settings > SoundCloud)."
YOUTUBE_MISSING = "YouTube module not installed (Settings > YouTube)."
YTDLP_MISSING = "yt-dlp is missing, so YouTube cannot be read."


def make_tidal_provider(gui: Any) -> ProviderFactory:
    def factory() -> PlaylistProvider:
        from .tidal_library import TidalLibrary

        try:
            library = TidalLibrary.from_orpheus(getattr(gui, "orpheus_instance", None))
        except AuthRequiredError:
            return UnavailableProvider("TIDAL", TIDAL_SIGN_IN)
        except Exception:
            return UnavailableProvider("TIDAL", TIDAL_MISSING)
        if library is None:
            return UnavailableProvider("TIDAL", TIDAL_MISSING)
        return TidalProvider(library)

    return factory


def make_spotify_provider(gui: Any) -> ProviderFactory:
    from .integration import make_spotify_source_provider

    source_provider = make_spotify_source_provider(gui)

    def factory() -> PlaylistProvider:
        try:
            return SpotifyProvider(source_provider())
        except Exception as exc:
            return UnavailableProvider("Spotify", f"Spotify cannot be read: {type(exc).__name__}")

    return factory


def make_soundcloud_provider(gui: Any) -> ProviderFactory:
    def factory() -> PlaylistProvider:
        from .soundcloud_source import SoundCloudProvider

        if module_installed(gui, SOUNDCLOUD_MODULE_NAME) is False:
            return UnavailableProvider("SoundCloud", SOUNDCLOUD_MISSING)
        module = loaded_module(gui, SOUNDCLOUD_MODULE_NAME)
        session = getattr(module, "websession", None) if module is not None else None
        if session is None:
            return UnavailableProvider("SoundCloud", SOUNDCLOUD_MISSING)
        # A fresh client with the *current* token: the module keeps the one it
        # was loaded with (same reasoning as the Accounts tab).
        token = str(_credentials(gui, "SoundCloud").get("web_access_token") or "").strip()
        try:
            api = type(session)(token, getattr(module, "exception", Exception))
        except Exception:
            api = session
        return SoundCloudProvider(api)

    return factory


def make_youtube_provider(gui: Any) -> ProviderFactory:
    def factory() -> PlaylistProvider:
        from .youtube_source import YouTubeProvider, make_ytdlp_extractor

        if module_installed(gui, "youtube") is False:
            return UnavailableProvider("YouTube", YOUTUBE_MISSING)
        if importlib.util.find_spec("yt_dlp") is None:
            return UnavailableProvider("YouTube", YTDLP_MISSING)
        raw = str(_credentials(gui, "YouTube").get("cookies_path") or "")
        path = resolve_cookie_path(gui, raw)
        has_cookies = os.path.isfile(path)
        return YouTubeProvider(
            make_ytdlp_extractor(path if has_cookies else ""), has_cookies=has_cookies
        )

    return factory


def make_playlist_providers(gui: Any) -> Dict[str, ProviderFactory]:
    """Service name -> provider factory, for the Playlists and Port tabs."""
    return {
        "Spotify": make_spotify_provider(gui),
        "TIDAL": make_tidal_provider(gui),
        "YouTube": make_youtube_provider(gui),
        "SoundCloud": make_soundcloud_provider(gui),
    }
