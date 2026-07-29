"""Spotify metadata source: playlists and tracks, including ISRC.

This module never downloads anything - it only reads metadata, which is then
handed to :mod:`hires.matcher` to find the same track on TIDAL.

Two backends are available and :class:`SpotifySource` automatically falls back
from the first to the second:

``WebApiBackend``
    The official Web API, driven with the user's *own* app credentials.
    Client-credentials auth is enough for public playlists; private playlists
    and Liked Songs need the Authorization-Code + PKCE flow.  This is the only
    backend that reliably returns an ISRC (``track.external_ids.isrc``).

``EmbedBackend``
    The anonymous GraphQL API behind ``open.spotify.com/embed``.  Needs no
    setup at all but only works for public playlists, and it does **not**
    expose ISRCs - :attr:`TrackRef.isrc` stays ``None`` and the matcher has to
    fall back to fuzzy matching.

Nothing here imports the OrpheusDL Spotify module: that is a separate package
which may not be installed.  ``requests`` is imported lazily so this module
stays importable (and testable) without it; every HTTP call goes through an
injected session object, so tests never touch the network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from .models import (
    AuthRequiredError,
    HiresError,
    PlaylistRef,
    SourceUnavailableError,
    TrackRef,
)

log = logging.getLogger(__name__)

PLATFORM = "Spotify"

API_BASE = "https://api.spotify.com/v1"
ACCOUNTS_BASE = "https://accounts.spotify.com"
TOKEN_URL = ACCOUNTS_BASE + "/api/token"
AUTHORIZE_URL = ACCOUNTS_BASE + "/authorize"

#: Everything the playlist browser needs, and not one scope more.
DEFAULT_SCOPES: Tuple[str, ...] = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
)

#: Where Spotify sends the user back after they approve.
#:
#: This has to be one of the redirect URIs registered on the Spotify app, or
#: Spotify simply refuses to redirect after login -- the browser stops on an
#: error page and the app waits for a callback that will never arrive.
#:
#: It matches the URI the GUI's own *Settings > Spotify* panel offers for
#: copying into the Spotify dashboard, so following that panel is enough to
#: make signing in work. It used to be ``:8888/callback``, which nothing ever
#: told anyone to register -- so the sign-in could only ever hang.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:4381/login"

#: The URI the GUI's setup panel tells people to register. Kept as its own name
#: so the two cannot drift apart silently again.
SETUP_PANEL_REDIRECT_URI = "http://127.0.0.1:4381/login"

#: Id of the synthetic "Liked Songs" playlist (Spotify has no real id for it).
LIKED_ID = "liked"
LIKED_NAME = "Liked Songs"

REQUEST_TIMEOUT = 30

#: Renew an access token this many seconds before it actually expires.
TOKEN_EXPIRY_MARGIN = 60

#: How often a 429 is retried before giving up.
MAX_RATE_LIMIT_RETRIES = 3

#: Never sleep longer than this on a Retry-After, however long Spotify asks.
MAX_RETRY_AFTER = 60.0

#: Page sizes the API accepts.
PLAYLIST_PAGE_LIMIT = 50
TRACK_PAGE_LIMIT = 100

#: Hard stop for pagination loops - a broken ``next`` link must not hang the GUI.
MAX_PAGES = 500

_TOKEN_FILE_VERSION = 1


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_KINDS = "playlist|track|album|artist|show|episode|user"

#: ``/intl-de/``, but also region qualified forms such as ``/intl-pt-br/``.
_LOCALE = r"(?:intl-[a-z]{2,3}(?:-[a-z]{2,4})?/)?"

_URL_RE = re.compile(
    r"(?:https?://)?(?:open|play)\.spotify\.com/"
    + _LOCALE
    + r"(?:embed/)?"
    # Old links put the owner in front: /user/<name>/playlist/<id>
    r"(?:user/[A-Za-z0-9._~%-]+/)?"
    r"(?P<kind>" + _KINDS + r")/(?P<id>[A-Za-z0-9]+)",
    re.IGNORECASE,
)

#: Liked Songs have no playlist id; this is the link the web player uses.
_LIKED_RE = re.compile(
    r"(?:(?:https?://)?(?:open|play)\.spotify\.com/" + _LOCALE + r"collection/tracks"
    r"|spotify:collection:tracks)",
    re.IGNORECASE,
)

# The legacy form nests the real object behind ``user:<name>:``.
_URI_RE = re.compile(
    r"spotify:(?:user:[A-Za-z0-9._~-]+:)?(?P<kind>" + _KINDS + r"):(?P<id>[A-Za-z0-9]+)",
    re.IGNORECASE,
)

_BARE_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_spotify_url(url_or_uri: Any) -> Optional[Tuple[str, str]]:
    """Return ``(kind, id)`` for anything that addresses a Spotify object.

    Understands the web links (with ``/intl-xx/`` locale prefix, ``/embed/``
    and ``?si=`` tracking parameters), ``spotify:playlist:ID`` URIs including
    the legacy ``spotify:user:me:playlist:ID`` form, and a bare 22 character
    base62 id (which is assumed to be a playlist - that is what the converter
    asks for).  The Liked Songs link comes back as ``("liked", "liked")``
    because that collection has no id of its own.  Returns ``None`` when the
    input addresses nothing.
    """
    if not isinstance(url_or_uri, str):
        return None
    text = url_or_uri.strip()
    if not text:
        return None

    if _BARE_ID_RE.match(text):
        return "playlist", text

    if _LIKED_RE.search(text):
        return "liked", LIKED_ID

    match = _URI_RE.search(text)
    if match:
        return match.group("kind").lower(), match.group("id")

    match = _URL_RE.search(text)
    if match:
        return match.group("kind").lower(), match.group("id")
    return None


def playlist_url(playlist_id: Any) -> str:
    return f"https://open.spotify.com/playlist/{playlist_id}"


def track_url(track_id: Any) -> str:
    return f"https://open.spotify.com/track/{track_id}"


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def generate_code_verifier(length: int = 64) -> str:
    """A random PKCE code verifier, clamped to the legal 43..128 characters."""
    length = max(43, min(128, int(length)))
    # token_urlsafe(96) is 128 characters, enough to slice any legal length.
    return secrets.token_urlsafe(96)[:length]


def get_code_challenge(verifier: str) -> str:
    """S256 challenge: base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256((verifier or "").encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    scopes: Sequence[str],
    code_challenge: str,
    state: str = "",
) -> str:
    """The URL the user has to open in a browser to grant access."""
    params = {
        "client_id": client_id or "",
        "response_type": "code",
        "redirect_uri": redirect_uri or "",
        "scope": " ".join(scopes or ()),
        "code_challenge_method": "S256",
        "code_challenge": code_challenge or "",
    }
    if state:
        params["state"] = state
    return AUTHORIZE_URL + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Defensive JSON helpers
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json.loads() happily produces Infinity/NaN floats.
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN/Infinity are legal JSON for Python's parser but useless as numbers.
    return number if math.isfinite(number) else None


