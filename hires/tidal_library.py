"""TIDAL playlist browser backend.

Wraps the ``TidalApi`` object of the OrpheusDL TIDAL module (duck typed - the
module is loaded at runtime from a different package, so nothing is imported
from it here) and turns its raw JSON into the :mod:`hires.models` contract.

The TIDAL API returns real-world JSON: keys go missing, ``creator`` can be an
empty dict, playlists can contain videos. Every access therefore goes through
``.get()`` chains and the small ``_as_dict`` / ``_as_list`` helpers below.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    AuthRequiredError,
    HiresError,
    PlaylistRef,
    SourceUnavailableError,
    TrackRef,
)

PLATFORM = "TIDAL"

#: Service name of the TIDAL module inside an Orpheus instance.
MODULE_NAME = "tidal"

#: Owner shown for editorial playlists (they have no real creator).
EDITORIAL_OWNER = "TIDAL"

#: Cover sizes the TIDAL image CDN actually serves.
_ARTWORK_SIZES = (80, 160, 320, 480, 640, 1080, 1280, 1281)

#: Playlists are only rendered up to 1080x1080 by the CDN.
_PLAYLIST_MAX_SIZE = 1080

#: The scheme is optional, so the host has to be anchored explicitly - otherwise
#: ``re.search`` happily finds "tidal.com/track/1" inside "nottidal.com/track/1".
_URL_RE = re.compile(
    r"(?:\A|(?<=\s))(?:https?://)?(?:www\.|listen\.)?tidal\.com/(?:browse/)?"
    r"(?P<kind>track|album|playlist|artist)/(?P<id>[A-Za-z0-9-]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def track_url(track_id: Any) -> str:
    return f"https://tidal.com/browse/track/{track_id}"


def album_url(album_id: Any) -> str:
    return f"https://tidal.com/browse/album/{album_id}"


def artist_url(artist_id: Any) -> str:
    return f"https://tidal.com/browse/artist/{artist_id}"


def playlist_url(playlist_id: Any) -> str:
    return f"https://tidal.com/browse/playlist/{playlist_id}"


def artwork_url(cover_id: Any, size: int = 640, max_size: int = 1280) -> str:
    """Build a cover URL the way the TIDAL module's ``_generate_artwork_url`` does.

    ``size`` is snapped to the nearest size the CDN serves; anything above
    ``max_size`` falls back to ``origin.jpg``. Returns ``""`` for a missing id.
    """
    if not cover_id:
        return ""
    try:
        wanted = int(size)
    except (TypeError, ValueError):
        wanted = 640
    best = min(_ARTWORK_SIZES, key=lambda s: abs(s - wanted))
    image_name = f"{best}x{best}.jpg" if best <= max_size else "origin.jpg"
    return f"https://resources.tidal.com/images/{str(cover_id).replace('-', '/')}/{image_name}"


def parse_tidal_url(url: str) -> Optional[Tuple[str, str]]:
    """Return ``(kind, id)`` for a TIDAL link, or ``None`` if it is not one.

    Understands ``tidal.com`` and ``listen.tidal.com``, with and without the
    ``/browse/`` segment, with or without scheme.
    """
    if not url or not isinstance(url, str):
        return None
    match = _URL_RE.search(url)
    if not match:
        return None
    return match.group("kind").lower(), match.group("id")


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _id_str(value: Any) -> str:
    """Ids arrive as int (tracks/albums) or str (playlist uuids)."""
    return "" if value is None else str(value).strip()


def _cover_id(data: Dict[str, Any]) -> Any:
    """TIDAL is inconsistent about which key carries the image id."""
    return (
        data.get("squareImage")
        or data.get("image")
        or data.get("cover")
        or data.get("picture")
    )


def _is_error_payload(data: Dict[str, Any]) -> bool:
    """True for the ``{"status": 404, ...}`` bodies the API returns instead of raising."""
    status = _int_or_none(data.get("status"))
    return status is not None and status != 200


# ---------------------------------------------------------------------------
# JSON -> contract mapping
# ---------------------------------------------------------------------------

def _playlist_from_json(data: Any, kind: str = "user") -> PlaylistRef:
    data = _as_dict(data)
    creator = _as_dict(data.get("creator"))
    tidal_type = _text(data.get("type")).upper()

    owner = _text(creator.get("name"))
    if not owner and tidal_type == "EDITORIAL":
        owner = EDITORIAL_OWNER

    pid = _id_str(data.get("uuid")) or _id_str(data.get("id"))
    extra: Dict[str, Any] = {}
    if tidal_type:
        extra["tidal_type"] = tidal_type
    if data.get("lastUpdated"):
        extra["last_updated"] = data.get("lastUpdated")
    if creator.get("id") is not None:
        extra["creator_id"] = str(creator.get("id"))

    return PlaylistRef(
        id=pid,
        name=data.get("title") or "",
        description=data.get("description") or "",
        owner=owner,
        track_count=_int_or_none(data.get("numberOfTracks")) or 0,
        duration_sec=_int_or_none(data.get("duration")),
        image_url=artwork_url(_cover_id(data), max_size=_PLAYLIST_MAX_SIZE),
        url=playlist_url(pid) if pid else "",
        platform=PLATFORM,
        kind=kind,
        extra=extra,
    )


def _track_from_json(data: Any) -> TrackRef:
    data = _as_dict(data)
    album = _as_dict(data.get("album"))

    tid = _id_str(data.get("id"))
    title = data.get("title") or ""
    version = _text(data.get("version"))
    if version:
        title = f"{title} ({version})"

    artists = [
        _text(a.get("name"))
        for a in _as_list(data.get("artists"))
        if isinstance(a, dict) and _text(a.get("name"))
    ]

    extra: Dict[str, Any] = {}
    if version:
        # Kept separately so matching can work on the plain title.
        extra["version"] = version
    if album.get("id") is not None:
        extra["album_id"] = str(album.get("id"))

    return TrackRef(
        id=tid,
        title=title,
        artists=artists,
        album=album.get("title") or "",
        duration_sec=_int_or_none(data.get("duration")),
        isrc=_text(data.get("isrc")) or None,
        explicit=bool(data.get("explicit")),
        url=track_url(tid) if tid else "",
        image_url=artwork_url(album.get("cover")),
        track_number=_int_or_none(data.get("trackNumber")),
        disc_number=_int_or_none(data.get("volumeNumber")),
        platform=PLATFORM,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

class TidalLibrary:
    """Read-only view on the logged-in user's TIDAL playlists.

    ``api`` is a ``TidalApi``-like object (the ``.session`` attribute of the
    TIDAL ``ModuleInterface``). Only these methods are used:
    ``authenticated_session``, ``iter_user_playlist_entries``, ``get_playlist``,
    ``get_playlist_items``, ``get_search_data`` and ``get_tracks_by_isrc``.
    """

    def __init__(self, api: Any):
        self.api = api

    # -- construction -------------------------------------------------------
    @classmethod
    def from_orpheus(cls, orpheus_instance: Any) -> Optional["TidalLibrary"]:
        """Build a library from a running Orpheus instance.

        Returns ``None`` when the TIDAL module is not available at all (not
        installed, failed to load, no ``.session``) and raises
        :class:`AuthRequiredError` when the module is there but nobody is
        logged in - the caller can distinguish "no TIDAL" from "please log in".
        """
        if orpheus_instance is None:
            return None

        module = _as_dict(getattr(orpheus_instance, "loaded_modules", None)).get(MODULE_NAME)
        if module is None:
            known = getattr(orpheus_instance, "module_list", None) or ()
            try:
                available = MODULE_NAME in known
            except TypeError:
                available = False
            loader = getattr(orpheus_instance, "load_module", None)
            if not available or not callable(loader):
                return None
            try:
                module = loader(MODULE_NAME)
            except Exception:
                return None

        api = getattr(module, "session", None)
        if api is None:
            return None

        library = cls(api)
        if not library.is_authenticated():
            raise AuthRequiredError("TIDAL module is loaded but no user is logged in")
        return library

    # -- auth ---------------------------------------------------------------
    def _authenticated_session(self) -> Any:
        getter = getattr(self.api, "authenticated_session", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def current_user_id(self) -> Optional[str]:
        """User id of the first logged-in session, or ``None``."""
        session = self._authenticated_session()
        user_id = getattr(session, "user_id", None) if session is not None else None
        return str(user_id) if user_id else None

    def is_authenticated(self) -> bool:
        return self.current_user_id() is not None

    def _require_user_id(self) -> str:
        user_id = self.current_user_id()
        if not user_id:
            raise AuthRequiredError("TIDAL login required to browse personal playlists")
        return user_id

    # -- playlists ----------------------------------------------------------
    def list_playlists(self, include_favorites: bool = True) -> List[PlaylistRef]:
        """Own and favourited playlists of the logged-in user, de-duplicated."""
        user_id = self._require_user_id()

        out: List[PlaylistRef] = []
        seen = set()
        try:
            for entry in self.api.iter_user_playlist_entries(user_id):
                entry = _as_dict(entry)
                data = entry.get("playlist")
                if not isinstance(data, dict):
                    # Some responses inline the playlist instead of wrapping it.
                    data = entry if entry.get("uuid") else None
                if not data:
                    continue

                kind = "user" if _text(entry.get("type")).upper() == "USER_CREATED" else "favorite"
                if kind != "user" and not include_favorites:
                    continue

                playlist = _playlist_from_json(data, kind=kind)
                if not playlist.id or playlist.id in seen:
                    continue
                seen.add(playlist.id)
                out.append(playlist)
        except HiresError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(f"Could not list TIDAL playlists: {exc}") from exc
        return out

    def get_playlist(self, playlist_id: str) -> PlaylistRef:
        """Metadata of a single playlist."""
        try:
            data = _as_dict(self.api.get_playlist(playlist_id))
        except Exception as exc:
            raise SourceUnavailableError(f"Could not load TIDAL playlist {playlist_id}: {exc}") from exc
        if not data or _is_error_payload(data):
            raise SourceUnavailableError(f"TIDAL playlist {playlist_id} not found")

        kind = "editorial" if _text(data.get("type")).upper() == "EDITORIAL" else "user"
        playlist = _playlist_from_json(data, kind=kind)
        if not playlist.id:
            # Keep the caller's id so the result stays addressable.
            playlist.id = str(playlist_id)
            playlist.url = playlist_url(playlist_id)
        return playlist

    def get_playlist_tracks(self, playlist_id: str) -> List[TrackRef]:
        """All *tracks* of a playlist - videos and other item types are skipped."""
        try:
            data = _as_dict(self.api.get_playlist_items(playlist_id))
        except Exception as exc:
            raise SourceUnavailableError(
                f"Could not load items of TIDAL playlist {playlist_id}: {exc}"
            ) from exc
        if not data or _is_error_payload(data):
            # An empty playlist still answers with a dict, so a falsy body means
            # the response was unusable - never report that as "0 tracks".
            raise SourceUnavailableError(f"TIDAL playlist {playlist_id} not found")

        out: List[TrackRef] = []
        for entry in _as_list(data.get("items")):
            entry = _as_dict(entry)
            if _text(entry.get("type")).lower() != "track":
                continue
            item = entry.get("item")
            if not isinstance(item, dict):
                continue
            track = _track_from_json(item)
            if track.id:
                out.append(track)
        return out

    # -- lookup -------------------------------------------------------------
    def search_tracks(self, query: str, limit: int = 20) -> List[TrackRef]:
        """Catalogue track search."""
        if not _text(query):
            return []
        try:
            data = _as_dict(self.api.get_search_data(query, limit=limit))
        except Exception as exc:
            raise SourceUnavailableError(f"TIDAL search for {query!r} failed: {exc}") from exc

        items = _as_list(_as_dict(data.get("tracks")).get("items"))
        tracks = [_track_from_json(i) for i in items]
        return [t for t in tracks if t.id]

    def find_by_isrc(self, isrc: str) -> List[TrackRef]:
        """Tracks with this ISRC. Never raises - an unknown ISRC yields ``[]``."""
        code = _text(isrc).upper()
        if not code:
            return []
        try:
            data = _as_dict(self.api.get_tracks_by_isrc(code))
        except Exception:
            return []
        if _is_error_payload(data):
            return []
        tracks = [_track_from_json(i) for i in _as_list(data.get("items"))]
        return [t for t in tracks if t.id]
