"""YouTube as a playlist source and as a search target, read through yt-dlp.

yt-dlp is already part of the app (the YouTube module downloads with it), so
reading needs nothing new. Everything runs with ``extract_flat``: one request
returns a whole playlist page or a page of search results, without touching
the individual videos.

The cookie file is only ever handed to yt-dlp as a *copy* in the temp folder
-- yt-dlp writes cookies back on exit, and the user's file must stay as it is.
Its contents are never read here.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Optional

from .models import AuthRequiredError, PlaylistRef, RateLimitedError, SourceUnavailableError, TrackRef
from .playlist_providers import Pacer, PlaylistProvider, ProviderStatus, clean_channel, split_artist_title

SERVICE = "YouTube"

#: Seconds between two requests: yt-dlp's own "sleep" preset spaces
#: extraction requests 0.75 s apart (W/research-delays.md); rounded up.
REQUEST_INTERVAL = 1.0

#: Where yt-dlp lists the signed-in user's playlists.
OWN_PLAYLISTS_URL = "https://www.youtube.com/feed/playlists"

NO_COOKIES_HINT = (
    "Links and search work without a login. To list your own playlists, export a "
    "cookies.txt and select it under Settings > YouTube."
)

#: Placeholders YouTube keeps in playlists for videos that are gone.
_GONE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}

_PLAYLIST_ID = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")

Extractor = Callable[[str], Dict[str, Any]]


def playlist_id_from_url(url: str) -> str:
    match = _PLAYLIST_ID.search(url or "")
    if match:
        return match.group(1)
    if re.fullmatch(r"(?:PL|OL|UU|LL|FL|RD)[A-Za-z0-9_-]{8,}", (url or "").strip()):
        return url.strip()
    return ""


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def track_from_entry(entry: Any) -> Optional[TrackRef]:
    """Map a flat yt-dlp entry (search hit or playlist item); gone videos -> ``None``."""
    if not isinstance(entry, dict):
        return None
    video_id = str(entry.get("id") or "").strip()
    raw_title = str(entry.get("title") or "").strip()
    if not video_id or not raw_title or raw_title.lower() in _GONE_TITLES:
        return None
    uploader = str(entry.get("channel") or entry.get("uploader") or "").strip()
    artists, title = split_artist_title(raw_title, uploader)
    try:
        duration = int(float(entry.get("duration"))) if entry.get("duration") else None
    except (TypeError, ValueError):
        duration = None
    return TrackRef(
        id=video_id,
        title=title,
        artists=artists,
        duration_sec=duration,
        url=video_url(video_id),
        platform=SERVICE,
        extra={"channel": clean_channel(uploader)},
    )


def _raise_for(exc: Exception) -> None:
    message = str(exc)
    lowered = message.lower()
    blocked = ("429", "too many requests", "not a bot", "captcha", "rate-limit", "rate limit", "try again later")
    if any(word in lowered for word in blocked):
        raise RateLimitedError(
            "YouTube is limiting requests (HTTP 429 or bot check). Stopped; try again later."
        ) from exc
    if "private" in lowered or "does not exist" in lowered or "unavailable" in lowered:
        raise SourceUnavailableError("YouTube: playlist not found (deleted or private).") from exc
    raise SourceUnavailableError(f"YouTube request failed: {message[:160]}") from exc


class _QuietLogger:
    """yt-dlp would print to the app console; errors still raise."""

    def debug(self, _msg):
        pass

    info = warning = debug

    def error(self, _msg):
        pass


def make_ytdlp_extractor(cookies_path: str = "") -> Extractor:
    """An extractor running yt-dlp flat, with a temp copy of the cookie file.

    A copy, because yt-dlp's ``cookiefile`` is "read and dumped to": it would
    rewrite the user's own file.
    """
    # Context7: yt-dlp (/yt-dlp/yt-dlp) v2026.10.26 -- YoutubeDL options extract_flat,
    # skip_download, cookiefile, logger; extract_info(download=False), sanitize_info.

    def extract(url: str) -> Dict[str, Any]:
        import yt_dlp  # lazy: the tab must load even where yt-dlp is missing

        options: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "logger": _QuietLogger(),
        }
        temp_copy = ""
        try:
            if cookies_path and os.path.isfile(cookies_path):
                fd, temp_copy = tempfile.mkstemp(prefix="hires_yt_", suffix=".txt")
                os.close(fd)
                shutil.copy2(cookies_path, temp_copy)
                options["cookiefile"] = temp_copy
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
            return ydl.sanitize_info(info) if info else {}
        finally:
            if temp_copy:
                try:
                    os.remove(temp_copy)
                except OSError:
                    pass

    return extract


class YouTubeProvider(PlaylistProvider):
    """YouTube behind the common provider interface."""

    service = SERVICE
    quality = None

    def __init__(self, extract: Extractor, *, has_cookies: bool = False, pacer: Optional[Pacer] = None):
        self.extract = extract
        self.has_cookies = has_cookies
        self.pacer = pacer or Pacer(REQUEST_INTERVAL)

    def _fetch(self, url: str) -> Dict[str, Any]:
        self.pacer.wait()
        try:
            info = self.extract(url)
        except (RateLimitedError, SourceUnavailableError):
            raise
        except Exception as exc:  # yt-dlp's DownloadError and friends
            _raise_for(exc)
        return info if isinstance(info, dict) else {}

    def status(self) -> ProviderStatus:
        if self.has_cookies:
            return ProviderStatus(self.service, can_list_own=True)
        return ProviderStatus(self.service, can_list_own=False, hint=NO_COOKIES_HINT)

    # -- source -------------------------------------------------------------
    def list_playlists(self, include_favorites: bool = True) -> List[PlaylistRef]:
        if not self.has_cookies:
            raise AuthRequiredError(NO_COOKIES_HINT)
        info = self._fetch(OWN_PLAYLISTS_URL)
        out = []
        for entry in info.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            pid = playlist_id_from_url(str(entry.get("url") or "")) or str(entry.get("id") or "")
            if not pid:
                continue
            out.append(
                PlaylistRef(
                    id=pid,
                    name=str(entry.get("title") or pid),
                    owner=str(entry.get("uploader") or entry.get("channel") or ""),
                    track_count=int(entry.get("playlist_count") or 0),
                    url=f"https://www.youtube.com/playlist?list={pid}",
                    platform=SERVICE,
                )
            )
        return out

    def _playlist_info(self, playlist: Any) -> Dict[str, Any]:
        key = playlist.url or playlist.id if isinstance(playlist, PlaylistRef) else str(playlist or "")
        pid = playlist_id_from_url(key) or (playlist.id if isinstance(playlist, PlaylistRef) else "")
        if not pid:
            raise SourceUnavailableError(f"Not a YouTube playlist link: {key!r}")
        info = self._fetch(f"https://www.youtube.com/playlist?list={pid}")
        info.setdefault("id", pid)
        return info

    def get_playlist(self, playlist: Any) -> PlaylistRef:
        info = self._playlist_info(playlist)
        pid = str(info.get("id"))
        return PlaylistRef(
            id=pid,
            name=str(info.get("title") or pid),
            description=str(info.get("description") or ""),
            owner=clean_channel(str(info.get("uploader") or info.get("channel") or "")),
            track_count=int(info.get("playlist_count") or len(info.get("entries") or [])),
            url=f"https://www.youtube.com/playlist?list={pid}",
            platform=SERVICE,
        )

    def get_playlist_tracks(self, playlist: Any) -> List[TrackRef]:
        info = self._playlist_info(playlist)
        return [t for t in (track_from_entry(e) for e in info.get("entries") or []) if t is not None]

    def owns_url(self, url: str) -> bool:
        lowered = (url or "").lower()
        return "youtube.com/" in lowered or "youtu.be/" in lowered

    # -- target -------------------------------------------------------------
    def search_tracks(self, query: str, limit: int = 10) -> List[TrackRef]:
        query = (query or "").strip()
        if not query:
            return []
        size = max(1, min(int(limit), 10))
        info = self._fetch(f"ytsearch{size}:{query}")
        return [t for t in (track_from_entry(e) for e in info.get("entries") or []) if t is not None]
