"""Tests for hires.spotify_source.

No network: a FakeHttp session (routes keyed by URL) is injected into both
backends, and the clock/sleep are injected too so token expiry and 429 backoff
are deterministic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires import spotify_source as ss  # noqa: E402
from hires.models import AuthRequiredError, PlaylistRef, SourceUnavailableError  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None, text=""):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("response has no JSON body")
        return self._payload


class FakeHttp:
    """Stands in for a requests.Session; responses are looked up by URL.

    A list value is consumed one response per call (and must not run dry), a
    single response is served for every call to that URL.
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers or {}, "params": params})
        return self._respond(url)

    def post(self, url, headers=None, data=None, json=None, timeout=None):
        self.calls.append(
            {"method": "POST", "url": url, "headers": headers or {}, "data": data, "json": json}
        )
        return self._respond(url)

    def _respond(self, url):
        if url not in self.routes:
            return FakeResponse(payload={"error": {"status": 404}}, status_code=404)
        value = self.routes[url]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"no response left for {url}")
            return value.pop(0)
        return value

    # -- assertions helpers -------------------------------------------------
    def urls(self, method=None):
        return [c["url"] for c in self.calls if method is None or c["method"] == method]

    def count(self, url, method=None):
        return len([u for u in self.urls(method) if u == url])


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.value = float(start)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# Fixtures shaped like real Spotify JSON
# ---------------------------------------------------------------------------

PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
TRACKS_URL = f"{ss.API_BASE}/playlists/{PLAYLIST_ID}/tracks"


def api_track(track_id="4cOdK2wGLETKBW3PvgPWqT", name="Never Gonna Give You Up", isrc="GBARL9300135"):
    return {
        "type": "track",
        "id": track_id,
        "name": name,
        "duration_ms": 213573,
        "explicit": False,
        "track_number": 1,
        "disc_number": 1,
        "uri": f"spotify:track:{track_id}",
        "external_ids": {"isrc": isrc},
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
        "artists": [{"id": "0gxyHStUsqpMadRV0Di1Qt", "name": "Rick Astley", "type": "artist"}],
        "album": {
            "id": "6eUW0wxWtzkFdaEFsTJto6",
            "name": "Whenever You Need Somebody",
            "images": [
                {"url": "https://i.scdn.co/image/large", "width": 640, "height": 640},
                {"url": "https://i.scdn.co/image/small", "width": 64, "height": 64},
            ],
        },
    }


def api_item(track):
    return {"added_at": "2020-01-01T00:00:00Z", "is_local": False, "track": track}


EPISODE_ITEM = {
    "added_at": "2021-02-03T00:00:00Z",
    "track": {
        "type": "episode",
        "id": "512ojhOuo1ktJprKbVcKyQ",
        "name": "Some Podcast Episode",
        "duration_ms": 3600000,
        "external_ids": {},
        "artists": [],
        "album": {},
    },
}

LOCAL_ITEM = {
    "added_at": "2019-05-05T00:00:00Z",
    "is_local": True,
    "track": {
        "type": "track",
        "id": None,
        "name": "Some Local File.mp3",
        "duration_ms": 0,
        "artists": [{"name": ""}],
        "album": {"name": "", "images": []},
        "external_ids": {},
    },
}

NULL_ITEM = {"added_at": "2018-01-01T00:00:00Z", "track": None}

API_PLAYLIST = {
    "id": PLAYLIST_ID,
    "name": "Today's Top Hits",
    "description": "The hottest tracks",
    "uri": f"spotify:playlist:{PLAYLIST_ID}",
    "public": True,
    "owner": {"id": "spotify", "display_name": "Spotify"},
    "images": [{"url": "https://i.scdn.co/image/pl", "width": 300, "height": 300}],
    "tracks": {"total": 50},
    "external_urls": {"spotify": f"https://open.spotify.com/playlist/{PLAYLIST_ID}"},
}

EMBED_PLAYLIST_DATA = {
    "data": {
        "playlistV2": {
            "__typename": "Playlist",
            "uri": f"spotify:playlist:{PLAYLIST_ID}",
            "name": "Today's Top Hits",
            "description": "The hottest tracks",
            "ownerV2": {"data": {"__typename": "User", "name": "Spotify"}},
            "images": {
                "items": [{"sources": [{"url": "https://i.scdn.co/image/embed", "width": 640}]}]
            },
            "content": {
                "totalCount": 3,
                "items": [
                    {
                        "uid": "aaa",
                        "itemV2": {
                            "__typename": "TrackResponseWrapper",
                            "data": {
                                "__typename": "Track",
                                "uri": "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
                                "name": "Never Gonna Give You Up",
                                "trackDuration": {"totalMilliseconds": 213573},
                                "contentRating": {"label": "NONE"},
                                "artists": {"items": [{"profile": {"name": "Rick Astley"}}]},
                                "albumOfTrack": {
                                    "name": "Whenever You Need Somebody",
                                    "coverArt": {
                                        "sources": [{"url": "https://i.scdn.co/image/cover", "width": 640}]
                                    },
                                },
                            },
                        },
                    },
                    {
                        "uid": "bbb",
                        "itemV2": {
                            "__typename": "EpisodeResponseWrapper",
                            "data": {"__typename": "Episode", "uri": "spotify:episode:xyz"},
                        },
                    },
                    {"uid": "ccc", "itemV2": {"__typename": "UnknownType", "data": {}}},
                ],
            },
        }
    }
}

EMBED_PAGE = '<html><script>{"accessToken":"BQanonymous123","accessTokenExpirationTimestampMs":1}</script></html>'


def token_response(access_token="BQaccess", expires_in=3600, refresh_token=None, extra=None):
    payload = {"access_token": access_token, "token_type": "Bearer", "expires_in": expires_in}
    if refresh_token is not None:
        payload["refresh_token"] = refresh_token
    payload.update(extra or {})
    return FakeResponse(payload=payload)


