"""The common playlist provider interface and its four services (B1).

YouTube and SoundCloud run against responses recorded from the real services
on 16.11.2026 (tests/fixtures/providers/, whitelisted fields only -- public
playlist and search data, no token, cookie or account field). Spotify uses the
Web API's documented shapes through the same fake HTTP as test_spotify_source,
TIDAL the library surface the existing tests already fake.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from hires import spotify_source as ss  # noqa: E402
from hires.matcher import TrackMatcher  # noqa: E402
from hires.models import (  # noqa: E402
    AuthRequiredError,
    FolderRef,
    HiresError,
    MatchDecision,
    PlaylistRef,
    RateLimitedError,
    SourceUnavailableError,
    TrackRef,
)
from hires.playlist_providers import (  # noqa: E402
    SERVICES,
    Pacer,
    SpotifyProvider,
    TidalProvider,
    UnavailableProvider,
    port_directions,
    provider_for_url,
    split_artist_title,
)
from hires.soundcloud_source import SoundCloudProvider, track_from_json  # noqa: E402
from hires.youtube_source import (  # noqa: E402
    YouTubeProvider,
    make_ytdlp_extractor,
    playlist_id_from_url,
    track_from_entry,
)
from test_spotify_source import FakeResponse, api_track, make_backend, token_response  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "providers")


@pytest.fixture(autouse=True)
def fresh_soundcloud_client_id():
    """The found public client id is shared per run; tests must not share it."""
    import hires.soundcloud_source as scs

    scs._PUBLIC_CLIENT_ID["value"] = ""
    yield
    scs._PUBLIC_CLIENT_ID["value"] = ""


def fixture(name):
    with open(os.path.join(FIXTURES, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


class NoWait(Pacer):
    """A pacer that counts instead of sleeping."""

    def __init__(self):
        super().__init__(0)
        self.calls = 0

    def wait(self):
        self.calls += 1


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class TestPlaylistProviderInterface:
    def test_four_services_give_twelve_directions(self):
        directions = port_directions()
        assert set(SERVICES) == {"Spotify", "TIDAL", "YouTube", "SoundCloud"}
        assert len(directions) == 12 == len(set(directions))
        assert all(a != b for a, b in directions)

    def test_pacer_keeps_the_interval_without_real_waiting(self):
        now = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        pacer = Pacer(1.0, clock=lambda: now[0], sleep=sleep)
        pacer.wait()  # first call: no wait
        now[0] += 0.25
        pacer.wait()
        now[0] += 5
        pacer.wait()  # long gap: no wait
        assert slept == [pytest.approx(0.75)]

    def test_unavailable_provider_explains_every_refusal(self):
        provider = UnavailableProvider("TIDAL", "Sign in to TIDAL on the Accounts tab first.")
        status = provider.status()
        assert (status.available, status.can_list_own, status.can_search) == (False, False, False)
        for call in (provider.list_folders, provider.get_playlist_tracks, provider.search_tracks):
            with pytest.raises(HiresError, match="Accounts tab"):
                call("x")

    @pytest.mark.parametrize(
        "title, uploader, artists, song",
        [
            ("Daft Punk - Get Lucky (Official Audio) ft. Pharrell", "Daft Punk", ["Daft Punk"], "Get Lucky ft. Pharrell"),
            ("Kygo, Conrad Sewell - Firestone", "Kygo", ["Kygo", "Conrad Sewell"], "Firestone"),
            ("Levels (Radio Edit)", "Avicii - Topic", ["Avicii"], "Levels (Radio Edit)"),
            ("Animals [Official Video]", "MartinGarrixVEVO", ["Martin Garrix"], "Animals"),
            ("Losing It (Live at Coachella)", "", [], "Losing It (Live at Coachella)"),
        ],
    )
    def test_split_artist_title_keeps_version_markers(self, title, uploader, artists, song):
        assert split_artist_title(title, uploader) == (artists, song)

    @pytest.mark.parametrize(
        "channel, artist",
        [
            ("BASTILLEvideos", "BASTILLE"),
            ("Daft Punk - Topic", "Daft Punk"),
            ("MartinGarrixVEVO", "Martin Garrix"),
            ("AviciiOfficialVEVO", "Avicii"),
            ("Dua Lipa Official", "Dua Lipa"),
            ("DuaLipaOfficial", "Dua Lipa"),
            ("Calvin Harris Music", "Calvin Harris"),
            ("RobinSchulzMusic", "Robin Schulz"),
            ("KygoOfficialTV", "Kygo"),
            ("Kraftwerk", "Kraftwerk"),
            ("deadmau5", "deadmau5"),
            ("McFlyVEVO", "McFly"),
            ("The Music", "The Music"),
            ("Video", "Video"),
            ("MUSIC", "MUSIC"),
            ("", ""),
        ],
    )
    def test_channel_name_is_reduced_to_the_artist(self, channel, artist):
        """Channel endings are not part of the artist ("BASTILLEvideos", gui review)."""
        from hires.playlist_providers import clean_channel

        assert clean_channel(channel) == artist

    def test_channel_name_feeds_the_artist_of_an_upload_without_dash(self):
        assert split_artist_title("Pompeii (Official Music Video)", "BASTILLEvideos") == (["BASTILLE"], "Pompeii")

    def test_rate_limit_stops_a_port_instead_of_moving_on(self):
        """A 429 must end the run: every further search makes a block likelier."""
        calls = []

        class Limited:
            def search_tracks(self, query, limit=10):
                calls.append(query)
                raise RateLimitedError("HTTP 429")

        matcher = TrackMatcher(Limited(), target_name="YouTube")
        with pytest.raises(RateLimitedError):
            matcher.match_many([TrackRef(id="1", title="A", artists=["B"]), TrackRef(id="2", title="C")])
        assert len(calls) == 1, "searched on after the service said stop"

    def test_other_search_errors_are_notes_naming_the_target(self):
        class Broken:
            def search_tracks(self, query, limit=10):
                raise SourceUnavailableError("offline")

        result = TrackMatcher(Broken(), target_name="SoundCloud").match(TrackRef(id="1", title="A"))
        assert result.decision == MatchDecision.NO_MATCH.value
        assert "SoundCloud search" in result.note and "no SoundCloud search result" in result.note

    def test_links_are_routed_to_their_service(self):
        providers = [
            SpotifyProvider(object()),
            TidalProvider(object()),
            YouTubeProvider(lambda url: {}),
            SoundCloudProvider(object()),
        ]
        assert provider_for_url(providers, "https://open.spotify.com/playlist/x").service == "Spotify"
        assert provider_for_url(providers, "https://tidal.com/browse/playlist/x").service == "TIDAL"
        assert provider_for_url(providers, "https://youtu.be/abc").service == "YouTube"
        assert provider_for_url(providers, "https://soundcloud.com/a/sets/b").service == "SoundCloud"
        assert provider_for_url(providers, "https://example.com/") is None


# ---------------------------------------------------------------------------
# TIDAL
# ---------------------------------------------------------------------------

UUID = "36ea71a8-445e-41a4-82ab-6628c581535d"


class FakeLibrary:
    def __init__(self):
        self.calls = []

    def list_folders(self, include_favorites=True):
        self.calls.append(("folders", include_favorites))
        return FolderRef(playlists=[PlaylistRef(id=UUID, name="Mine", platform="TIDAL")])

    def get_playlist(self, playlist_id):
        self.calls.append(("playlist", playlist_id))
        return PlaylistRef(id=playlist_id, name="Mine")

    def get_playlist_tracks(self, playlist_id):
        self.calls.append(("tracks", playlist_id))
        return [TrackRef(id="1", title="Song")]

    def search_tracks(self, query, limit=20):
        self.calls.append(("search", query, limit))
        return []

    def find_by_isrc(self, isrc):
        self.calls.append(("isrc", isrc))
        return []


class TestTidalPlaylistProvider:
    def test_a_playlist_link_resolves_to_its_uuid(self):
        library = FakeLibrary()
        provider = TidalProvider(library)
        provider.get_playlist_tracks(f"https://tidal.com/browse/playlist/{UUID}?u")
        assert ("tracks", UUID) in library.calls

    def test_an_album_link_is_not_a_playlist(self):
        with pytest.raises(SourceUnavailableError, match="not a playlist"):
            TidalProvider(FakeLibrary()).get_playlist("https://tidal.com/browse/album/123")

    def test_search_and_isrc_reach_the_library_and_items_stay_hi_res(self):
        library = FakeLibrary()
        pacer = NoWait()
        provider = TidalProvider(library, pacer=pacer)
        provider.search_tracks("daft punk", limit=10)
        provider.find_by_isrc("USQX91300108")
        assert pacer.calls == 2, "TIDAL lookups must be paced"
        assert ("search", "daft punk", 10) in library.calls and ("isrc", "USQX91300108") in library.calls
        assert provider.quality == "hifi"
        assert provider.status().can_list_own

    def test_folders_come_from_the_library(self):
        root = TidalProvider(FakeLibrary()).list_folders(include_favorites=False)
        assert [p.name for p in root.all_playlists()] == ["Mine"]


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

def spotify_with_search(payload, *, client_id="cid"):
    routes = {
        ss.TOKEN_URL: token_response("BQapp"),
        ss.API_BASE + "/search": FakeResponse(payload=payload),
    }
    backend, http = make_backend(routes, client_id=client_id, client_secret="secret" if client_id else "")
    return SpotifyProvider(ss.SpotifySource(backend, None)), http


class TestSpotifyPlaylistProvider:
    def test_search_uses_the_app_token_and_maps_tracks(self):
        provider, http = spotify_with_search({"tracks": {"items": [api_track()]}})
        tracks = provider.search_tracks("rick astley never gonna", limit=50)
        assert [t.title for t in tracks] == ["Never Gonna Give You Up"]
        assert tracks[0].isrc == "GBARL9300135"
        search = [c for c in http.calls if c["url"].endswith("/search")][0]
        assert search["params"]["limit"] == ss.SEARCH_LIMIT, "limit must be capped"
        assert search["headers"]["Authorization"] == "Bearer BQapp"

    def test_isrc_lookup_keeps_only_exact_hits(self):
        payload = {"tracks": {"items": [api_track(isrc="GBARL9300135"), api_track("x2", isrc="OTHER0000001")]}}
        provider, http = spotify_with_search(payload)
        hits = provider.find_by_isrc("gbarl9300135")
        assert [t.isrc for t in hits] == ["GBARL9300135"]
        assert http.calls[-1]["params"]["q"] == "isrc:GBARL9300135"

    def test_without_client_id_links_work_but_search_explains(self):
        provider, _http = spotify_with_search({}, client_id="")
        status = provider.status()
        assert status.available and not status.can_search and not status.can_list_own
        assert "Client ID" in status.hint
        with pytest.raises(AuthRequiredError, match="Client ID"):
            provider.search_tracks("x")
        assert provider.find_by_isrc("GBARL9300135") == []

    def test_own_playlists_need_the_sign_in(self):
        provider, _http = spotify_with_search({})
        assert "Accounts tab" in provider.status().hint
        with pytest.raises(AuthRequiredError):
            provider.list_playlists()


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def youtube(responses, **kwargs):
    seen = []

    def extract(url):
        seen.append(url)
        value = responses(url) if callable(responses) else responses
        if isinstance(value, Exception):
            raise value
        return value

    provider = YouTubeProvider(extract, pacer=NoWait(), **kwargs)
    return provider, seen


class TestYouTubePlaylistProvider:
    def test_recorded_playlist_reads_every_track_with_artist_and_length(self):
        info = fixture("youtube_playlist")
        provider, seen = youtube(info)
        tracks = provider.get_playlist_tracks(f"https://www.youtube.com/watch?v=x&list={info['id']}")
        assert seen == [f"https://www.youtube.com/playlist?list={info['id']}"]
        assert len(tracks) == len(info["entries"])
        assert all(t.artists and t.title and t.duration_sec for t in tracks)
        assert all("official" not in t.title.lower() for t in tracks)

    def test_recorded_search_maps_hits_to_watch_links(self):
        provider, seen = youtube(fixture("youtube_search"))
        tracks = provider.search_tracks("Daft Punk Get Lucky", limit=25)
        assert seen == ["ytsearch10:Daft Punk Get Lucky"], "limit must be capped at 10"
        best = tracks[0]
        assert best.artists == ["Daft Punk"] and best.title.startswith("Get Lucky")
        assert best.url == "https://www.youtube.com/watch?v=5NV6Rdv1a3I"

    def test_gone_videos_are_skipped(self):
        assert track_from_entry({"id": "a", "title": "[Deleted video]"}) is None
        assert track_from_entry({"id": "", "title": "X"}) is None

    def test_bot_check_and_429_stop_everything(self):
        for message in ("HTTP Error 429: Too Many Requests", "Sign in to confirm you're not a bot"):
            provider, _ = youtube(RuntimeError(message))
            with pytest.raises(RateLimitedError):
                provider.search_tracks("x")

    def test_private_playlist_reads_as_not_found(self):
        provider, _ = youtube(RuntimeError("This playlist is private"))
        with pytest.raises(SourceUnavailableError, match="not found"):
            provider.get_playlist("https://www.youtube.com/playlist?list=PLabcdefghij")

    def test_links_without_a_playlist_are_refused(self):
        assert playlist_id_from_url("https://www.youtube.com/watch?v=abc") == ""
        provider, seen = youtube({})
        with pytest.raises(SourceUnavailableError, match="Not a YouTube playlist"):
            provider.get_playlist_tracks("https://www.youtube.com/watch?v=abc")
        assert seen == []

    def test_own_playlists_need_the_cookie_file(self):
        provider, seen = youtube({})
        assert "cookies.txt" in provider.status().hint
        with pytest.raises(AuthRequiredError):
            provider.list_playlists()
        provider, seen = youtube(
            {"entries": [{"url": "https://www.youtube.com/playlist?list=PLmine12345", "title": "Set"}]},
            has_cookies=True,
        )
        assert [p.id for p in provider.list_playlists()] == ["PLmine12345"]

    def test_extractor_hands_yt_dlp_a_copy_of_the_cookie_file(self, tmp_path, monkeypatch):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        seen = {}

        class FakeYDL:
            def __init__(self, options):
                seen["cookiefile"] = options.get("cookiefile")
                seen["exists_during"] = os.path.exists(options.get("cookiefile") or "")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                assert download is False
                return {"id": "x"}

            def sanitize_info(self, info):
                return info

        fake_module = type(sys)("yt_dlp")
        fake_module.YoutubeDL = FakeYDL
        monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)

        assert make_ytdlp_extractor(str(cookies))("ytsearch1:x") == {"id": "x"}
        assert seen["cookiefile"] != str(cookies) and seen["exists_during"]
        assert not os.path.exists(seen["cookiefile"]), "temp copy left behind"
        assert cookies.read_text(encoding="utf-8") == "# Netscape HTTP Cookie File\n"


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

class FakeSoundCloudApi:
    """The surface of the module's SoundCloudWebAPI, answering from fixtures."""

    def __init__(self, *, token="", errors=None):
        self.access_token = token
        self.client_id = "stale"
        self.errors = list(errors or [])
        self.calls = []

    def _maybe_fail(self):
        if self.errors:
            raise Exception(self.errors.pop(0))

    def resolve_url(self, url):
        self.calls.append(("resolve", url))
        self._maybe_fail()
        return fixture("soundcloud_resolve_playlist")

    def search(self, kind, query, limit=10):
        self.calls.append(("search", kind, query, limit, self.client_id))
        self._maybe_fail()
        return fixture("soundcloud_search_tracks")

    def get_tracks_from_tracklist(self, tracks):
        self.calls.append(("tracklist", len(tracks)))
        return {t["id"]: {**t, "title": f"Track {t['id']}", "duration": 1000} for t in tracks}

    def get_me(self):
        self.calls.append(("me",))
        return {"id": 42, "username": "dj"}

    def _get(self, path, params=None):
        self.calls.append(("get", path))
        self._maybe_fail()
        if path.startswith("users/42/playlists_without_albums"):
            return {
                "collection": [{"id": 7, "title": "Warm up", "track_count": 3, "permalink_url": "https://soundcloud.com/dj/sets/warm-up"}],
                "next_href": "https://evil.example.com/steal",
            }
        if path == "tracks":  # api-v2 tracks?ids=: full objects for stub entries
            ids = [i for i in str((params or {}).get("ids", "")).split(",") if i]
            return [{"id": int(i), "title": f"Track {i}", "duration": 1000} for i in ids]
        return fixture("soundcloud_resolve_playlist")


