"""One interface for every playlist service of the Playlists and Port tabs.

A *provider* is both ends of a port at once:

* **source** -- list your own playlists (where the login allows it), open one
  by link, read its tracks;
* **target** -- search tracks and look them up by ISRC, which is what
  :class:`hires.matcher.TrackMatcher` matches against.

The method names are the duck types the existing code already speaks --
``get_playlist`` / ``get_playlist_tracks`` like :class:`SpotifySource`,
``search_tracks`` / ``find_by_isrc`` like :class:`TidalLibrary` -- so the
converter and the matcher work with any provider unchanged.

Everything here only *reads*. No provider creates, changes or likes anything
in an account.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .models import (
    AuthRequiredError,
    FolderRef,
    HiresError,
    PlaylistRef,
    SourceUnavailableError,
    TrackRef,
    flat_root,
)
from .quality import HIRES

#: The four services of this suite, in the order the tabs show them.
SERVICES: Tuple[str, ...] = ("Spotify", "TIDAL", "YouTube", "SoundCloud")

#: Tier pinned on queue items found on a service; others leave it to the GUI.
TARGET_QUALITY = {"TIDAL": HIRES}

#: Seconds between two TIDAL catalogue calls: streamrip's default of 60
#: requests per minute (W/research-delays.md).
TIDAL_REQUEST_INTERVAL = 1.0


def port_directions(services: Sequence[str] = SERVICES) -> List[Tuple[str, str]]:
    """Every (source, target) pair with two different services -- 12 for 4."""
    return [(a, b) for a in services for b in services if a != b]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class ProviderStatus:
    """What a provider can do right now, and what to tell the user if not.

    ``hint`` is shown in the tab whenever something is missing, so an empty
    list never appears without an explanation.
    """

    service: str
    available: bool = True
    can_list_own: bool = False
    can_search: bool = True
    hint: str = ""


class PlaylistProvider:
    """Base class: the interface, with honest refusals as defaults."""

    service = ""
    #: Tier pinned on queue items found here; ``None`` leaves it to the GUI.
    quality: Optional[str] = None

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.service)

    # -- source -------------------------------------------------------------
    def list_folders(self, include_favorites: bool = True) -> FolderRef:
        return flat_root(self.list_playlists(include_favorites), platform=self.service)

    def list_playlists(self, include_favorites: bool = True) -> List[PlaylistRef]:
        raise AuthRequiredError(self.status().hint or f"{self.service}: own playlists not available")

    def get_playlist(self, playlist: Any) -> PlaylistRef:
        raise SourceUnavailableError(f"{self.service} cannot open playlists")

    def get_playlist_tracks(self, playlist: Any) -> List[TrackRef]:
        raise SourceUnavailableError(f"{self.service} cannot read playlists")

    def owns_url(self, url: str) -> bool:
        return False

    # -- target -------------------------------------------------------------
    def search_tracks(self, query: str, limit: int = 10) -> List[TrackRef]:
        raise SourceUnavailableError(self.status().hint or f"{self.service} cannot search")

    def find_by_isrc(self, isrc: str) -> List[TrackRef]:
        """Services without an ISRC lookup simply have no exact hits."""
        return []


class UnavailableProvider(PlaylistProvider):
    """Stands in for a service whose module or login is missing (N3).

    Every call fails with the same explanation the tab shows, instead of the
    tab having to special-case "no provider".
    """

    def __init__(self, service: str, hint: str):
        self.service = service
        self.hint = hint

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            self.service, available=False, can_list_own=False, can_search=False, hint=self.hint
        )

    def _refuse(self, *_args, **_kwargs):
        raise HiresError(self.hint)

    list_playlists = list_folders = get_playlist = get_playlist_tracks = _refuse
    search_tracks = _refuse


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class Pacer:
    """Keeps at least ``interval`` seconds between two requests.

    The Port tab sends one search per track, which is exactly the pattern
    that gets an IP or an account blocked. Clock and sleep are injectable so
    tests never really wait.
    """

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.interval = max(0.0, float(interval))
        self._clock = clock
        self._sleep = sleep
        self._last: Optional[float] = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self.interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last = now


#: Bracketed or dashed video noise that is not part of a song title.
_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:official\s*)?(?:hd\s*|hq\s*|4k\s*)?"
    r"(?:music\s*video|lyric\s*video|lyrics?|video|audio|visuali[sz]er|clip(?:\s*officiel)?|"
    r"official|hd|hq|4k|explicit|free\s*download)\s*(?:hd|hq|4k)?\s*[\)\]]",
    re.IGNORECASE,
)
_DASH = re.compile(r"\s+[-–—:]\s+")
#: Channel endings that are not part of the artist's name: "Daft Punk - Topic",
#: "MartinGarrixVEVO", "BASTILLEvideos", "Dua Lipa Official", "Avicii Music".
_CHANNEL_SUFFIX = re.compile(
    r"(?P<sep>\s*-\s*|\s+|)(?P<word>topic|vevo|official|officiel|videos?|music|tv)\s*$",
    re.IGNORECASE,
)
#: "MartinGarrix" -> "Martin Garrix"; only after a glued suffix ("…VEVO") and
#: only after a word of 3+ letters and before a capitalised word, so "McFly",
#: "deadmau5" or "BASTILLE" stay as they are.
_CAMEL = re.compile(r"(?:(?<=[a-z]{3})|(?<=[A-Z][a-z]{2}))(?=[A-Z][a-z])")
#: What may never be left over: "The Music" is a band, not "The".
_TOO_SHORT = {"", "the", "a", "an", "dj", "mc"}


#: Separators between several artists in an upload title ("A, B", "A & B",
#: "A x B", "A feat. B").
_ARTIST_SEPARATORS = re.compile(
    r"\s*,\s*|\s+&\s+|\s+x\s+|\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE
)


def split_artists(text: str) -> List[str]:
    return [a.strip() for a in _ARTIST_SEPARATORS.split(text or "") if a and a.strip()]


def clean_channel(name: str) -> str:
    """The artist behind a channel name.

    "Daft Punk - Topic" -> "Daft Punk", "BASTILLEvideos" -> "BASTILLE",
    "AviciiOfficialVEVO" -> "Avicii", "MartinGarrixVEVO" -> "Martin Garrix".
    Endings are stripped one at a time; a name that would shrink to nothing
    or to an article ("The Music") is left alone.
    """
    original = (name or "").strip()
    text = original
    glued = False
    while True:
        match = _CHANNEL_SUFFIX.search(text)
        if match is None:
            break
        rest = text[: match.start()].strip()
        if rest.lower() in _TOO_SHORT:
            break
        glued = glued or match.group("sep") == ""
        text = rest
    if glued and " " not in text:
        text = _CAMEL.sub(" ", text)
    return text or original


def split_artist_title(title: str, uploader: str = "") -> Tuple[List[str], str]:
    """Artist(s) and song title from a free-form upload title.

    YouTube videos and SoundCloud uploads carry no artist field worth the
    name; the artist sits in the title ("Artist - Song (Official Video)").
    Version markers like "(Live)" or "(Extended Mix)" stay in the title on
    purpose -- the matcher's version guard needs them.
    """
    text = _NOISE.sub("", title or "").strip()
    parts = _DASH.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return split_artists(parts[0]) or [parts[0].strip()], parts[1].strip()
    artist = clean_channel(uploader)
    return ([artist] if artist else []), text


def _playlist_key(playlist: Any) -> str:
    if isinstance(playlist, PlaylistRef):
        return (playlist.url or playlist.id or "").strip()
    return str(playlist or "").strip()


# ---------------------------------------------------------------------------
# TIDAL and Spotify: thin adapters over the existing code
# ---------------------------------------------------------------------------

class TidalProvider(PlaylistProvider):
    """:class:`hires.tidal_library.TidalLibrary` behind the common interface."""

    service = "TIDAL"
    quality = TARGET_QUALITY["TIDAL"]

    def __init__(self, library: Any, *, pacer: Optional[Pacer] = None):
        self.library = library
        self.pacer = pacer or Pacer(TIDAL_REQUEST_INTERVAL)

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.service, can_list_own=True)

    def list_folders(self, include_favorites: bool = True) -> FolderRef:
        lister = getattr(self.library, "list_folders", None)
        if callable(lister):
            return lister(include_favorites=include_favorites)
        return flat_root(self.list_playlists(include_favorites), platform=self.service)

    def list_playlists(self, include_favorites: bool = True) -> List[PlaylistRef]:
        return self.library.list_playlists(include_favorites=include_favorites)

    def _id(self, playlist: Any) -> str:
        if isinstance(playlist, PlaylistRef) and playlist.id:
            return playlist.id
        from .tidal_library import parse_tidal_url

        key = _playlist_key(playlist)
        parsed = parse_tidal_url(key)
        if parsed is not None:
            kind, tidal_id = parsed
            if kind != "playlist":
                raise SourceUnavailableError(f"TIDAL {kind} is not a playlist: {key!r}")
            return tidal_id
        if re.fullmatch(r"[0-9a-fA-F-]{20,}", key):
            return key  # a bare playlist uuid
        raise SourceUnavailableError(f"Not a TIDAL playlist link: {key!r}")

    def get_playlist(self, playlist: Any) -> PlaylistRef:
        if isinstance(playlist, PlaylistRef) and playlist.name:
            return playlist
        return self.library.get_playlist(self._id(playlist))

    def get_playlist_tracks(self, playlist: Any) -> List[TrackRef]:
        return self.library.get_playlist_tracks(self._id(playlist))

    def owns_url(self, url: str) -> bool:
        return "tidal.com" in (url or "").lower()

    def search_tracks(self, query: str, limit: int = 10) -> List[TrackRef]:
        self.pacer.wait()
        return self.library.search_tracks(query, limit=limit)

    def find_by_isrc(self, isrc: str) -> List[TrackRef]:
        self.pacer.wait()
        return self.library.find_by_isrc(isrc)


class SpotifyProvider(PlaylistProvider):
    """:class:`hires.spotify_source.SpotifySource` behind the common interface.

    Reading public playlists works without anything (embed backend); own
    playlists need the sign-in, searching needs the Client ID and Secret.
    """

    service = "Spotify"
    quality = None  # Spotify streams Ogg Vorbis whatever the tier says

    def __init__(self, source: Any):
        self.source = source

    def _has_web_api(self) -> bool:
        check = getattr(self.source, "has_web_api", None)
        return bool(check()) if callable(check) else False

    def _signed_in(self) -> bool:
        check = getattr(self.source, "is_user_authorized", None)
        return bool(check()) if callable(check) else False

    def status(self) -> ProviderStatus:
        if not self._has_web_api():
            return ProviderStatus(
                self.service,
                can_list_own=False,
                can_search=False,
                hint=(
                    "Public playlist links work. Your own playlists and searching Spotify "
                    "need the Client ID and Secret under Settings > Spotify."
                ),
            )
        if not self._signed_in():
            return ProviderStatus(
                self.service,
                can_list_own=False,
                hint="Sign in on the Accounts tab to list your own playlists. Links work without it.",
            )
        return ProviderStatus(self.service, can_list_own=True)

    def list_playlists(self, include_favorites: bool = True) -> List[PlaylistRef]:
        if not self._signed_in():
            raise AuthRequiredError(self.status().hint)
        return self.source.list_user_playlists()

    def get_playlist(self, playlist: Any) -> PlaylistRef:
        return self.source.get_playlist(playlist)

    def get_playlist_tracks(self, playlist: Any) -> List[TrackRef]:
        return self.source.get_playlist_tracks(playlist)

    def owns_url(self, url: str) -> bool:
        lowered = (url or "").lower()
        return "spotify.com" in lowered or lowered.startswith("spotify:")

    def search_tracks(self, query: str, limit: int = 10) -> List[TrackRef]:
        web = getattr(self.source, "web", None)
        if web is None or not self._has_web_api():
            raise AuthRequiredError(self.status().hint)
        return web.search_tracks(query, limit=limit)

    def find_by_isrc(self, isrc: str) -> List[TrackRef]:
        web = getattr(self.source, "web", None)
        if web is None or not self._has_web_api():
            return []
        return web.find_by_isrc(isrc)


def provider_for_url(providers: Sequence[PlaylistProvider], url: str) -> Optional[PlaylistProvider]:
    """The provider whose service a pasted link belongs to."""
    for provider in providers:
        if provider.owns_url(url):
            return provider
    return None