def make_backend(routes, *, client_id="cid", client_secret="secret", token_path=None, clock=None, sleep=None):
    http = FakeHttp(routes)
    backend = ss.WebApiBackend(
        client_id,
        client_secret,
        token_path=token_path,
        http=http,
        sleep=sleep or FakeSleep(),
        now=clock or FakeClock(),
    )
    return backend, http


def authorized_backend(routes, **kwargs):
    """A backend that already holds a (valid) user token."""
    clock = kwargs.pop("clock", None) or FakeClock()
    backend, http = make_backend(routes, clock=clock, **kwargs)
    backend._user_token = {
        "access_token": "BQuser",
        "refresh_token": "REFRESH",
        "expires_at": clock() + 3600,
        "scope": " ".join(ss.DEFAULT_SCOPES),
    }
    return backend, http


# ---------------------------------------------------------------------------
# parse_spotify_url
# ---------------------------------------------------------------------------

def test_parse_plain_playlist_url():
    assert ss.parse_spotify_url(f"https://open.spotify.com/playlist/{PLAYLIST_ID}") == (
        "playlist",
        PLAYLIST_ID,
    )


def test_parse_url_with_locale_prefix_and_query():
    url = f"https://open.spotify.com/intl-de/playlist/{PLAYLIST_ID}?si=abc123&pt=xyz"
    assert ss.parse_spotify_url(url) == ("playlist", PLAYLIST_ID)


def test_parse_embed_and_schemeless_url():
    assert ss.parse_spotify_url(f"open.spotify.com/embed/playlist/{PLAYLIST_ID}") == (
        "playlist",
        PLAYLIST_ID,
    )


def test_parse_legacy_user_url():
    url = f"https://open.spotify.com/user/spotify/playlist/{PLAYLIST_ID}"
    assert ss.parse_spotify_url(url) == ("playlist", PLAYLIST_ID)
    assert ss.parse_spotify_url("https://open.spotify.com/user/spotify") == ("user", "spotify")


def test_parse_uri():
    assert ss.parse_spotify_url(f"spotify:playlist:{PLAYLIST_ID}") == ("playlist", PLAYLIST_ID)


def test_parse_legacy_user_uri_returns_the_playlist():
    assert ss.parse_spotify_url(f"spotify:user:chris:playlist:{PLAYLIST_ID}") == (
        "playlist",
        PLAYLIST_ID,
    )


def test_parse_bare_base62_id_is_treated_as_playlist():
    assert ss.parse_spotify_url(PLAYLIST_ID) == ("playlist", PLAYLIST_ID)


def test_parse_region_qualified_locale_prefix():
    url = f"https://open.spotify.com/intl-pt-br/playlist/{PLAYLIST_ID}"
    assert ss.parse_spotify_url(url) == ("playlist", PLAYLIST_ID)


def test_parse_liked_songs_link_round_trips():
    # liked_playlist_ref() hands out exactly this URL, so it has to parse back.
    assert ss.parse_spotify_url(ss.liked_playlist_ref().url) == ("liked", ss.LIKED_ID)
    assert ss.parse_spotify_url("spotify:collection:tracks") == ("liked", ss.LIKED_ID)
    assert ss.parse_spotify_url("https://open.spotify.com/intl-de/collection/tracks") == (
        "liked",
        ss.LIKED_ID,
    )


def test_parse_other_kinds():
    assert ss.parse_spotify_url("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT") == (
        "track",
        "4cOdK2wGLETKBW3PvgPWqT",
    )
    assert ss.parse_spotify_url("spotify:album:6eUW0wxWtzkFdaEFsTJto6") == (
        "album",
        "6eUW0wxWtzkFdaEFsTJto6",
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "not a link",
        "https://tidal.com/browse/playlist/abc",
        "37i9dQZF1DXcBWIGoYBM5",  # 21 chars - not a base62 id
        12345,
    ],
)
def test_parse_invalid_inputs_return_none(value):
    assert ss.parse_spotify_url(value) is None


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def test_code_verifier_length_is_legal_and_url_safe():
    for wanted in (1, 43, 64, 128, 999):
        verifier = ss.generate_code_verifier(wanted)
        assert 43 <= len(verifier) <= 128
        assert all(c.isalnum() or c in "-._~" for c in verifier)
    assert ss.generate_code_verifier(64) != ss.generate_code_verifier(64)


def test_code_challenge_matches_manual_s256():
    verifier = "abcdefghijklmnopqrstuvwxyz0123456789-._~ABCDEFG"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    challenge = ss.get_code_challenge(verifier)
    assert challenge == expected
    assert "=" not in challenge