class TestSoundCloudPlaylistProvider:
    def test_recorded_playlist_resolves_and_reads_full_lengths_and_isrc(self):
        api = FakeSoundCloudApi()
        provider = SoundCloudProvider(api, pacer=NoWait())
        url = "https://soundcloud.com/daftpunkofficialmusic/sets/random-access-memories-4"
        playlist = provider.get_playlist(url)
        tracks = provider.get_playlist_tracks(url)
        assert playlist.name == "Random Access Memories" and playlist.kind == "album"
        assert len(tracks) == playlist.track_count
        first = tracks[0]
        assert first.artists == ["Daft Punk"] and first.isrc == "USQX91300101"
        # The recorded stream is a 30 s Go+ preview; the track is 4:35 long.
        assert first.duration_sec == 275

    def test_stub_tracks_are_completed_in_one_batch(self):
        class StubApi(FakeSoundCloudApi):
            def resolve_url(self, url):
                data = fixture("soundcloud_resolve_playlist")
                data["tracks"] = [data["tracks"][0], {"id": 1, "kind": "track"}, {"id": 2, "kind": "track"}]
                return data

        api = StubApi()
        tracks = SoundCloudProvider(api, pacer=NoWait()).get_playlist_tracks("https://soundcloud.com/a/sets/b")
        assert [t.title for t in tracks][1:] == ["Track 1", "Track 2"]
        assert api.calls.count(("get", "tracks")) == 1, "stubs must be fetched in one batch"

    def test_recorded_search_maps_tracks(self):
        api = FakeSoundCloudApi()
        tracks = SoundCloudProvider(api, pacer=NoWait()).search_tracks("daft punk get lucky", limit=50)
        assert tracks and all(t.platform == "SoundCloud" and t.url.startswith("https://soundcloud.com/") for t in tracks)
        assert api.calls[0][3] == 20, "limit must be capped"

    def test_429_stops_without_retrying(self):
        api = FakeSoundCloudApi(errors=["429: Too Many Requests"])
        with pytest.raises(RateLimitedError):
            SoundCloudProvider(api, pacer=NoWait()).search_tracks("x")
        assert len([c for c in api.calls if c[0] == "search"]) == 1

    def test_stale_client_id_is_replaced_once_without_a_token(self):
        api = FakeSoundCloudApi(errors=["Unauthorized (401). Response: secret body"])
        pages = {
            "https://soundcloud.com/": '<script crossorigin src="https://a-v2.sndcdn.com/assets/0-a.js"></script>'
            '<script crossorigin src="https://a-v2.sndcdn.com/assets/49-b.js"></script>',
            "https://a-v2.sndcdn.com/assets/49-b.js": 'x={client_id:"AbCdEfGhIjKlMnOpQrStUvWxYz012345"}',
            "https://a-v2.sndcdn.com/assets/0-a.js": "nothing here",
        }
        fetched = []
        provider = SoundCloudProvider(api, pacer=NoWait(), http_get=lambda u: fetched.append(u) or pages[u])
        assert provider.search_tracks("x")
        assert api.client_id == "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        assert fetched == ["https://soundcloud.com/", "https://a-v2.sndcdn.com/assets/49-b.js"]
        assert api.calls[-1][-1] == api.client_id, "the retry must use the new id"

    def test_a_second_401_is_an_error_without_the_response_body(self):
        api = FakeSoundCloudApi(errors=["Unauthorized (401). Response: secret body"] * 2)
        provider = SoundCloudProvider(api, pacer=NoWait(), http_get=lambda u: "")
        with pytest.raises(AuthRequiredError) as caught:
            provider.search_tracks("x")
        assert "secret body" not in str(caught.value)

    def test_own_playlists_need_a_token_and_stay_on_soundcloud(self):
        provider = SoundCloudProvider(FakeSoundCloudApi(), pacer=NoWait())
        assert "token" in provider.status().hint
        with pytest.raises(AuthRequiredError):
            provider.list_playlists()

        api = FakeSoundCloudApi(token="set")
        playlists = SoundCloudProvider(api, pacer=NoWait()).list_playlists()
        assert [p.name for p in playlists] == ["Warm up"]
        assert not any("evil" in str(c) for c in api.calls), "followed a link off SoundCloud"

    def test_track_objects_without_title_are_skipped(self):
        assert track_from_json({"id": 5, "kind": "track"}) is None
        track = track_from_json({"id": 6, "title": "Kygo - Firestone", "user": {"username": "x"}})
        assert track.artists == ["Kygo"] and track.title == "Firestone"