def _ms_to_sec(value: Any) -> Optional[int]:
    millis = _float_or_none(value)
    if millis is None or millis < 0:
        return None
    return int(round(millis / 1000.0))


def _best_image(images: Any) -> str:
    """Largest image URL of a Spotify ``images`` list."""
    best_url = ""
    best_width = -1
    for image in _as_list(images):
        url = _text(_as_dict(image).get("url"))
        if not url:
            continue
        width = _int_or_none(_as_dict(image).get("width")) or 0
        if width > best_width:
            best_width, best_url = width, url
    return best_url


def _spotify_url(data: Dict[str, Any], fallback: str = "") -> str:
    return _text(_as_dict(data.get("external_urls")).get("spotify")) or fallback


def _header(response: Any, name: str) -> str:
    """Read a header from a response whose headers may be a plain dict."""
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    except AttributeError:
        return ""
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` without ever leaving a half-written file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".spotify-token-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                log.warning("could not remove temp file %s", tmp_path)
    try:
        # Contains a refresh token - keep it out of other users' reach.
        os.chmod(path, 0o600)
    except OSError:
        pass


#: Page the browser lands on once Spotify has redirected back.
_CALLBACK_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>OrpheusDL GUI</title>"
    "<body style='font-family:system-ui;background:#1d1e1e;color:#e4e5e6;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
    "<div style='text-align:center'><h2>{heading}</h2><p>{message}</p></div>"
)


def make_callback_server(address: Any, handler: Any) -> Any:
    """Default server factory for :func:`wait_for_authorization_code`.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` purely to fill in
    ``server_name``. On macOS that reverse lookup can stall for tens of seconds
    when nothing answers for 127.0.0.1 -- long enough that the browser redirect
    arrives before the socket is serving and sign-in fails. We serve exactly one
    local request, so the FQDN is worthless to us; skip it.
    """
    import socketserver
    from http.server import HTTPServer

    class _FastBindHTTPServer(HTTPServer):
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            bound_host, bound_port = self.server_address[:2]
            self.server_name = str(bound_host)
            self.server_port = bound_port

    return _FastBindHTTPServer(address, handler)


def wait_for_authorization_code(
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    expected_state: str = "",
    timeout: float = 300.0,
    *,
    server_factory: Any = None,
) -> str:
    """Serve ``redirect_uri`` locally and return the ``code`` Spotify sends back.

    Spotify hands the authorization code to a redirect URI rather than showing
    it, so something has to listen. This runs a single-shot HTTP server on the
    host/port of ``redirect_uri`` until the browser hits it.

    ``expected_state`` is compared against the ``state`` parameter and a
    mismatch is rejected -- without that check any page the user happens to
    visit could feed us a code.

    Raises :class:`AuthRequiredError` on denial, state mismatch or timeout.
    """
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(redirect_uri or DEFAULT_REDIRECT_URI)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    outcome: Dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            params = parse_qs(urlparse(self.path).query)
            code = (params.get("code") or [""])[0]
            state = (params.get("state") or [""])[0]
            error = (params.get("error") or [""])[0]

            if error:
                outcome["error"] = f"Spotify denied the request: {error}"
            elif expected_state and state != expected_state:
                outcome["error"] = "Authorization state did not match - ignoring the response"
            elif code:
                outcome["code"] = code
            else:
                # Some other request (favicon, etc.) - keep waiting.
                self.send_response(404)
                self.end_headers()
                return

            ok = "code" in outcome
            body = _CALLBACK_PAGE.format(
                heading="Signed in" if ok else "Sign-in failed",
                message=(
                    "You can close this tab and go back to OrpheusDL GUI."
                    if ok
                    else outcome.get("error", "")
                ),
            ).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # keep the console clean
            pass

    factory = server_factory or make_callback_server
    try:
        server = factory((host, port), Handler)
    except OSError as exc:
        raise AuthRequiredError(
            f"Cannot listen on {host}:{port} for the Spotify redirect ({exc}). "
            "Another sign-in may still be waiting - give it a minute, or close "
            "whatever else is using that port."
        ) from exc

    deadline = time.monotonic() + max(1.0, float(timeout))
    try:
        server.timeout = 1.0
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    finally:
        try:
            server.server_close()
        except Exception:
            pass

    if outcome.get("code"):
        return str(outcome["code"])
    if outcome.get("error"):
        raise AuthRequiredError(outcome["error"])

    # Nothing arrived at all. By far the most common cause is that this exact
    # URI is not registered on the Spotify app, in which case Spotify shows an
    # error after login and never redirects -- so name the URI rather than
    # reporting a bare timeout the user cannot act on.
    raise AuthRequiredError(
        "Spotify never redirected back. Open your app on developer.spotify.com "
        f"and make sure its Redirect URIs contain exactly:  {redirect_uri or DEFAULT_REDIRECT_URI}"
    )


def _default_http() -> Any:
    """A ``requests.Session``; imported lazily so the module stays dependency free."""
    import requests  # noqa: PLC0415 - deliberately lazy

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    return session


# ---------------------------------------------------------------------------
# Web API JSON -> contract
# ---------------------------------------------------------------------------

def _track_from_api(data: Any) -> Optional[TrackRef]:
    """Map a Web API track object, or ``None`` if it is not a usable track.

    Playlists contain podcast episodes (``type: "episode"``) and local files
    (no id) - both are skipped rather than turned into an unplayable entry.
    """
    data = _as_dict(data)
    kind = _text(data.get("type")).lower()
    if kind and kind != "track":
        return None

    track_id = _text(data.get("id"))
    if not track_id:
        # Local files and removed tracks have no id, so nothing can match them.
        return None

    album = _as_dict(data.get("album"))
    artists = [
        _text(_as_dict(a).get("name"))
        for a in _as_list(data.get("artists"))
        if _text(_as_dict(a).get("name"))
    ]

    extra: Dict[str, Any] = {}
    if _text(album.get("id")):
        extra["album_id"] = _text(album.get("id"))
    if _text(data.get("uri")):
        extra["uri"] = _text(data.get("uri"))

    return TrackRef(
        id=track_id,
        title=_text(data.get("name")),
        artists=artists,
        album=_text(album.get("name")),
        duration_sec=_ms_to_sec(data.get("duration_ms")),
        isrc=(_text(_as_dict(data.get("external_ids")).get("isrc")).upper() or None),
        explicit=bool(data.get("explicit")),
        url=_spotify_url(data, track_url(track_id)),
        image_url=_best_image(album.get("images")),
        track_number=_int_or_none(data.get("track_number")),
        disc_number=_int_or_none(data.get("disc_number")),
        platform=PLATFORM,
        extra=extra,
    )


def _tracks_from_items(items: Any) -> List[TrackRef]:
    """Map ``{"track": {...}}`` wrappers, skipping everything unusable.

    ``track`` is ``null`` for titles that were removed or are local files, and
    an episode object when a podcast was added to the playlist.
    """
    out: List[TrackRef] = []
    for item in _as_list(items):
        payload = _as_dict(item)
        raw = payload.get("track")
        if raw is None:
            raw = payload if payload.get("type") == "track" else None
        if not isinstance(raw, dict):
            continue
        track = _track_from_api(raw)
        if track is not None:
            out.append(track)
    return out


def _playlist_from_api(data: Any, kind: str = "user") -> PlaylistRef:
    data = _as_dict(data)
    owner = _as_dict(data.get("owner"))
    playlist_id = _text(data.get("id"))

    extra: Dict[str, Any] = {}
    if _text(data.get("uri")):
        extra["uri"] = _text(data.get("uri"))
    if _text(owner.get("id")):
        extra["owner_id"] = _text(owner.get("id"))
    if data.get("public") is not None:
        extra["public"] = bool(data.get("public"))

    return PlaylistRef(
        id=playlist_id,
        name=_text(data.get("name")),
        description=_text(data.get("description")),
        owner=_text(owner.get("display_name")) or _text(owner.get("id")),
        track_count=_int_or_none(_as_dict(data.get("tracks")).get("total")) or 0,
        image_url=_best_image(data.get("images")),
        url=_spotify_url(data, playlist_url(playlist_id) if playlist_id else ""),
        platform=PLATFORM,
        kind=kind,
        extra=extra,
    )


def liked_playlist_ref(track_count: int = 0) -> PlaylistRef:
    """The synthetic playlist standing in for the user's Liked Songs."""
    return PlaylistRef(
        id=LIKED_ID,
        name=LIKED_NAME,
        owner="",
        track_count=max(0, _int_or_none(track_count) or 0),
        url="https://open.spotify.com/collection/tracks",
        platform=PLATFORM,
        kind="liked",
    )