def test_build_authorize_url_carries_all_pkce_parameters():
    url = ss.build_authorize_url(
        "cid", "http://127.0.0.1:8888/callback", ss.DEFAULT_SCOPES, "CHALLENGE", state="st"
    )
    assert url.startswith(ss.AUTHORIZE_URL + "?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "code_challenge_method=S256" in url
    assert "code_challenge=CHALLENGE" in url
    assert "state=st" in url
    assert "playlist-read-private" in url and "user-library-read" in url


def test_begin_authorization_uses_the_returned_verifier():
    backend, _ = make_backend({})
    url, verifier = backend.begin_authorization()
    assert ss.get_code_challenge(verifier) in url
    assert 43 <= len(verifier) <= 128


def test_exchange_code_posts_pkce_payload_and_stores_tokens():
    clock = FakeClock()
    backend, http = make_backend(
        {ss.TOKEN_URL: token_response("BQnew", refresh_token="RT")}, clock=clock
    )
    tokens = backend.exchange_code("THECODE", "THEVERIFIER")

    post = http.calls[-1]
    assert post["url"] == ss.TOKEN_URL
    assert post["data"]["grant_type"] == "authorization_code"
    assert post["data"]["code"] == "THECODE"
    assert post["data"]["code_verifier"] == "THEVERIFIER"
    assert tokens["access_token"] == "BQnew"
    assert tokens["refresh_token"] == "RT"
    assert tokens["expires_at"] == clock() + 3600
    assert backend.has_user_token()


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def test_client_credentials_token_is_fetched_once_and_reused():
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        f"{ss.API_BASE}/playlists/{PLAYLIST_ID}": FakeResponse(payload=API_PLAYLIST),
    }
    backend, http = make_backend(routes)

    backend.get_playlist(PLAYLIST_ID)
    backend.get_playlist(PLAYLIST_ID)

    assert http.count(ss.TOKEN_URL, "POST") == 1
    basic = base64.b64encode(b"cid:secret").decode("ascii")
    assert http.calls[0]["headers"]["Authorization"] == f"Basic {basic}"
    assert http.calls[0]["data"] == {"grant_type": "client_credentials"}
    assert http.calls[1]["headers"]["Authorization"] == "Bearer BQapp"


def test_client_credentials_token_is_refetched_after_expiry():
    clock = FakeClock()
    routes = {
        ss.TOKEN_URL: [token_response("BQfirst"), token_response("BQsecond")],
        f"{ss.API_BASE}/playlists/{PLAYLIST_ID}": FakeResponse(payload=API_PLAYLIST),
    }
    backend, http = make_backend(routes, clock=clock)

    backend.get_playlist(PLAYLIST_ID)
    # Still inside the safety margin -> no new token.
    clock.advance(3600 - ss.TOKEN_EXPIRY_MARGIN - 1)
    backend.get_playlist(PLAYLIST_ID)
    assert http.count(ss.TOKEN_URL, "POST") == 1

    clock.advance(2)
    backend.get_playlist(PLAYLIST_ID)
    assert http.count(ss.TOKEN_URL, "POST") == 2
    assert http.calls[-1]["headers"]["Authorization"] == "Bearer BQsecond"


def test_expired_user_token_is_refreshed_before_the_request():
    clock = FakeClock()
    routes = {
        ss.TOKEN_URL: token_response("BQrefreshed"),
        f"{ss.API_BASE}/me/tracks": FakeResponse(payload={"items": [], "next": None, "total": 0}),
    }
    backend, http = make_backend(routes, clock=clock)
    backend._user_token = {
        "access_token": "BQstale",
        "refresh_token": "RT",
        "expires_at": clock() + 10,  # inside the 60s margin -> counts as expired
    }

    backend.get_liked_tracks()

    assert http.calls[0]["url"] == ss.TOKEN_URL
    assert http.calls[0]["data"]["grant_type"] == "refresh_token"
    assert http.calls[0]["data"]["refresh_token"] == "RT"
    assert http.calls[1]["headers"]["Authorization"] == "Bearer BQrefreshed"
    # Spotify did not send a new refresh token, so the old one survives.
    assert backend._user_token["refresh_token"] == "RT"


def test_refresh_without_stored_token_raises_auth_required():
    backend, _ = make_backend({})
    with pytest.raises(AuthRequiredError):
        backend.refresh()


def test_token_file_roundtrip(tmp_path):
    path = str(tmp_path / "spotify_tokens.json")
    backend, _ = make_backend({ss.TOKEN_URL: token_response("BQnew", refresh_token="RT")}, token_path=path)
    backend.exchange_code("CODE", "VERIFIER")

    on_disk = json.loads(open(path, encoding="utf-8").read())
    assert on_disk["user"]["refresh_token"] == "RT"

    reloaded = ss.WebApiBackend("cid", "secret", token_path=path, http=FakeHttp({}))
    assert reloaded.has_user_token()
    assert reloaded._user_token["access_token"] == "BQnew"

    reloaded.forget_user()
    assert not reloaded.has_user_token()
    assert ss.WebApiBackend("cid", "secret", token_path=path, http=FakeHttp({})).has_user_token() is False


def test_broken_token_file_is_ignored(tmp_path):
    path = str(tmp_path / "broken.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    backend = ss.WebApiBackend("cid", "secret", token_path=path, http=FakeHttp({}))
    assert not backend.has_user_token()


# ---------------------------------------------------------------------------
# Playlist tracks via the Web API
# ---------------------------------------------------------------------------

def test_playlist_tracks_follow_three_pages():
    page2 = TRACKS_URL + "?offset=2&limit=2"
    page3 = TRACKS_URL + "?offset=3&limit=2"
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(
            payload={"items": [api_item(api_track("id1", "One")), api_item(api_track("id2", "Two"))],
                     "next": page2}
        ),
        page2: FakeResponse(payload={"items": [api_item(api_track("id3", "Three"))], "next": page3}),
        page3: FakeResponse(payload={"items": [api_item(api_track("id4", "Four"))], "next": None}),
    }
    backend, http = make_backend(routes)

    tracks = backend.get_playlist_tracks(PLAYLIST_ID)

    assert [t.title for t in tracks] == ["One", "Two", "Three", "Four"]
    assert http.urls("GET") == [TRACKS_URL, page2, page3]
    first_params = http.calls[1]["params"]
    assert first_params["limit"] == ss.TRACK_PAGE_LIMIT
    assert first_params["offset"] == 0
    assert first_params["additional_types"] == "track"
    # The next link already carries its own paging.
    assert http.calls[2]["params"] is None


def test_null_track_and_episode_and_local_items_are_skipped():
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(
            payload={
                "items": [NULL_ITEM, api_item(api_track()), EPISODE_ITEM, LOCAL_ITEM, {}, None],
                "next": None,
            }
        ),
    }
    backend, _ = make_backend(routes)

    tracks = backend.get_playlist_tracks(PLAYLIST_ID)

    assert len(tracks) == 1
    assert tracks[0].id == "4cOdK2wGLETKBW3PvgPWqT"