# ---------------------------------------------------------------------------
# Registry (the real assembly's factories)
# ---------------------------------------------------------------------------

class TestPlaylistProviderRegistry:
    def test_missing_soundcloud_module_becomes_a_hint(self):
        from hires.provider_registry import make_soundcloud_provider
        from test_integration import FakeGui

        gui = FakeGui()

        class Orpheus:
            module_list = ["tidal"]
            loaded_modules = {}

        gui.orpheus_instance = Orpheus()
        status = make_soundcloud_provider(gui)().status()
        assert not status.available and "SoundCloud module" in status.hint

    def test_soundcloud_gets_a_client_with_the_current_token(self):
        from hires.provider_registry import make_soundcloud_provider
        from test_integration import FakeGui

        made = []

        class Api:
            def __init__(self, token, error):
                made.append(token)
                self.access_token = token

        class Module:
            websession = Api.__new__(Api)
            exception = RuntimeError

        class Orpheus:
            module_list = ["soundcloud"]
            loaded_modules = {"soundcloud": Module()}

        gui = FakeGui()
        gui.orpheus_instance = Orpheus()
        gui.current_settings["credentials"]["SoundCloud"] = {"web_access_token": "fresh"}
        provider = make_soundcloud_provider(gui)()
        assert made == ["fresh"] and provider.status().can_list_own

    def test_youtube_cookie_file_enables_own_playlists(self, tmp_path, monkeypatch):
        import importlib.util

        from hires.provider_registry import make_youtube_provider
        from test_integration import FakeGui

        # yt-dlp ships with the app, not with the test venv: only its presence matters here.
        real_find_spec = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda name, *a: object() if name == "yt_dlp" else real_find_spec(name, *a)
        )

        gui = FakeGui()
        gui.application_path = str(tmp_path)
        (tmp_path / "cookies.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        gui.current_settings["credentials"]["YouTube"] = {"cookies_path": "cookies.txt"}
        assert make_youtube_provider(gui)().status().can_list_own

    def test_missing_yt_dlp_becomes_a_hint(self, monkeypatch):
        import importlib.util

        from hires.provider_registry import make_youtube_provider
        from test_integration import FakeGui

        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
        status = make_youtube_provider(FakeGui())().status()
        assert not status.available and "yt-dlp" in status.hint


class TestSoundCloudPlaylistProviderRegressions:
    """Found by the real-window run (drive_part2.py, 16.11.2026)."""

    class PlaylistsByIdNeedLogin(FakeSoundCloudApi):
        def _get(self, path, params=None):
            self.calls.append(("get", path))
            if path.startswith("playlists/"):
                raise Exception("Unauthorized (401). Response: ")
            return super()._get(path, params)

    def test_a_listed_playlist_is_read_through_its_link_not_its_id(self):
        """api-v2 answers playlists/<id> with 401 without a login; resolve works."""
        api = self.PlaylistsByIdNeedLogin()
        provider = SoundCloudProvider(api, pacer=NoWait(), http_get=lambda u: "")
        ref = PlaylistRef(id="243061639", name="RAM", url="https://soundcloud.com/daftpunkofficialmusic/sets/ram")
        assert provider.get_playlist_tracks(ref)
        assert ("resolve", ref.url) in api.calls
        assert not any(c[0] == "get" and c[1].startswith("playlists/") for c in api.calls)

    def test_a_found_client_id_is_reused_by_the_next_provider(self):
        import hires.soundcloud_source as scs

        scs._PUBLIC_CLIENT_ID["value"] = ""
        pages = {
            "https://soundcloud.com/": '<script src="https://a-v2.sndcdn.com/assets/49-b.js"></script>',
            "https://a-v2.sndcdn.com/assets/49-b.js": 'client_id:"AbCdEfGhIjKlMnOpQrStUvWxYz012345"',
        }
        fetched = []
        first = FakeSoundCloudApi(errors=["Unauthorized (401). Response: "])
        SoundCloudProvider(first, pacer=NoWait(), http_get=lambda u: fetched.append(u) or pages[u]).search_tracks("x")
        second = FakeSoundCloudApi()
        SoundCloudProvider(second, pacer=NoWait(), http_get=lambda u: fetched.append(u) or pages[u]).search_tracks("y")
        assert second.calls[0][-1] == "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        assert len(fetched) == 2, "the second provider scraped soundcloud.com again"
        scs._PUBLIC_CLIENT_ID["value"] = ""

    def test_without_a_token_a_401_does_not_blame_the_token(self):
        api = FakeSoundCloudApi(errors=["Unauthorized (401). Response: "] * 2)
        provider = SoundCloudProvider(api, pacer=NoWait(), http_get=lambda u: "")
        with pytest.raises(AuthRequiredError) as caught:
            provider.search_tracks("x")
        assert "token" not in str(caught.value).lower()