# ---------------------------------------------------------------------------
# Web API backend
# ---------------------------------------------------------------------------

class WebApiBackend:
    """Spotify Web API client using the user's own app credentials.

    Two auth modes, both optional:

    * *client credentials* (``client_id`` + ``client_secret``) - enough for
      public playlists, obtained silently without any browser.
    * *authorization code + PKCE* - needed for private playlists and Liked
      Songs.  Drive it with :meth:`begin_authorization`, hand the ``code`` from
      the redirect to :meth:`exchange_code`, and the refresh token is kept in
      ``token_path`` from then on.

    ``http`` is anything with ``requests``-style ``get``/``post`` returning an
    object with ``status_code``, ``headers`` and ``json()``; ``sleep`` and
    ``now`` exist so tests neither wait nor depend on the wall clock.
    """

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        *,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        token_path: Optional[str] = None,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = _text(client_id)
        self.client_secret = _text(client_secret)
        self.redirect_uri = _text(redirect_uri) or DEFAULT_REDIRECT_URI
        self.token_path = str(token_path) if token_path else None
        self.scopes = tuple(scopes or DEFAULT_SCOPES)
        self._http = http
        self._sleep = sleep
        self._now = now

        # {"access_token": str, "expires_at": float}
        self._app_token: Dict[str, Any] = {}
        # {"access_token": str, "refresh_token": str, "expires_at": float, "scope": str}
        self._user_token: Dict[str, Any] = {}
        self._load_tokens()

    # -- plumbing -----------------------------------------------------------
    @property
    def http(self) -> Any:
        if self._http is None:
            self._http = _default_http()
        return self._http

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        auth_request: bool = False,
    ) -> Dict[str, Any]:
        """One HTTP round trip, with 429 handling. Returns the parsed body.

        ``auth_request`` marks a call to the token endpoint, where every 4xx
        means "these credentials do not work" (Spotify answers a revoked
        refresh token with ``400 invalid_grant``) rather than "service down".
        """
        attempt = 0
        while True:
            try:
                if method == "POST":
                    response = self.http.post(
                        url, headers=headers, data=data, timeout=REQUEST_TIMEOUT
                    )
                else:
                    response = self.http.get(
                        url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
                    )
            except Exception as exc:  # noqa: BLE001 - any transport error is "unavailable"
                raise SourceUnavailableError(f"Spotify request to {url} failed: {exc}") from exc

            status = _int_or_none(getattr(response, "status_code", None)) or 0

            if status == 429:
                attempt += 1
                if attempt > MAX_RATE_LIMIT_RETRIES:
                    raise SourceUnavailableError(
                        f"Spotify rate limit not lifted after {MAX_RATE_LIMIT_RETRIES} retries: {url}"
                    )
                wait = self._retry_after(response)
                log.warning("Spotify rate limited (429), waiting %ss before retry %s", wait, attempt)
                self._sleep(wait)
                continue

            if auth_request and 400 <= status < 500:
                raise AuthRequiredError(
                    f"Spotify rejected the login request (HTTP {status}) - log in again"
                )
            if status == 401:
                raise AuthRequiredError(f"Spotify rejected the access token for {url}")
            if status == 403:
                raise AuthRequiredError(f"Spotify denied access to {url} (missing scope?)")
            if status >= 400:
                raise SourceUnavailableError(f"Spotify returned HTTP {status} for {url}")

            try:
                body = response.json()
            except Exception as exc:  # noqa: BLE001 - malformed body is a source problem
                raise SourceUnavailableError(f"Spotify sent no JSON for {url}: {exc}") from exc
            return _as_dict(body)

    @staticmethod
    def _retry_after(response: Any) -> float:
        seconds = _float_or_none(_header(response, "Retry-After"))
        if seconds is None or seconds < 0:
            seconds = 1.0
        return min(seconds, MAX_RETRY_AFTER)

    # -- token storage ------------------------------------------------------
    def _load_tokens(self) -> None:
        """Read the token file. A missing or broken file simply means "logged out"."""
        if not self.token_path or not os.path.exists(self.token_path):
            return
        try:
            with open(self.token_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            log.warning("could not read Spotify token file %s", self.token_path)
            return
        user = _as_dict(_as_dict(raw).get("user"))
        if _text(user.get("refresh_token")) or _text(user.get("access_token")):
            self._user_token = dict(user)

    def _save_tokens(self) -> None:
        if not self.token_path:
            return
        try:
            _atomic_write_json(
                self.token_path,
                {"version": _TOKEN_FILE_VERSION, "user": self._user_token},
            )
        except OSError:
            log.exception("could not write Spotify token file %s", self.token_path)

    def forget_user(self) -> None:
        """Drop the user login (the app token is unaffected)."""
        self._user_token = {}
        self._save_tokens()

    # -- state --------------------------------------------------------------
    def has_client_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def has_user_token(self) -> bool:
        return bool(_text(self._user_token.get("refresh_token")) or self._valid_user_access_token())

    def is_configured(self) -> bool:
        """True when this backend can fetch *something*."""
        return self.has_client_credentials() or self.has_user_token()

    def _valid_user_access_token(self) -> str:
        token = _text(self._user_token.get("access_token"))
        expires_at = _float_or_none(self._user_token.get("expires_at")) or 0.0
        if token and self._now() < expires_at - TOKEN_EXPIRY_MARGIN:
            return token
        return ""

    # -- authorization code + PKCE -----------------------------------------
    def begin_authorization(self, state: str = "") -> Tuple[str, str]:
        """Return ``(authorize_url, code_verifier)``; keep the verifier for
        :meth:`exchange_code`."""
        if not self.client_id:
            raise AuthRequiredError("A Spotify client id is required to log in")
        verifier = generate_code_verifier()
        url = build_authorize_url(
            self.client_id,
            self.redirect_uri,
            self.scopes,
            get_code_challenge(verifier),
            state=state,
        )
        return url, verifier

    def exchange_code(self, code: str, code_verifier: str) -> Dict[str, Any]:
        """Trade the redirect ``code`` for an access + refresh token."""
        if not self.client_id:
            raise AuthRequiredError("A Spotify client id is required to log in")
        payload = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": code or "",
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier or "",
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        return self._store_user_token(
            self._send("POST", TOKEN_URL, data=payload, auth_request=True)
        )

    def refresh(self, refresh_token: str = "") -> Dict[str, Any]:
        """Renew the user access token. Spotify may or may not send a new
        refresh token back - the old one is kept when it does not."""
        token = _text(refresh_token) or _text(self._user_token.get("refresh_token"))
        if not token:
            raise AuthRequiredError("No Spotify refresh token stored")
        if not self.client_id:
            raise AuthRequiredError("A Spotify client id is required to refresh the token")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        return self._store_user_token(
            self._send("POST", TOKEN_URL, data=payload, auth_request=True),
            fallback_refresh=token,
        )

    def _store_user_token(
        self, data: Dict[str, Any], fallback_refresh: str = ""
    ) -> Dict[str, Any]:
        access = _text(data.get("access_token"))
        if not access:
            raise AuthRequiredError("Spotify returned no access token")
        expires_in = _float_or_none(data.get("expires_in")) or 3600.0
        self._user_token = {
            "access_token": access,
            "refresh_token": _text(data.get("refresh_token"))
            or fallback_refresh
            or _text(self._user_token.get("refresh_token")),
            "expires_at": self._now() + expires_in,
            "scope": _text(data.get("scope")) or " ".join(self.scopes),
        }
        self._save_tokens()
        return dict(self._user_token)

    # -- client credentials -------------------------------------------------
    def _client_credentials_token(self) -> str:
        token = _text(self._app_token.get("access_token"))
        expires_at = _float_or_none(self._app_token.get("expires_at")) or 0.0
        if token and self._now() < expires_at - TOKEN_EXPIRY_MARGIN:
            return token
        if not self.has_client_credentials():
            raise AuthRequiredError("Spotify client id and secret are not configured")

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        data = self._send(
            "POST",
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            auth_request=True,
        )
        access = _text(data.get("access_token"))
        if not access:
            raise AuthRequiredError("Spotify returned no client-credentials token")
        expires_in = _float_or_none(data.get("expires_in")) or 3600.0
        self._app_token = {"access_token": access, "expires_at": self._now() + expires_in}
        return access

    # -- token selection ----------------------------------------------------
    def user_access_token(self) -> str:
        """Access token of the logged-in user, refreshed when it is about to expire."""
        token = self._valid_user_access_token()
        if token:
            return token
        if not _text(self._user_token.get("refresh_token")):
            raise AuthRequiredError("Spotify login required")
        return _text(self.refresh().get("access_token"))

    def _access_token(self) -> str:
        """User token when we have one (it can read more), app token otherwise."""
        if self.has_user_token():
            return self.user_access_token()
        return self._client_credentials_token()

    # -- requests -----------------------------------------------------------
    def _api_get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        user_only: bool = False,
    ) -> Dict[str, Any]:
        token = self.user_access_token() if user_only else self._access_token()
        return self._send("GET", url, headers={"Authorization": f"Bearer {token}"}, params=params)

    def _paginate(
        self,
        path: str,
        *,
        limit: int,
        params: Optional[Dict[str, Any]] = None,
        user_only: bool = False,
    ) -> List[Any]:
        """Follow ``next`` links until the collection is exhausted."""
        query: Optional[Dict[str, Any]] = dict(params or {})
        query["limit"] = limit
        query["offset"] = 0

        url: Optional[str] = API_BASE + path
        items: List[Any] = []
        seen_urls = set()
        for _ in range(MAX_PAGES):
            if not url or url in seen_urls:
                break
            seen_urls.add(url)
            data = self._api_get(url, query, user_only=user_only)
            items.extend(_as_list(data.get("items")))
            next_url = _text(data.get("next"))
            if next_url and not next_url.startswith(API_BASE + "/"):
                # Never send the bearer token to a host Spotify did not name.
                log.warning("ignoring off-site Spotify next link %s", next_url)
                next_url = ""
            url = next_url or None
            # The next link already carries limit/offset.
            query = None
        return items

    # -- public surface -----------------------------------------------------
    def current_user(self) -> Dict[str, Any]:
        return self._api_get(API_BASE + "/me", user_only=True)

    def list_playlists(self) -> List[PlaylistRef]:
        raw = self._paginate("/me/playlists", limit=PLAYLIST_PAGE_LIMIT, user_only=True)
        playlists = [_playlist_from_api(entry) for entry in raw]
        return [p for p in playlists if p.id]

    def get_playlist(self, playlist_id: str) -> PlaylistRef:
        data = self._api_get(f"{API_BASE}/playlists/{playlist_id}")
        playlist = _playlist_from_api(data)
        if not playlist.id:
            playlist.id = str(playlist_id)
            playlist.url = playlist.url or playlist_url(playlist_id)
        return playlist

    def get_playlist_tracks(self, playlist_id: str) -> List[TrackRef]:
        raw = self._paginate(
            f"/playlists/{playlist_id}/tracks",
            limit=TRACK_PAGE_LIMIT,
            params={"additional_types": "track"},
        )
        return _tracks_from_items(raw)

    def get_liked_tracks(self) -> List[TrackRef]:
        raw = self._paginate("/me/tracks", limit=TRACK_PAGE_LIMIT, user_only=True)
        return _tracks_from_items(raw)

    def liked_track_count(self) -> int:
        """Total number of Liked Songs. Returns 0 when it cannot be read."""
        try:
            data = self._api_get(API_BASE + "/me/tracks", {"limit": 1, "offset": 0}, user_only=True)
        except HiresError:
            return 0
        return _int_or_none(data.get("total")) or 0