def test_isrc_is_read_from_external_ids_and_uppercased():
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(
            payload={"items": [api_item(api_track(isrc="gbarl9300135"))], "next": None}
        ),
    }
    backend, _ = make_backend(routes)

    track = backend.get_playlist_tracks(PLAYLIST_ID)[0]

    assert track.isrc == "GBARL9300135"
    assert track.platform == ss.PLATFORM
    assert track.duration_sec == 214
    assert track.artists == ["Rick Astley"]
    assert track.album == "Whenever You Need Somebody"
    assert track.image_url.endswith("/large")
    assert track.extra["album_id"] == "6eUW0wxWtzkFdaEFsTJto6"


def test_missing_isrc_and_empty_artists_do_not_crash():
    broken = {
        "type": "track",
        "id": "brokenid",
        "name": "Nameless",
        "artists": [],
        "album": None,
        "external_ids": None,
        "duration_ms": None,
    }
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(payload={"items": [{"track": broken}], "next": None}),
    }
    backend, _ = make_backend(routes)

    track = backend.get_playlist_tracks(PLAYLIST_ID)[0]

    assert track.isrc is None
    assert track.artists == []
    assert track.album == ""
    assert track.duration_sec is None
    assert track.url == "https://open.spotify.com/track/brokenid"


def test_non_finite_numbers_do_not_crash_the_whole_playlist():
    # json.loads() accepts NaN/Infinity, so a broken payload must not take the
    # int() conversion (and with it every other track) down with it.
    broken = dict(api_track("nanid", "Nan Track"))
    broken["duration_ms"] = float("nan")
    worse = dict(api_track("infid", "Inf Track"))
    worse["duration_ms"] = float("inf")
    worse["track_number"] = float("inf")
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(
            payload={"items": [api_item(broken), api_item(worse), api_item(api_track())], "next": None}
        ),
    }
    backend, _ = make_backend(routes)

    tracks = backend.get_playlist_tracks(PLAYLIST_ID)

    assert [t.title for t in tracks] == ["Nan Track", "Inf Track", "Never Gonna Give You Up"]
    assert tracks[0].duration_sec is None
    assert tracks[1].duration_sec is None
    assert tracks[1].track_number is None


def test_offsite_next_link_is_not_followed():
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(
            payload={"items": [api_item(api_track())], "next": "https://evil.example/steal?limit=100"}
        ),
        "https://evil.example/steal?limit=100": FakeResponse(payload={"items": [], "next": None}),
    }
    backend, http = make_backend(routes)

    tracks = backend.get_playlist_tracks(PLAYLIST_ID)

    assert len(tracks) == 1
    # The bearer token must never leave api.spotify.com.
    assert "https://evil.example/steal?limit=100" not in http.urls()


def test_self_referencing_next_link_terminates():
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(payload={"items": [api_item(api_track())], "next": TRACKS_URL}),
    }
    backend, http = make_backend(routes)

    assert len(backend.get_playlist_tracks(PLAYLIST_ID)) == 1
    assert http.count(TRACKS_URL, "GET") == 1


def test_revoked_refresh_token_reports_auth_required_not_outage():
    # Spotify answers invalid_grant with HTTP 400 - that is a login problem,
    # and the GUI has to tell the user to log in again.
    routes = {
        ss.TOKEN_URL: FakeResponse(
            payload={"error": "invalid_grant", "error_description": "Refresh token revoked"},
            status_code=400,
        ),
    }
    backend, _ = make_backend(routes)
    backend._user_token = {"refresh_token": "REVOKED"}

    with pytest.raises(AuthRequiredError):
        backend.refresh()
    with pytest.raises(AuthRequiredError):
        backend.get_playlist(PLAYLIST_ID)


def test_wrong_client_credentials_report_auth_required():
    routes = {ss.TOKEN_URL: FakeResponse(payload={"error": "invalid_client"}, status_code=400)}
    backend, _ = make_backend(routes)
    with pytest.raises(AuthRequiredError):
        backend.get_playlist(PLAYLIST_ID)


def test_liked_track_count_survives_a_failing_request():
    backend, _ = authorized_backend({})  # every URL 404s
    assert backend.liked_track_count() == 0


def test_get_playlist_maps_metadata():
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        f"{ss.API_BASE}/playlists/{PLAYLIST_ID}": FakeResponse(payload=API_PLAYLIST),
    }
    backend, _ = make_backend(routes)

    playlist = backend.get_playlist(PLAYLIST_ID)

    assert playlist.id == PLAYLIST_ID
    assert playlist.name == "Today's Top Hits"
    assert playlist.owner == "Spotify"
    assert playlist.track_count == 50
    assert playlist.platform == ss.PLATFORM
    assert playlist.url.endswith(PLAYLIST_ID)


def test_get_liked_tracks_paginates_over_me_tracks():
    page2 = f"{ss.API_BASE}/me/tracks?offset=1&limit=1"
    routes = {
        f"{ss.API_BASE}/me/tracks": FakeResponse(
            payload={"items": [api_item(api_track("l1", "Liked One"))], "next": page2, "total": 2}
        ),
        page2: FakeResponse(
            payload={"items": [api_item(api_track("l2", "Liked Two"))], "next": None, "total": 2}
        ),
    }
    backend, http = authorized_backend(routes)

    tracks = backend.get_liked_tracks()

    assert [t.title for t in tracks] == ["Liked One", "Liked Two"]
    assert http.calls[0]["headers"]["Authorization"] == "Bearer BQuser"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_429_with_retry_after_is_retried_and_then_succeeds():
    sleep = FakeSleep()
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: [
            FakeResponse(status_code=429, headers={"Retry-After": "7"}),
            FakeResponse(payload={"items": [api_item(api_track())], "next": None}),
        ],
    }
    backend, http = make_backend(routes, sleep=sleep)

    tracks = backend.get_playlist_tracks(PLAYLIST_ID)

    assert len(tracks) == 1
    assert sleep.calls == [7.0]
    assert http.count(TRACKS_URL, "GET") == 2


