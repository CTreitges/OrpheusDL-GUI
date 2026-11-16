"""SoundCloud as a playlist source and as a search target.

Talks through the SoundCloud module's own API client (``module.websession``,
``SoundCloudWebAPI`` in ``modules/soundcloud/soundcloud_api.py``), so the
public client id and the user's token stay where the module keeps them. This
file never sees the token's value; it only asks the client whether one is set.

Only reading calls are made: ``resolve``, ``playlists/<id>``, ``tracks``,
``search/tracks`` and -- with a token -- ``me`` and the user's own playlists.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from .models import (
    AuthRequiredError,
    PlaylistRef,
    RateLimitedError,
    SourceUnavailableError,
    TrackRef,
)
from .playlist_providers import Pacer, PlaylistProvider, ProviderStatus, split_artist_title, split_artists

SERVICE = "SoundCloud"

#: Seconds between two API calls. SoundCloud publishes no figure for metadata
#: calls; yt-dlp's "sleep" preset spaces extraction requests 0.75 s apart, one
#: second keeps a 300-track port at about five minutes (W/research-delays.md).
REQUEST_INTERVAL = 1.0

#: Pages followed for the user's own playlists (200 per page).
MAX_PAGES = 10

NOT_SIGNED_IN_HINT = (
    "Links and search work without a login. To list your own playlists, enter "
    "your SoundCloud token under Settings > SoundCloud."
)


#: How the web player's scripts carry the public client id (same as yt-dlp).
_CLIENT_ID = re.compile(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"')


def _default_get(url: str) -> str:
    import requests

    response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    if response.status_code == 429:
        raise RateLimitedError("SoundCloud is limiting requests (HTTP 429).")
    response.raise_for_status()
    return response.text


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _ms_to_sec(value: Any) -> Optional[int]:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return ms // 1000 if ms > 0 else None


def track_from_json(data: Any) -> Optional[TrackRef]:
    """Map a full SoundCloud track object; stubs without a title return ``None``."""
    if not isinstance(data, dict) or not data.get("id") or not _text(data.get("title")):
        return None
    meta = data.get("publisher_metadata") if isinstance(data.get("publisher_metadata"), dict) else {}
    uploader = _text((data.get("user") or {}).get("username"))
    title = _text(data.get("title"))
    label_artist = _text(meta.get("artist"))
    if label_artist:
        # Label uploads name the artist properly; the title may still repeat it.
        artists = split_artists(label_artist) or [label_artist]
        prefix = f"{label_artist} - "
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()
    else:
        artists, title = split_artist_title(title, uploader)
    return TrackRef(
        id=str(data["id"]),
        title=title,
        artists=artists,
        album=_text(meta.get("album_title") or meta.get("release_title")),
        duration_sec=_ms_to_sec(data.get("full_duration") or data.get("duration")),
        isrc=(_text(meta.get("isrc")).upper() or None),
        url=_text(data.get("permalink_url")),
        image_url=_text(data.get("artwork_url")),
        platform=SERVICE,
    )


def playlist_from_json(data: Any) -> PlaylistRef:
    data = data if isinstance(data, dict) else {}
    return PlaylistRef(
        id=str(data.get("id") or ""),
        name=_text(data.get("title")),
        description=_text(data.get("description")),
        owner=_text((data.get("user") or {}).get("username")),
        track_count=int(data.get("track_count") or 0),
        duration_sec=_ms_to_sec(data.get("duration")),
        image_url=_text(data.get("artwork_url")),
        url=_text(data.get("permalink_url")),
        platform=SERVICE,
        kind="album" if data.get("is_album") else "user",
    )


class SoundCloudProvider(PlaylistProvider):
    """SoundCloud behind the common provider interface.

    ``api`` is the module's ``SoundCloudWebAPI`` (or a stand-in with the same
    ``_get``, ``resolve_url``, ``search`` and ``get_tracks_from_tracklist``).
    """

    service = SERVICE
    quality = None  # the module picks the best stream it can get

    def __init__(
        self,
        api: Any,
        *,
        pacer: Optional[Pacer] = None,
        http_get: Optional[Callable[[str], str]] = None,
    ):
        """
        Args:
            http_get: ``url -> body text``, used only to look up a current
                public client id (see :meth:`_refresh_client_id`). Injectable
                for tests; defaults to a plain ``requests.get``.
        """
        self.api = api
        self.pacer = pacer or Pacer(REQUEST_INTERVAL)
        self.http_get = http_get or _default_get
        self._client_id_refreshed = False

    # -- plumbing -----------------------------------------------------------
    def _has_token(self) -> bool:
        return bool(_text(getattr(self.api, "access_token", "")))

    def _refresh_client_id(self) -> bool:
        """Replace a stale public client id with the one soundcloud.com uses now.

        The module ships a fixed client id; once SoundCloud rotates it, every
        call without a token answers 401. yt-dlp solves the same problem the
        same way: the id is in one of the web player's script files. Tried
        once per provider, so a real 401 cannot loop.
        """
        if self._client_id_refreshed or not hasattr(self.api, "client_id"):
            return False
        self._client_id_refreshed = True
        try:
            self.pacer.wait()
            page = self.http_get("https://soundcloud.com/")
            scripts = re.findall(r'<script[^>]+src="(https://[^"]+\.sndcdn\.com/[^"]+\.js)"', page)
            for src in reversed(scripts):  # the id sits in one of the last bundles
                self.pacer.wait()
                match = _CLIENT_ID.search(self.http_get(src))
                if match:
                    self.api.client_id = match.group(1)
                    return True
        except RateLimitedError:
            raise
        except Exception:
            return False
        return False

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """One paced request; a 429 or a captcha stops everything (never retried)."""
        self.pacer.wait()
        try:
            return fn(*args)
        except Exception as exc:  # the module raises its own error type
            message = str(exc)
            lowered = message.lower()
            if message.startswith("429") or "captcha" in lowered or "too many requests" in lowered:
                raise RateLimitedError(
                    "SoundCloud is limiting requests (HTTP 429). Stopped; try again later."
                ) from exc
            unauthorized = message.startswith("401") or "unauthorized" in lowered
            if unauthorized and not self._has_token() and self._refresh_client_id():
                return self._call(fn, *args)
            if unauthorized:
                # The module quotes the response body here -- never pass it on.
                raise AuthRequiredError(
                    "SoundCloud rejected the request (401). The token may have expired."
                ) from exc
            if message.startswith("404"):
                raise SourceUnavailableError("SoundCloud: not found (deleted or private).") from exc
            raise SourceUnavailableError(f"SoundCloud request failed: {message[:160]}") from exc

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._call(self.api._get, path, dict(params or {}))

    def status(self) -> ProviderStatus:
        if self._has_token():
            return ProviderStatus(self.service, can_list_own=True)
        return ProviderStatus(self.service, can_list_own=False, hint=NOT_SIGNED_IN_HINT)

    # -- source -------------------------------------------------------------
    def list_playlists(self, include_favorites: bool = True) -> List[PlaylistRef]:
        if not self._has_token():
            raise AuthRequiredError(NOT_SIGNED_IN_HINT)
        me = self._call(self.api.get_me)
        user_id = _text((me or {}).get("id"))
        if not user_id:
            raise SourceUnavailableError("SoundCloud did not say which account the token belongs to.")
        playlists: List[PlaylistRef] = []
        page: Any = self._get(f"users/{user_id}/playlists_without_albums", {"limit": 200, "linked_partitioning": 1})
        for _ in range(MAX_PAGES):
            collection = page.get("collection", []) if isinstance(page, dict) else (page or [])
            playlists.extend(playlist_from_json(p) for p in collection if isinstance(p, dict))
            next_href = _text(page.get("next_href")) if isinstance(page, dict) else ""
            if not next_href.startswith("https://api-v2.soundcloud.com/"):
                break  # also refuses to follow a link to any other host
            page = self._get(next_href)
        return [p for p in playlists if p.id]

    def _playlist_json(self, playlist: Any) -> Dict[str, Any]:
        if isinstance(playlist, PlaylistRef) and playlist.id.isdigit():
            data = self._get(f"playlists/{playlist.id}")
        else:
            key = playlist.url if isinstance(playlist, PlaylistRef) else _text(playlist)
            if key.isdigit():
                data = self._get(f"playlists/{key}")
            else:
                if not self.owns_url(key):
                    raise SourceUnavailableError(f"Not a SoundCloud link: {key!r}")
                data = self._call(self.api.resolve_url, key)
        if not isinstance(data, dict) or data.get("kind") != "playlist":
            kind = data.get("kind") if isinstance(data, dict) else "nothing"
            raise SourceUnavailableError(f"That SoundCloud link is a {kind}, not a playlist.")
        return data

    def get_playlist(self, playlist: Any) -> PlaylistRef:
        return playlist_from_json(self._playlist_json(playlist))

    def get_playlist_tracks(self, playlist: Any) -> List[TrackRef]:
        data = self._playlist_json(playlist)
        entries = [t for t in (data.get("tracks") or []) if isinstance(t, dict) and t.get("id")]
        if any("title" not in t for t in entries):
            # Big playlists only carry full objects for the first few tracks;
            # the module's helper fetches the rest 50 at a time, in order.
            full = self._call(self.api.get_tracks_from_tracklist, entries)
            entries = list(full.values()) if isinstance(full, dict) else entries
        return [t for t in (track_from_json(e) for e in entries) if t is not None]

    def owns_url(self, url: str) -> bool:
        return "soundcloud.com/" in (url or "").lower()

    # -- target -------------------------------------------------------------
    def search_tracks(self, query: str, limit: int = 10) -> List[TrackRef]:
        if not _text(query):
            return []
        data = self._call(self.api.search, "tracks", query, max(1, min(int(limit), 20)))
        collection = data.get("collection", []) if isinstance(data, dict) else []
        return [t for t in (track_from_json(e) for e in collection) if t is not None]