# ---------------------------------------------------------------------------
# Embed backend
# ---------------------------------------------------------------------------

#: Persisted-query hashes used by Spotify's own web client.
_PERSISTED_QUERIES = {
    "fetchPlaylist": "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77",
}

_GRAPHQL_ENDPOINT = "https://api-partner.spotify.com/pathfinder/v1/query"

#: Any public track works - the embed page is only used to harvest a token.
_TOKEN_SEED_URL = "https://open.spotify.com/embed/track/0VjIjW4GlUZAMYd2vXMi3b"

_EMBED_TOKEN_RE = re.compile(r'"accessToken":"([^"]+)"')
_EMBED_EXPIRY_RE = re.compile(r'"accessTokenExpirationTimestampMs":(\d+)')

#: The anonymous token lives an hour; renew a little earlier than that.
_EMBED_TOKEN_TTL = 50 * 60

_EMBED_PAGE_LIMIT = 100

_EMBED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _embed_track(data: Any) -> Optional[TrackRef]:
    """Map one ``itemV2.data`` node of a playlist, or ``None`` if unusable.

    The GraphQL payload has **no ISRC**, so :attr:`TrackRef.isrc` stays
    ``None`` here and matching falls back to title/artist comparison.
    """
    data = _as_dict(data)
    typename = _text(data.get("__typename"))
    if typename and typename != "Track":
        # Episodes, restricted and removed items live in the same list.
        return None

    uri = _text(data.get("uri"))
    track_id = uri.split(":")[-1] if uri.startswith("spotify:track:") else ""
    if not track_id:
        return None

    artists = [
        _text(_as_dict(_as_dict(a).get("profile")).get("name"))
        for a in _as_list(_as_dict(data.get("artists")).get("items"))
        if _text(_as_dict(_as_dict(a).get("profile")).get("name"))
    ]
    album = _as_dict(data.get("albumOfTrack"))
    cover = _as_list(_as_dict(album.get("coverArt")).get("sources"))
    rating = _text(_as_dict(data.get("contentRating")).get("label")).upper()

    return TrackRef(
        id=track_id,
        title=_text(data.get("name")),
        artists=artists,
        album=_text(album.get("name")),
        duration_sec=_ms_to_sec(_as_dict(data.get("trackDuration")).get("totalMilliseconds")),
        isrc=None,
        explicit=rating == "EXPLICIT",
        url=track_url(track_id),
        image_url=_best_image(cover),
        platform=PLATFORM,
        extra={"uri": uri, "source": "embed"},
    )