def test_429_gives_up_after_max_retries():
    sleep = FakeSleep()
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: [FakeResponse(status_code=429, headers={"Retry-After": "2"}) for _ in range(9)],
    }
    backend, http = make_backend(routes, sleep=sleep)

    with pytest.raises(SourceUnavailableError):
        backend.get_playlist_tracks(PLAYLIST_ID)

    assert len(sleep.calls) == ss.MAX_RATE_LIMIT_RETRIES
    assert http.count(TRACKS_URL, "GET") == ss.MAX_RATE_LIMIT_RETRIES + 1


def test_429_without_usable_retry_after_falls_back_to_one_second():
    sleep = FakeSleep()
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: [
            FakeResponse(status_code=429, headers={"Retry-After": "not-a-number"}),
            FakeResponse(payload={"items": [], "next": None}),
        ],
    }
    backend, _ = make_backend(routes, sleep=sleep)

    assert backend.get_playlist_tracks(PLAYLIST_ID) == []
    assert sleep.calls == [1.0]


def test_http_401_becomes_auth_required_and_500_becomes_unavailable():
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(status_code=401),
        f"{ss.API_BASE}/playlists/{PLAYLIST_ID}": FakeResponse(status_code=500),
    }
    backend, _ = make_backend(routes)

    with pytest.raises(AuthRequiredError):
        backend.get_playlist_tracks(PLAYLIST_ID)
    with pytest.raises(SourceUnavailableError):
        backend.get_playlist(PLAYLIST_ID)


def test_transport_error_becomes_source_unavailable():
    class ExplodingHttp:
        def get(self, *args, **kwargs):
            raise OSError("connection reset")

        def post(self, *args, **kwargs):
            raise OSError("connection reset")

    backend = ss.WebApiBackend("cid", "secret", http=ExplodingHttp(), now=FakeClock())
    with pytest.raises(SourceUnavailableError):
        backend.get_playlist(PLAYLIST_ID)


# ---------------------------------------------------------------------------
# Embed backend
# ---------------------------------------------------------------------------

def embed_backend(routes=None):
    routes = routes or {
        ss._TOKEN_SEED_URL: FakeResponse(text=EMBED_PAGE),
        ss._GRAPHQL_ENDPOINT: FakeResponse(payload=EMBED_PLAYLIST_DATA),
    }
    http = FakeHttp(routes)
    return ss.EmbedBackend(http=http, now=FakeClock()), http


def test_embed_backend_reads_public_playlist_without_isrc():
    backend, http = embed_backend()

    tracks = backend.get_playlist_tracks(PLAYLIST_ID)

    # Only the real Track survives; episode and unknown item are skipped.
    assert len(tracks) == 1
    assert tracks[0].title == "Never Gonna Give You Up"
    assert tracks[0].artists == ["Rick Astley"]
    assert tracks[0].duration_sec == 214
    assert tracks[0].isrc is None
    assert tracks[0].extra["source"] == "embed"
    assert http.calls[0]["url"] == ss._TOKEN_SEED_URL
    assert http.calls[1]["headers"]["Authorization"] == "Bearer BQanonymous123"


def test_embed_backend_reuses_the_anonymous_token():
    backend, http = embed_backend()
    backend.get_playlist_tracks(PLAYLIST_ID)
    backend.get_playlist(PLAYLIST_ID)
    assert http.count(ss._TOKEN_SEED_URL, "GET") == 1


def test_embed_backend_maps_playlist_metadata():
    backend, _ = embed_backend()
    playlist = backend.get_playlist(PLAYLIST_ID)
    assert playlist.id == PLAYLIST_ID
    assert playlist.name == "Today's Top Hits"
    assert playlist.owner == "Spotify"
    assert playlist.track_count == 3
    assert playlist.platform == ss.PLATFORM


def embed_track_node(index):
    return {
        "uid": f"u{index}",
        "itemV2": {
            "__typename": "TrackResponseWrapper",
            "data": {
                "__typename": "Track",
                "uri": f"spotify:track:t{index:022d}",
                "name": f"Track {index}",
                "trackDuration": {"totalMilliseconds": 100000},
                "artists": {"items": [{"profile": {"name": "A"}}]},
                "albumOfTrack": {"name": "Album"},
            },
        },
    }


def embed_payload(items, total):
    return FakeResponse(
        payload={
            "data": {
                "playlistV2": {
                    "__typename": "Playlist",
                    "name": "Big One",
                    "content": {"totalCount": total, "items": items},
                }
            }
        }
    )


def test_embed_backend_pages_through_a_long_playlist():
    limit = ss._EMBED_PAGE_LIMIT
    total = 2 * limit + 7
    nodes = [embed_track_node(i) for i in range(total)]
    routes = {
        ss._TOKEN_SEED_URL: FakeResponse(text=EMBED_PAGE),
        ss._GRAPHQL_ENDPOINT: [
            embed_payload(nodes[0:limit], total),
            embed_payload(nodes[limit : 2 * limit], total),
            embed_payload(nodes[2 * limit :], total),
        ],
    }
    http = FakeHttp(routes)
    backend = ss.EmbedBackend(http=http, now=FakeClock())

    tracks = backend.get_playlist_tracks(PLAYLIST_ID)

    assert len(tracks) == total
    assert [t.title for t in tracks] == [f"Track {i}" for i in range(total)]
    offsets = [c["json"]["variables"]["offset"] for c in http.calls if c["method"] == "POST"]
    assert offsets == [0, limit, 2 * limit]


