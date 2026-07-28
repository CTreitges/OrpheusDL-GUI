"""Tests for hires.tidal_library.

No network: a FakeApi with fixtures shaped like the real TIDAL responses is
injected into TidalLibrary.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires import tidal_library as tl  # noqa: E402
from hires.models import AuthRequiredError, SourceUnavailableError  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures shaped like real TIDAL JSON
# ---------------------------------------------------------------------------

USER_PLAYLIST = {
    "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
    "title": "Nachtfahrt",
    "numberOfTracks": 42,
    "numberOfVideos": 0,
    "description": "Late night driving",
    "duration": 10800,
    "lastUpdated": "2024-11-02T18:22:31.000+0000",
    "created": "2023-05-01T10:11:12.000+0000",
    "type": "USER",
    "publicPlaylist": False,
    "url": "http://www.tidal.com/playlist/aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
    "image": "11111111-2222-3333-4444-555555555555",
    "squareImage": "99999999-8888-7777-6666-555555555555",
    "creator": {"id": 1234567, "name": "Chris"},
}

EDITORIAL_PLAYLIST = {
    "uuid": "cccccccc-3333-4333-8333-cccccccccccc",
    "title": "Hi-Res Essentials",
    "numberOfTracks": 100,
    "description": "",
    "duration": 24000,
    "type": "EDITORIAL",
    "squareImage": "abcdabcd-1234-5678-9012-abcdabcdabcd",
    # Editorial playlists come with an empty creator object.
    "creator": {},
}

FRIEND_PLAYLIST = {
    "uuid": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
    "title": "Friday Bass",
    "numberOfTracks": 7,
    "duration": 1800,
    "type": "USER",
    "squareImage": "deadbeef-0000-1111-2222-333333333333",
    "creator": {"id": 7654321, "name": "Someone Else"},
}

TRACK_ITEM = {
    "type": "track",
    "cut": None,
    "item": {
        "id": 232917499,
        "title": "Blue Monday",
        "version": "2016 Remaster",
        "duration": 448,
        "isrc": "GBAAA8600011",
        "explicit": True,
        "trackNumber": 3,
        "volumeNumber": 2,
        "audioQuality": "LOSSLESS",
        "streamStartDate": "2016-01-01T00:00:00.000+0000",
        "artists": [
            {"id": 12, "name": "New Order", "type": "MAIN"},
            {"id": 13, "name": "Some Remixer", "type": "FEATURED"},
        ],
        "album": {
            "id": 987654,
            "title": "Substance",
            "cover": "12345678-90ab-cdef-1234-567890abcdef",
        },
    },
}

VIDEO_ITEM = {
    "type": "video",
    "item": {"id": 555111, "title": "Blue Monday (Video)", "duration": 460},
}

BROKEN_TRACK_ITEM = {
    "type": "track",
    "item": {
        "id": 777888,
        "title": "Orphan Track",
        # no album, no artists, no isrc, no duration
    },
}


def playlist_entry(playlist, entry_type="USER_CREATED"):
    return {"type": entry_type, "created": "2023-05-01T10:11:12.000+0000", "playlist": playlist}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSession:
    def __init__(self, user_id=98765, country_code="DE"):
        self.user_id = user_id
        self.country_code = country_code


def guest_session():
    """A guest session has no user_id - that is how "not logged in" looks."""
    return FakeSession(user_id=None)


class FakeApi:
    """Duck-typed stand-in for TidalApi. Pages exactly like the real one."""

    PAGE_SIZE = 50

    def __init__(
        self,
        session=None,
        entries=None,
        playlist_items=None,
        playlists=None,
        search=None,
        isrc_result=None,
    ):
        self.session = FakeSession() if session is None else session
        self.entries = list(entries or [])
        self.playlist_items = playlist_items or {}
        self.playlists = playlists or {}
        self.search = search or {}
        self.isrc_result = isrc_result
        self.page_calls = []

    # -- auth
    def authenticated_session(self):
        return self.session

    # -- playlists of the user
    def get_user_playlists_and_favorites(self, user_id, offset=0, limit=50):
        if not self.session:
            raise RuntimeError("User login required to list personal playlists")
        self.page_calls.append((user_id, offset, limit))
        window = self.entries[offset:offset + limit]
        return {
            "limit": limit,
            "offset": offset,
            "totalNumberOfItems": len(self.entries),
            "items": window,
        }

    def iter_user_playlist_entries(self, user_id):
        offset = 0
        while True:
            page = self.get_user_playlists_and_favorites(
                user_id, offset=offset, limit=self.PAGE_SIZE
            )
            items = page.get("items") or []
            for item in items:
                yield item
            offset += len(items)
            if not items or offset >= page.get("totalNumberOfItems", 0):
                break

    # -- single playlist
    def get_playlist(self, playlist_id):
        if playlist_id not in self.playlists:
            return {"status": 404, "error": "Not Found"}
        return self.playlists[playlist_id]

    def get_playlist_items(self, playlist_id):
        if playlist_id not in self.playlist_items:
            raise RuntimeError("404 Not Found")
        items = self.playlist_items[playlist_id]
        return {"limit": 100, "offset": 0, "totalNumberOfItems": len(items), "items": items}

    # -- lookup
    def get_search_data(self, search_term, limit=20):
        return self.search

    def get_tracks_by_isrc(self, isrc):
        if isinstance(self.isrc_result, Exception):
            raise self.isrc_result
        return self.isrc_result


class FakeModule:
    """Stand-in for the TIDAL ModuleInterface (its .session is the TidalApi)."""

    def __init__(self, api):
        self.session = api


class FakeOrpheus:
    """Orpheus instance stub: ``loadable`` modules are installed but not loaded yet."""

    def __init__(self, loaded=None, loadable=None, module_list=None):
        self.loaded_modules = dict(loaded or {})
        self.loadable = dict(loadable or {})
        names = set(self.loaded_modules) | set(self.loadable)
        self.module_list = set(module_list if module_list is not None else names)
        self.loads = []

    def load_module(self, name):
        self.loads.append(name)
        if name in self.loaded_modules:
            return self.loaded_modules[name]
        if name in self.loadable:
            self.loaded_modules[name] = self.loadable[name]
            return self.loaded_modules[name]
        raise Exception(f"{name} is not installed")


def library_with_entries(entries, **kwargs):
    return tl.TidalLibrary(FakeApi(entries=entries, **kwargs))


# ---------------------------------------------------------------------------
# list_playlists
# ---------------------------------------------------------------------------

def test_list_playlists_maps_all_fields():
    lib = library_with_entries([playlist_entry(USER_PLAYLIST)])
    (pl,) = lib.list_playlists()

    assert pl.id == USER_PLAYLIST["uuid"]
    assert pl.name == "Nachtfahrt"
    assert pl.description == "Late night driving"
    assert pl.owner == "Chris"
    assert pl.track_count == 42
    assert pl.duration_sec == 10800
    assert pl.platform == "TIDAL"
    assert pl.kind == "user"
    assert pl.url == f"https://tidal.com/browse/playlist/{USER_PLAYLIST['uuid']}"
    assert pl.image_url == (
        "https://resources.tidal.com/images/99999999/8888/7777/6666/555555555555/640x640.jpg"
    )


def test_list_playlists_marks_favorites_and_keeps_them_by_default():
    lib = library_with_entries([
        playlist_entry(USER_PLAYLIST, "USER_CREATED"),
        playlist_entry(FRIEND_PLAYLIST, "FAVORITE"),
    ])
    kinds = {p.name: p.kind for p in lib.list_playlists()}
    assert kinds == {"Nachtfahrt": "user", "Friday Bass": "favorite"}


def test_list_playlists_can_exclude_favorites():
    lib = library_with_entries([
        playlist_entry(USER_PLAYLIST, "USER_CREATED"),
        playlist_entry(FRIEND_PLAYLIST, "FAVORITE"),
        playlist_entry(EDITORIAL_PLAYLIST, "FAVORITE"),
    ])
    names = [p.name for p in lib.list_playlists(include_favorites=False)]
    assert names == ["Nachtfahrt"]


def test_list_playlists_dedupes_same_uuid():
    lib = library_with_entries([
        playlist_entry(USER_PLAYLIST, "USER_CREATED"),
        playlist_entry(USER_PLAYLIST, "FAVORITE"),
    ])
    playlists = lib.list_playlists()
    assert len(playlists) == 1
    # first occurrence wins
    assert playlists[0].kind == "user"


def test_list_playlists_editorial_owner_falls_back_to_tidal():
    lib = library_with_entries([playlist_entry(EDITORIAL_PLAYLIST, "FAVORITE")])
    (pl,) = lib.list_playlists()
    assert pl.owner == "TIDAL"
    assert pl.kind == "favorite"
    assert pl.description == ""


def test_list_playlists_paginates_over_multiple_pages():
    entries = []
    for i in range(120):
        data = dict(USER_PLAYLIST)
        data["uuid"] = f"uuid-{i:03d}"
        data["title"] = f"Playlist {i}"
        entries.append(playlist_entry(data))
    api = FakeApi(entries=entries)
    playlists = tl.TidalLibrary(api).list_playlists()

    assert len(playlists) == 120
    assert playlists[0].id == "uuid-000"
    assert playlists[-1].id == "uuid-119"
    assert [offset for _, offset, _ in api.page_calls] == [0, 50, 100]


def test_list_playlists_survives_broken_entries():
    lib = library_with_entries([
        {"type": "USER_CREATED", "playlist": None},
        {"type": "FAVORITE"},
        "not-a-dict",
        {"type": "USER_CREATED", "playlist": {"title": "No uuid"}},
        playlist_entry(USER_PLAYLIST),
    ])
    playlists = lib.list_playlists()
    assert [p.name for p in playlists] == ["Nachtfahrt"]


def test_list_playlists_without_session_raises_auth_required():
    lib = tl.TidalLibrary(FakeApi(session=guest_session(), entries=[playlist_entry(USER_PLAYLIST)]))
    with pytest.raises(AuthRequiredError):
        lib.list_playlists()


def test_list_playlists_wraps_api_errors():
    class Boom(FakeApi):
        def iter_user_playlist_entries(self, user_id):
            raise RuntimeError("TIDAL is down")

    with pytest.raises(SourceUnavailableError):
        tl.TidalLibrary(Boom()).list_playlists()


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------

def test_current_user_id_and_is_authenticated():
    lib = tl.TidalLibrary(FakeApi(session=FakeSession(user_id=98765)))
    assert lib.current_user_id() == "98765"
    assert lib.is_authenticated() is True

    guest = tl.TidalLibrary(FakeApi(session=guest_session()))
    assert guest.current_user_id() is None
    assert guest.is_authenticated() is False


def test_current_user_id_when_api_cannot_answer():
    """A missing or exploding authenticated_session() reads as "not logged in"."""

    class NoAuthMethod:
        pass

    class Exploding(FakeApi):
        def authenticated_session(self):
            raise RuntimeError("session storage unreadable")

    assert tl.TidalLibrary(NoAuthMethod()).current_user_id() is None
    assert tl.TidalLibrary(Exploding()).is_authenticated() is False
    # ...and the higher-level call turns that into AuthRequiredError, not a crash.
    with pytest.raises(AuthRequiredError):
        tl.TidalLibrary(Exploding()).list_playlists()


def test_list_playlists_propagates_auth_error_raised_while_iterating():
    """A session that dies mid-iteration must stay an AuthRequiredError."""

    class Expired(FakeApi):
        def iter_user_playlist_entries(self, user_id):
            raise AuthRequiredError("session expired")
            yield  # pragma: no cover - makes this a generator

    with pytest.raises(AuthRequiredError):
        tl.TidalLibrary(Expired()).list_playlists()


# ---------------------------------------------------------------------------
# get_playlist_tracks
# ---------------------------------------------------------------------------

def test_get_playlist_tracks_maps_track_fields():
    api = FakeApi(playlist_items={"pl-1": [TRACK_ITEM]})
    (track,) = tl.TidalLibrary(api).get_playlist_tracks("pl-1")

    assert track.id == "232917499"
    assert track.title == "Blue Monday (2016 Remaster)"
    assert track.artists == ["New Order", "Some Remixer"]
    assert track.album == "Substance"
    assert track.duration_sec == 448
    assert track.isrc == "GBAAA8600011"
    assert track.explicit is True
    assert track.track_number == 3
    assert track.disc_number == 2
    assert track.platform == "TIDAL"
    assert track.url == "https://tidal.com/browse/track/232917499"
    assert track.image_url == (
        "https://resources.tidal.com/images/12345678/90ab/cdef/1234/567890abcdef/640x640.jpg"
    )
    assert track.extra["album_id"] == "987654"
    assert track.extra["version"] == "2016 Remaster"


def test_get_playlist_tracks_skips_non_track_items():
    api = FakeApi(playlist_items={"pl-1": [VIDEO_ITEM, TRACK_ITEM, {"type": "track", "item": None}]})
    tracks = tl.TidalLibrary(api).get_playlist_tracks("pl-1")
    assert [t.id for t in tracks] == ["232917499"]


def test_get_playlist_tracks_tolerates_missing_album_and_artists():
    api = FakeApi(playlist_items={"pl-1": [BROKEN_TRACK_ITEM]})
    (track,) = tl.TidalLibrary(api).get_playlist_tracks("pl-1")

    assert track.id == "777888"
    assert track.title == "Orphan Track"
    assert track.artists == []
    assert track.album == ""
    assert track.image_url == ""
    assert track.isrc is None
    assert track.duration_sec is None
    assert track.explicit is False


def test_get_playlist_tracks_wraps_api_errors():
    with pytest.raises(SourceUnavailableError):
        tl.TidalLibrary(FakeApi()).get_playlist_tracks("missing")


def test_get_playlist_tracks_raises_on_404_payload():
    class Api404(FakeApi):
        def get_playlist_items(self, playlist_id):
            return {"status": 404, "error": "Not Found"}

    with pytest.raises(SourceUnavailableError):
        tl.TidalLibrary(Api404()).get_playlist_tracks("pl-1")


@pytest.mark.parametrize("body", [None, {}, [], "nope"])
def test_get_playlist_tracks_raises_on_unusable_body(body):
    """A falsy/garbled body must not be reported as an empty playlist."""

    class ApiJunk(FakeApi):
        def get_playlist_items(self, playlist_id):
            return body

    with pytest.raises(SourceUnavailableError):
        tl.TidalLibrary(ApiJunk()).get_playlist_tracks("pl-1")


def test_get_playlist_tracks_allows_genuinely_empty_playlist():
    api = FakeApi(playlist_items={"pl-empty": []})
    assert tl.TidalLibrary(api).get_playlist_tracks("pl-empty") == []


# ---------------------------------------------------------------------------
# get_playlist
# ---------------------------------------------------------------------------

def test_get_playlist_returns_playlist_ref():
    api = FakeApi(playlists={EDITORIAL_PLAYLIST["uuid"]: EDITORIAL_PLAYLIST})
    pl = tl.TidalLibrary(api).get_playlist(EDITORIAL_PLAYLIST["uuid"])

    assert pl.name == "Hi-Res Essentials"
    assert pl.owner == "TIDAL"
    assert pl.kind == "editorial"
    assert pl.track_count == 100
    assert pl.url == f"https://tidal.com/browse/playlist/{EDITORIAL_PLAYLIST['uuid']}"


def test_get_playlist_raises_on_404_payload():
    with pytest.raises(SourceUnavailableError):
        tl.TidalLibrary(FakeApi()).get_playlist("nope")


def test_get_playlist_wraps_api_exception():
    class Boom(FakeApi):
        def get_playlist(self, playlist_id):
            raise RuntimeError("TIDAL is down")

    with pytest.raises(SourceUnavailableError):
        tl.TidalLibrary(Boom()).get_playlist("pl-1")


def test_get_playlist_keeps_requested_id_when_payload_has_none():
    """Some payloads omit the uuid - the result must stay addressable."""
    api = FakeApi(playlists={"pl-1": {"title": "No uuid", "numberOfTracks": 3}})
    pl = tl.TidalLibrary(api).get_playlist("pl-1")

    assert pl.id == "pl-1"
    assert pl.url == "https://tidal.com/browse/playlist/pl-1"
    assert pl.name == "No uuid"


def test_get_playlist_tolerates_non_numeric_counts():
    api = FakeApi(playlists={"pl-1": {
        "uuid": "pl-1", "title": "Broken", "numberOfTracks": "many", "duration": None,
    }})
    pl = tl.TidalLibrary(api).get_playlist("pl-1")

    assert pl.track_count == 0
    assert pl.duration_sec is None


# ---------------------------------------------------------------------------
# search / isrc
# ---------------------------------------------------------------------------

def test_search_tracks_reads_nested_items():
    api = FakeApi(search={
        "tracks": {"limit": 20, "totalNumberOfItems": 1, "items": [TRACK_ITEM["item"]]},
        "albums": {"items": []},
    })
    (track,) = tl.TidalLibrary(api).search_tracks("blue monday")
    assert track.id == "232917499"
    assert track.artists == ["New Order", "Some Remixer"]


def test_search_tracks_handles_missing_and_empty_query():
    assert tl.TidalLibrary(FakeApi(search={})).search_tracks("x") == []
    assert tl.TidalLibrary(FakeApi(search={"tracks": None})).search_tracks("x") == []
    assert tl.TidalLibrary(FakeApi()).search_tracks("   ") == []


def test_search_tracks_wraps_api_errors():
    class Boom(FakeApi):
        def get_search_data(self, search_term, limit=20):
            raise RuntimeError("TIDAL is down")

    with pytest.raises(SourceUnavailableError):
        tl.TidalLibrary(Boom()).search_tracks("blue monday")


def test_find_by_isrc_returns_tracks():
    api = FakeApi(isrc_result={"items": [TRACK_ITEM["item"]]})
    (track,) = tl.TidalLibrary(api).find_by_isrc("gbaaa8600011")
    assert track.isrc == "GBAAA8600011"


def test_find_by_isrc_returns_empty_on_error_or_404():
    assert tl.TidalLibrary(FakeApi(isrc_result=RuntimeError("boom"))).find_by_isrc("X") == []
    assert tl.TidalLibrary(FakeApi(isrc_result={"status": 404, "error": "Not Found"})).find_by_isrc("X") == []
    assert tl.TidalLibrary(FakeApi(isrc_result={"items": []})).find_by_isrc("X") == []
    assert tl.TidalLibrary(FakeApi(isrc_result={"items": []})).find_by_isrc("") == []


# ---------------------------------------------------------------------------
# from_orpheus
# ---------------------------------------------------------------------------

def test_from_orpheus_returns_library_for_loaded_module():
    api = FakeApi()
    orpheus = FakeOrpheus(loaded={"tidal": FakeModule(api)})
    lib = tl.TidalLibrary.from_orpheus(orpheus)
    assert isinstance(lib, tl.TidalLibrary)
    assert lib.api is api
    assert orpheus.loads == []  # already loaded, no reload


def test_from_orpheus_loads_module_on_demand():
    api = FakeApi()
    orpheus = FakeOrpheus(loadable={"tidal": FakeModule(api)})
    lib = tl.TidalLibrary.from_orpheus(orpheus)
    assert lib is not None and lib.api is api
    assert orpheus.loads == ["tidal"]


def test_from_orpheus_returns_none_without_tidal_module():
    assert tl.TidalLibrary.from_orpheus(None) is None
    assert tl.TidalLibrary.from_orpheus(FakeOrpheus(loaded={}, module_list=set())) is None
    assert tl.TidalLibrary.from_orpheus(object()) is None
    # module present but load fails
    assert tl.TidalLibrary.from_orpheus(FakeOrpheus(loaded={}, module_list={"tidal"})) is None
    # module object without a .session attribute
    assert tl.TidalLibrary.from_orpheus(FakeOrpheus(loaded={"tidal": object()})) is None


def test_from_orpheus_with_unusable_module_list():
    """module_list is a set on the real Orpheus - anything else must not crash."""

    class WeirdOrpheus:
        loaded_modules = {}
        module_list = 42
        load_module = staticmethod(lambda name: None)

    assert tl.TidalLibrary.from_orpheus(WeirdOrpheus()) is None


def test_from_orpheus_raises_when_not_logged_in():
    orpheus = FakeOrpheus(loaded={"tidal": FakeModule(FakeApi(session=guest_session()))})
    with pytest.raises(AuthRequiredError):
        tl.TidalLibrary.from_orpheus(orpheus)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def test_url_builders():
    assert tl.track_url(123) == "https://tidal.com/browse/track/123"
    assert tl.album_url("456") == "https://tidal.com/browse/album/456"
    assert tl.artist_url(789) == "https://tidal.com/browse/artist/789"
    assert tl.playlist_url("uuid-1") == "https://tidal.com/browse/playlist/uuid-1"


def test_artwork_url_format_and_size_snapping():
    cover = "12345678-90ab-cdef-1234-567890abcdef"
    base = "https://resources.tidal.com/images/12345678/90ab/cdef/1234/567890abcdef"
    assert tl.artwork_url(cover) == f"{base}/640x640.jpg"
    assert tl.artwork_url(cover, size=80) == f"{base}/80x80.jpg"
    assert tl.artwork_url(cover, size=1281) == f"{base}/origin.jpg"
    assert tl.artwork_url(cover, size=1280, max_size=1080) == f"{base}/origin.jpg"
    assert tl.artwork_url(None) == ""
    assert tl.artwork_url("") == ""


def test_artwork_url_falls_back_on_unusable_size():
    cover = "12345678-90ab-cdef-1234-567890abcdef"
    base = "https://resources.tidal.com/images/12345678/90ab/cdef/1234/567890abcdef"
    assert tl.artwork_url(cover, size="big") == f"{base}/640x640.jpg"
    assert tl.artwork_url(cover, size=None) == f"{base}/640x640.jpg"


@pytest.mark.parametrize("url,expected", [
    ("https://listen.tidal.com/album/12345", ("album", "12345")),
    ("https://tidal.com/browse/track/232917499", ("track", "232917499")),
    ("https://tidal.com/track/232917499", ("track", "232917499")),
    ("https://www.tidal.com/browse/playlist/aaaa-bbbb-cccc", ("playlist", "aaaa-bbbb-cccc")),
    ("http://listen.tidal.com/browse/artist/42?u=1", ("artist", "42")),
    ("tidal.com/browse/album/999", ("album", "999")),
    ("  https://tidal.com/track/1  ", ("track", "1")),
    ("Listen to https://tidal.com/browse/track/7 now", ("track", "7")),
    ("https://open.spotify.com/track/abc", None),
    ("https://tidal.com/browse/mix/abc", None),
    # Lookalike hosts must not be accepted just because the scheme is optional.
    ("https://nottidal.com/track/123", None),
    ("https://myfaketidal.com/album/9", None),
    ("https://tidal.com.attacker.net/track/5", None),
    ("https://evil.example/?next=tidal.com/track/1", None),
    ("", None),
    (None, None),
    (12345, None),
])
def test_parse_tidal_url(url, expected):
    assert tl.parse_tidal_url(url) == expected


def test_module_is_tkinter_free():
    with open(tl.__file__, "r", encoding="utf-8") as fh:
        assert "tkinter" not in fh.read()


# ---------------------------------------------------------------------------
# Guest sessions must never read as "logged in"
#
# TidalApi.authenticated_session() picks the first session carrying a user_id,
# and a *guest* session is a fully constructed, authenticated-at-the-API-level
# object -- TidalGuestSession.auth() runs a client_credentials grant and sets
# an access token. The only thing separating it from a real login is that it
# never sets user_id (TidalSession.__init__ leaves it None).
#
# If that ever changed, the Accounts tab would show a green "Signed in" for a
# user who never logged in, and their downloads would silently not be hi-res.
# The fake below mirrors the module's real selection logic rather than handing
# back a session directly, so it fails if the assumption stops holding.
# ---------------------------------------------------------------------------

class SessionCollectionApi:
    """Mirrors TidalApi.authenticated_session() over a dict of sessions."""

    PREFERRED = ("TV", "MOBILE_ATMOS", "MOBILE_DEFAULT")

    def __init__(self, sessions):
        self.sessions = sessions

    def authenticated_session(self):
        for name in self.PREFERRED:
            session = self.sessions.get(name)
            if session and getattr(session, "user_id", None):
                return session
        for session in self.sessions.values():
            if getattr(session, "user_id", None):
                return session
        return None


class GuestOnlySession:
    """What TidalGuestSession looks like after auth(): token, but no user."""

    def __init__(self):
        self.access_token = "guest-token"
        self.country_code = "US"
        self.user_id = None


class TestGuestIsNotAuthenticated:
    def test_a_guest_only_session_reads_as_logged_out(self):
        api = SessionCollectionApi({"GUEST": GuestOnlySession()})
        library = tl.TidalLibrary(api)

        assert library.current_user_id() is None
        assert library.is_authenticated() is False

    def test_a_guest_alongside_a_real_login_still_finds_the_user(self):
        api = SessionCollectionApi(
            {"GUEST": GuestOnlySession(), "TV": FakeSession(user_id=4242)}
        )
        library = tl.TidalLibrary(api)

        assert library.current_user_id() == "4242"
        assert library.is_authenticated() is True

    def test_listing_playlists_as_a_guest_is_refused_not_attempted(self):
        """_require_user_id must stop before the API rejects it."""
        api = SessionCollectionApi({"GUEST": GuestOnlySession()})

        with pytest.raises(AuthRequiredError):
            tl.TidalLibrary(api).list_playlists()