def _embed_playlist(data: Any, playlist_id: str) -> PlaylistRef:
    data = _as_dict(data)
    owner = _as_dict(_as_dict(data.get("ownerV2")).get("data"))
    images = [
        source
        for image in _as_list(_as_dict(data.get("images")).get("items"))
        for source in _as_list(_as_dict(image).get("sources"))
    ]
    return PlaylistRef(
        id=playlist_id,
        name=_text(data.get("name")),
        description=_text(data.get("description")),
        owner=_text(owner.get("name")),
        track_count=_int_or_none(_as_dict(data.get("content")).get("totalCount")) or 0,
        image_url=_best_image(images),
        url=playlist_url(playlist_id),
        platform=PLATFORM,
        kind="user",
        extra={"source": "embed"},
    )


class EmbedBackend:
    """Anonymous metadata reader for *public* playlists.

    Needs no credentials at all: an access token is scraped off an embed page
    and used against Spotify's internal GraphQL endpoint.  The trade-off is
    that this route returns no ISRC and cannot see private playlists or Liked
    Songs - it exists purely as the zero-setup fallback.

    Anything unexpected (missing token, GraphQL error, odd payload) is reported
    as :class:`SourceUnavailableError`; no data is ever invented.
    """

    def __init__(
        self,
        *,
        http: Any = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._now = now
        self._token = ""
        self._token_expires_at = 0.0

    @property
    def http(self) -> Any:
        if self._http is None:
            self._http = _default_http()
        return self._http

    # -- token --------------------------------------------------------------
    def get_anonymous_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and self._now() < self._token_expires_at:
            return self._token
        try:
            response = self.http.get(
                _TOKEN_SEED_URL,
                headers={"User-Agent": _EMBED_USER_AGENT},
                params=None,
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            raise SourceUnavailableError(f"Could not load Spotify embed page: {exc}") from exc

        status = _int_or_none(getattr(response, "status_code", None)) or 0
        if status >= 400:
            raise SourceUnavailableError(f"Spotify embed page returned HTTP {status}")

        match = _EMBED_TOKEN_RE.search(getattr(response, "text", "") or "")
        if not match:
            raise SourceUnavailableError("Could not extract an anonymous token from the embed page")

        self._token = match.group(1)
        self._token_expires_at = self._now() + _EMBED_TOKEN_TTL

        # The page states when the token dies; trust it when it is shorter
        # than our guess (and ignore obvious nonsense pointing at the past).
        stated = _EMBED_EXPIRY_RE.search(getattr(response, "text", "") or "")
        if stated:
            deadline = (_float_or_none(stated.group(1)) or 0.0) / 1000.0 - TOKEN_EXPIRY_MARGIN
            if deadline > self._now():
                self._token_expires_at = min(self._token_expires_at, deadline)
        return self._token

    # -- graphql ------------------------------------------------------------
    def _graphql(self, operation: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        query_hash = _PERSISTED_QUERIES.get(operation)
        if not query_hash:
            raise SourceUnavailableError(f"Unknown Spotify GraphQL operation: {operation}")

        payload = {
            "operationName": operation,
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": query_hash}},
        }
        # Spotify can drop an anonymous token before it formally expires, so a
        # rejected token is worth exactly one retry with a freshly scraped one.
        for attempt in range(2):
            token = self.get_anonymous_token(force_refresh=attempt > 0)
            try:
                response = self.http.post(
                    _GRAPHQL_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "User-Agent": _EMBED_USER_AGENT,
                    },
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                raise SourceUnavailableError(f"Spotify GraphQL {operation} failed: {exc}") from exc

            status = _int_or_none(getattr(response, "status_code", None)) or 0
            if status in (401, 403) and attempt == 0:
                log.info("anonymous Spotify token rejected (HTTP %s), refreshing it", status)
                continue
            break

        if status >= 400:
            raise SourceUnavailableError(f"Spotify GraphQL {operation} returned HTTP {status}")

        try:
            body = _as_dict(response.json())
        except Exception as exc:  # noqa: BLE001
            raise SourceUnavailableError(f"Spotify GraphQL {operation} sent no JSON: {exc}") from exc

        if body.get("errors"):
            messages = [
                _text(_as_dict(e).get("message")) or str(e) for e in _as_list(body.get("errors"))
            ]
            raise SourceUnavailableError(f"Spotify GraphQL {operation}: {', '.join(messages)}")
        return _as_dict(body.get("data"))

    def _fetch_playlist(
        self, playlist_id: str, *, all_pages: bool = True
    ) -> Tuple[Dict[str, Any], List[Any]]:
        """Return ``(playlistV2, items)`` for a public playlist.

        ``all_pages=False`` stops after the first page - enough for the
        metadata, and it keeps a 10.000 track playlist from costing 100
        requests just to read its name.
        """
        offset = 0
        first: Optional[Dict[str, Any]] = None
        items: List[Any] = []
        for _ in range(MAX_PAGES):
            data = self._graphql(
                "fetchPlaylist",
                {
                    "uri": f"spotify:playlist:{playlist_id}",
                    "offset": offset,
                    "limit": _EMBED_PAGE_LIMIT,
                    "enableWatchFeedEntrypoint": False,
                },
            )
            playlist = _as_dict(data.get("playlistV2"))
            if not playlist:
                raise SourceUnavailableError(f"Spotify playlist {playlist_id} not found")
            if first is None:
                first = playlist

            content = _as_dict(playlist.get("content"))
            page = _as_list(content.get("items"))
            items.extend(page)
            total = _int_or_none(content.get("totalCount"))
            if (
                not all_pages
                or not page
                or (total is not None and len(items) >= total)
                or len(page) < _EMBED_PAGE_LIMIT
            ):
                break
            offset += len(page)
        return _as_dict(first), items

    # -- public surface -----------------------------------------------------
    def get_playlist(self, playlist_id: str) -> PlaylistRef:
        playlist, items = self._fetch_playlist(playlist_id, all_pages=False)
        ref = _embed_playlist(playlist, str(playlist_id))
        if not ref.track_count:
            ref.track_count = len(items)
        return ref

    def get_playlist_tracks(self, playlist_id: str) -> List[TrackRef]:
        _, items = self._fetch_playlist(playlist_id)
        out: List[TrackRef] = []
        for item in items:
            node = _as_dict(_as_dict(_as_dict(item).get("itemV2")).get("data"))
            track = _embed_track(node)
            if track is not None:
                out.append(track)
        return out


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

class SpotifySource:
    """What the converter talks to: playlists and tracks, ISRC where possible.

    Reads go through the Web API first because it is the only backend that
    returns ISRCs; if it is not configured, not authorized, or simply fails,
    the anonymous embed backend takes over for public playlists.
    """

    def __init__(self, web_backend: Optional[WebApiBackend] = None, embed_backend: Optional[EmbedBackend] = None):
        self.web = web_backend
        self.embed = embed_backend

    @classmethod
    def from_config(
        cls,
        client_id: str = "",
        client_secret: str = "",
        *,
        token_path: Optional[str] = None,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        http: Any = None,
        enable_embed: bool = True,
    ) -> "SpotifySource":
        """Build both backends from the GUI settings."""
        web = WebApiBackend(
            client_id,
            client_secret,
            redirect_uri=redirect_uri,
            token_path=token_path,
            http=http,
        )
        embed = EmbedBackend(http=http) if enable_embed else None
        return cls(web, embed)

    # -- state --------------------------------------------------------------
    def has_web_api(self) -> bool:
        return self.web is not None and self.web.is_configured()

    def is_user_authorized(self) -> bool:
        return self.web is not None and self.web.has_user_token()

    # -- resolving ----------------------------------------------------------
    @staticmethod
    def _playlist_id(playlist: Any) -> str:
        """Accept a :class:`PlaylistRef`, a raw id, a URL or a URI."""
        if isinstance(playlist, PlaylistRef):
            candidate = _text(playlist.id) or _text(playlist.url)
        else:
            candidate = _text(playlist)
        if not candidate:
            raise SourceUnavailableError("No Spotify playlist given")
        if candidate.lower() == LIKED_ID:
            return LIKED_ID

        parsed = parse_spotify_url(candidate)
        if parsed is None:
            raise SourceUnavailableError(f"Not a Spotify playlist: {candidate!r}")
        kind, spotify_id = parsed
        if kind == "liked":
            return LIKED_ID
        if kind != "playlist":
            raise SourceUnavailableError(f"Spotify {kind} is not a playlist: {candidate!r}")
        return spotify_id

    # -- playlists ----------------------------------------------------------
    def list_user_playlists(self) -> List[PlaylistRef]:
        """The user's own playlists, with Liked Songs pinned to the front."""
        if self.web is None or not self.web.has_user_token():
            raise AuthRequiredError("Spotify login required to list your playlists")
        playlists = self.web.list_playlists()
        return [liked_playlist_ref(self.web.liked_track_count()), *playlists]

    def get_playlist(self, playlist: Any) -> PlaylistRef:
        playlist_id = self._playlist_id(playlist)
        if playlist_id == LIKED_ID:
            return liked_playlist_ref(self.web.liked_track_count() if self.web else 0)
        return self._with_fallback(
            "get_playlist", playlist_id, lambda backend: backend.get_playlist(playlist_id)
        )

    def get_playlist_tracks(self, playlist: Any) -> List[TrackRef]:
        playlist_id = self._playlist_id(playlist)
        if playlist_id == LIKED_ID:
            return self.get_liked_tracks()
        return self._with_fallback(
            "get_playlist_tracks", playlist_id, lambda backend: backend.get_playlist_tracks(playlist_id)
        )

    def get_liked_tracks(self) -> List[TrackRef]:
        """Liked Songs. Only the Web API can read these, so no fallback here."""
        if self.web is None or not self.web.has_user_token():
            raise AuthRequiredError("Spotify login required to read your Liked Songs")
        return self.web.get_liked_tracks()

    # -- fallback -----------------------------------------------------------
    def _with_fallback(self, what: str, playlist_id: str, call: Callable[[Any], Any]) -> Any:
        """Web API first, embed second - whichever answers wins."""
        problems: List[str] = []

        if self.has_web_api():
            try:
                return call(self.web)
            except Exception as exc:  # noqa: BLE001 - the fallback exists for exactly this
                problems.append(f"web api: {exc}")
                log.warning("Spotify web API %s for %s failed: %s", what, playlist_id, exc)
        elif self.web is not None:
            problems.append("web api: not configured")

        if self.embed is not None:
            try:
                return call(self.embed)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"embed: {exc}")
                log.warning("Spotify embed %s for %s failed: %s", what, playlist_id, exc)

        detail = "; ".join(problems) if problems else "no Spotify backend configured"
        raise SourceUnavailableError(f"Could not read Spotify playlist {playlist_id} ({detail})")