def test_embed_backend_stops_when_a_page_is_short_even_without_total():
    limit = ss._EMBED_PAGE_LIMIT
    nodes = [embed_track_node(i) for i in range(limit + 1)]
    routes = {
        ss._TOKEN_SEED_URL: FakeResponse(text=EMBED_PAGE),
        ss._GRAPHQL_ENDPOINT: [
            embed_payload(nodes[:limit], None),
            embed_payload(nodes[limit:], None),
        ],
    }
    http = FakeHttp(routes)
    backend = ss.EmbedBackend(http=http, now=FakeClock())

    assert len(backend.get_playlist_tracks(PLAYLIST_ID)) == limit + 1
    assert http.count(ss._GRAPHQL_ENDPOINT, "POST") == 2


def test_embed_get_playlist_reads_only_the_first_page():
    limit = ss._EMBED_PAGE_LIMIT
    total = 3 * limit
    nodes = [embed_track_node(i) for i in range(limit)]
    routes = {
        ss._TOKEN_SEED_URL: FakeResponse(text=EMBED_PAGE),
        ss._GRAPHQL_ENDPOINT: [embed_payload(nodes, total)],
    }
    http = FakeHttp(routes)
    backend = ss.EmbedBackend(http=http, now=FakeClock())

    playlist = backend.get_playlist(PLAYLIST_ID)

    assert playlist.track_count == total
    assert http.count(ss._GRAPHQL_ENDPOINT, "POST") == 1


def test_embed_backend_refreshes_a_rejected_anonymous_token_once():
    routes = {
        ss._TOKEN_SEED_URL: [
            FakeResponse(text=EMBED_PAGE),
            FakeResponse(text=EMBED_PAGE.replace("BQanonymous123", "BQfresh")),
        ],
        ss._GRAPHQL_ENDPOINT: [
            FakeResponse(status_code=401),
            FakeResponse(payload=EMBED_PLAYLIST_DATA),
        ],
    }
    http = FakeHttp(routes)
    backend = ss.EmbedBackend(http=http, now=FakeClock())

    tracks = backend.get_playlist_tracks(PLAYLIST_ID)

    assert [t.title for t in tracks] == ["Never Gonna Give You Up"]
    assert http.count(ss._TOKEN_SEED_URL, "GET") == 2
    posts = [c for c in http.calls if c["method"] == "POST"]
    assert posts[0]["headers"]["Authorization"] == "Bearer BQanonymous123"
    assert posts[1]["headers"]["Authorization"] == "Bearer BQfresh"


def test_embed_backend_gives_up_after_one_token_refresh():
    routes = {
        ss._TOKEN_SEED_URL: FakeResponse(text=EMBED_PAGE),
        ss._GRAPHQL_ENDPOINT: [FakeResponse(status_code=401) for _ in range(4)],
    }
    http = FakeHttp(routes)
    backend = ss.EmbedBackend(http=http, now=FakeClock())

    with pytest.raises(SourceUnavailableError):
        backend.get_playlist_tracks(PLAYLIST_ID)
    assert http.count(ss._GRAPHQL_ENDPOINT, "POST") == 2


def test_embed_token_expiry_from_the_page_wins_when_it_is_shorter():
    clock = FakeClock()
    page = (
        '<script>{"accessToken":"BQshortlived",'
        f'"accessTokenExpirationTimestampMs":{int((clock() + 120) * 1000)}'
        "}</script>"
    )
    routes = {
        ss._TOKEN_SEED_URL: [FakeResponse(text=page), FakeResponse(text=page)],
        ss._GRAPHQL_ENDPOINT: FakeResponse(payload=EMBED_PLAYLIST_DATA),
    }
    http = FakeHttp(routes)
    backend = ss.EmbedBackend(http=http, now=clock)

    backend.get_playlist_tracks(PLAYLIST_ID)
    clock.advance(90)  # past the page's deadline, far inside the 50 min default
    backend.get_playlist_tracks(PLAYLIST_ID)

    assert http.count(ss._TOKEN_SEED_URL, "GET") == 2


def test_embed_backend_reports_graphql_errors():
    backend, _ = embed_backend(
        {
            ss._TOKEN_SEED_URL: FakeResponse(text=EMBED_PAGE),
            ss._GRAPHQL_ENDPOINT: FakeResponse(payload={"errors": [{"message": "nope"}]}),
        }
    )
    with pytest.raises(SourceUnavailableError):
        backend.get_playlist_tracks(PLAYLIST_ID)


def test_embed_backend_without_token_on_page_raises():
    backend, _ = embed_backend({ss._TOKEN_SEED_URL: FakeResponse(text="<html>nothing</html>")})
    with pytest.raises(SourceUnavailableError):
        backend.get_playlist_tracks(PLAYLIST_ID)


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

def test_list_user_playlists_without_oauth_raises_auth_required():
    backend, _ = make_backend({})  # client credentials only
    source = ss.SpotifySource(backend, None)
    assert source.has_web_api() is True
    assert source.is_user_authorized() is False
    with pytest.raises(AuthRequiredError):
        source.list_user_playlists()

    with pytest.raises(AuthRequiredError):
        ss.SpotifySource(None, None).list_user_playlists()


def test_liked_songs_entry_comes_first():
    routes = {
        f"{ss.API_BASE}/me/playlists": FakeResponse(
            payload={"items": [API_PLAYLIST], "next": None}
        ),
        f"{ss.API_BASE}/me/tracks": FakeResponse(payload={"items": [], "next": None, "total": 123}),
    }
    backend, _ = authorized_backend(routes)
    source = ss.SpotifySource(backend, None)

    playlists = source.list_user_playlists()

    assert playlists[0].id == ss.LIKED_ID
    assert playlists[0].kind == "liked"
    assert playlists[0].track_count == 123
    assert playlists[1].id == PLAYLIST_ID


def test_liked_playlist_resolves_to_liked_tracks():
    routes = {
        f"{ss.API_BASE}/me/tracks": FakeResponse(
            payload={"items": [api_item(api_track("l1", "Liked One"))], "next": None, "total": 1}
        ),
    }
    backend, _ = authorized_backend(routes)
    source = ss.SpotifySource(backend, None)

    tracks = source.get_playlist_tracks(ss.liked_playlist_ref())

    assert [t.title for t in tracks] == ["Liked One"]


def test_liked_songs_url_resolves_to_liked_tracks():
    routes = {
        f"{ss.API_BASE}/me/tracks": FakeResponse(
            payload={"items": [api_item(api_track("l1", "Liked One"))], "next": None, "total": 1}
        ),
    }
    backend, _ = authorized_backend(routes)
    source = ss.SpotifySource(backend, None)

    tracks = source.get_playlist_tracks("https://open.spotify.com/collection/tracks")

    assert [t.title for t in tracks] == ["Liked One"]
    assert source.get_playlist("spotify:collection:tracks").id == ss.LIKED_ID


def test_liked_tracks_require_user_login():
    backend, _ = make_backend({})
    with pytest.raises(AuthRequiredError):
        ss.SpotifySource(backend, None).get_liked_tracks()


def test_web_api_failure_falls_back_to_embed():
    web, web_http = make_backend(
        {
            ss.TOKEN_URL: token_response("BQapp"),
            TRACKS_URL: FakeResponse(status_code=500),
        }
    )
    embed, embed_http = embed_backend()
    source = ss.SpotifySource(web, embed)

    tracks = source.get_playlist_tracks(f"https://open.spotify.com/playlist/{PLAYLIST_ID}")

    assert [t.title for t in tracks] == ["Never Gonna Give You Up"]
    assert web_http.count(TRACKS_URL, "GET") == 1
    assert embed_http.count(ss._GRAPHQL_ENDPOINT, "POST") == 1


def test_unconfigured_web_backend_goes_straight_to_embed():
    web, web_http = make_backend({}, client_id="", client_secret="")
    embed, _ = embed_backend()
    source = ss.SpotifySource(web, embed)

    assert source.has_web_api() is False
    assert len(source.get_playlist_tracks(PLAYLIST_ID)) == 1
    assert web_http.calls == []


def test_no_backend_configured_raises_source_unavailable():
    source = ss.SpotifySource()
    with pytest.raises(SourceUnavailableError):
        source.get_playlist_tracks(PLAYLIST_ID)
    with pytest.raises(SourceUnavailableError):
        source.get_playlist(PLAYLIST_ID)


def test_both_backends_failing_raises_with_both_reasons():
    web, _ = make_backend({ss.TOKEN_URL: token_response("BQapp"), TRACKS_URL: FakeResponse(status_code=500)})
    embed, _ = embed_backend({ss._TOKEN_SEED_URL: FakeResponse(status_code=503)})
    source = ss.SpotifySource(web, embed)

    with pytest.raises(SourceUnavailableError) as excinfo:
        source.get_playlist_tracks(PLAYLIST_ID)
    message = str(excinfo.value)
    assert "web api" in message and "embed" in message


def test_source_accepts_playlist_ref_url_and_uri():
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        TRACKS_URL: FakeResponse(payload={"items": [api_item(api_track())], "next": None}),
    }
    backend, _ = make_backend(routes)
    source = ss.SpotifySource(backend, None)

    ref = PlaylistRef(id=PLAYLIST_ID, name="Today's Top Hits", platform=ss.PLATFORM)
    for value in (
        ref,
        PLAYLIST_ID,
        f"spotify:playlist:{PLAYLIST_ID}",
        f"https://open.spotify.com/intl-de/playlist/{PLAYLIST_ID}?si=1",
    ):
        assert len(source.get_playlist_tracks(value)) == 1


def test_source_rejects_non_playlist_input():
    backend, _ = make_backend({})
    source = ss.SpotifySource(backend, None)
    with pytest.raises(SourceUnavailableError):
        source.get_playlist_tracks("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
    with pytest.raises(SourceUnavailableError):
        source.get_playlist_tracks("")


def test_from_config_builds_both_backends():
    source = ss.SpotifySource.from_config("cid", "secret", http=FakeHttp({}))
    assert isinstance(source.web, ss.WebApiBackend)
    assert isinstance(source.embed, ss.EmbedBackend)
    assert source.has_web_api() is True

    without_embed = ss.SpotifySource.from_config(http=FakeHttp({}), enable_embed=False)
    assert without_embed.embed is None
    assert without_embed.has_web_api() is False


def test_module_imports_without_tkinter():
    """Importing the module must not pull in tkinter.

    Checked in a subprocess: ``sys.modules`` is process-global, so once the
    widget tests have run in the same session tkinter is loaded regardless of
    what this module does.
    """
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; sys.path.insert(0, %r);"
        "import hires.spotify_source;"
        "assert 'tkinter' not in sys.modules, sorted(m for m in sys.modules if 'tk' in m);"
        "print('clean')" % repo
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# ---------------------------------------------------------------------------
# Local OAuth callback server
# ---------------------------------------------------------------------------

def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_callback(redirect_uri, state, query, timeout=15.0):
    """Start the callback server, hit it once, return {"code"|"error"}.

    Readiness is signalled by the server_factory seam instead of a sleep:
    HTTPServer.__init__ binds *and* listens, and on macOS its server_bind()
    reverse-resolves the host, which is slow enough on CI that a fixed sleep
    raced the request.
    """
    import threading
    import urllib.error
    import urllib.request

    out = {}
    listening = threading.Event()

    def factory(address, handler):
        # Wrap the production factory so the test exercises the real bind path
        # (which deliberately skips the slow reverse-DNS lookup on macOS).
        server = ss.make_callback_server(address, handler)
        listening.set()
        return server

    def serve():
        try:
            out["code"] = ss.wait_for_authorization_code(
                redirect_uri, expected_state=state, timeout=timeout,
                server_factory=factory,
            )
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc
        finally:
            listening.set()  # never leave the caller hanging

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert listening.wait(timeout), "server never started listening"

    try:
        urllib.request.urlopen(f"{redirect_uri}?{query}", timeout=timeout).read()
    except urllib.error.HTTPError:
        pass  # the 400 page on failure is expected
    thread.join(timeout=timeout)
    return out


def test_callback_server_returns_the_code():
    uri = f"http://127.0.0.1:{_free_port()}/callback"
    out = _run_callback(uri, "STATE123", "code=THECODE&state=STATE123")
    assert out.get("code") == "THECODE"


def test_callback_server_rejects_a_mismatched_state():
    """Without this check any page could feed us a code."""
    uri = f"http://127.0.0.1:{_free_port()}/callback"
    out = _run_callback(uri, "STATE123", "code=THECODE&state=WRONG")
    assert isinstance(out.get("error"), AuthRequiredError)
    assert "state" in str(out["error"]).lower()
    assert "code" not in out


def test_callback_server_reports_a_denial():
    uri = f"http://127.0.0.1:{_free_port()}/callback"
    out = _run_callback(uri, "STATE123", "error=access_denied&state=STATE123")
    assert isinstance(out.get("error"), AuthRequiredError)
    assert "access_denied" in str(out["error"])


def test_callback_server_times_out_without_a_request():
    """Nothing arriving is almost always an unregistered redirect URI.

    A bare "timed out" left the user with nothing to act on, which is how the
    sign-in appeared simply broken; the message now names the URI to register.
    """
    uri = f"http://127.0.0.1:{_free_port()}/callback"
    with pytest.raises(AuthRequiredError) as excinfo:
        ss.wait_for_authorization_code(uri, expected_state="S", timeout=1.0)

    message = str(excinfo.value)
    assert uri in message
    assert "developer.spotify.com" in message


def test_callback_server_reports_an_unusable_port():
    """A bind failure must explain itself rather than hang.

    Driven through the server_factory seam: Windows lets two sockets share a
    port via SO_REUSEADDR, so a real occupied port cannot produce this state
    everywhere. What matters is that OSError becomes a readable message.
    """

    def refuses(_address, _handler):
        raise OSError(48, "Address already in use")

    with pytest.raises(AuthRequiredError, match="Cannot listen") as excinfo:
        ss.wait_for_authorization_code(
            "http://127.0.0.1:8888/callback",
            expected_state="S",
            timeout=2.0,
            server_factory=refuses,
        )
    assert "8888" in str(excinfo.value)
    assert "Address already in use" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The redirect URI
#
# Spotify only redirects back to URIs the app itself lists. When the one we
# listen on is not among them, Spotify stops on an error page after login and
# nothing ever arrives -- so the app waited five minutes and then reported a
# bare timeout. That was the whole of "the Spotify login doesn't work".
# ---------------------------------------------------------------------------

class TestRedirectUri:
    def test_the_default_matches_what_the_setup_panel_hands_out(self):
        """These two drifting apart is exactly what broke sign-in.

        gui.py's Spotify panel offers a redirect URI to copy into the Spotify
        dashboard. If we listen somewhere else, following that panel produces
        an app that can never complete a sign-in.
        """
        import re

        assert ss.DEFAULT_REDIRECT_URI == ss.SETUP_PANEL_REDIRECT_URI

        gui_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui.py"
        )
        with open(gui_py, encoding="utf-8") as handle:
            source = handle.read()
        offered = set(re.findall(r'_sp_redirect_uri\s*=\s*"([^"]+)"', source))

        assert offered, "gui.py no longer offers a redirect URI to copy"
        assert ss.DEFAULT_REDIRECT_URI in offered, (
            f"we listen on {ss.DEFAULT_REDIRECT_URI} but the setup panel tells "
            f"people to register {offered} -- sign-in cannot work"
        )

    def test_it_is_a_loopback_address(self):
        """Spotify dropped plain-HTTP redirects except on loopback IPs."""
        assert ss.DEFAULT_REDIRECT_URI.startswith("http://127.0.0.1")
        assert "localhost" not in ss.DEFAULT_REDIRECT_URI

    def test_a_silent_timeout_names_the_uri_to_register(self):
        """The failure has to explain itself; the old one could not be acted on."""

        class NeverCalled:
            def __init__(self, *_a, **_k):
                self.timeout = 1.0

            def handle_request(self):
                pass

            def server_close(self):
                pass

        with pytest.raises(ss.AuthRequiredError) as excinfo:
            ss.wait_for_authorization_code(
                "http://127.0.0.1:4381/login",
                expected_state="s",
                timeout=1.0,
                server_factory=lambda *_a, **_k: NeverCalled(),
            )

        message = str(excinfo.value)
        assert "http://127.0.0.1:4381/login" in message, "the URI is the actionable part"
        assert "developer.spotify.com" in message

    def test_a_real_denial_still_reports_itself(self):
        """A refusal from Spotify must not be relabelled as a URI problem.

        Driven through a real request, the same way ``_run_callback`` does it
        further up -- a fake server cannot reach the handler's outcome dict, so
        faking this would only test the fake.
        """
        uri = f"http://127.0.0.1:{_free_port()}/login"
        out = _run_callback(uri, "STATE123", "error=access_denied&state=STATE123")

        assert "access_denied" in str(out["error"])
        assert "developer.spotify.com" not in str(out["error"])
