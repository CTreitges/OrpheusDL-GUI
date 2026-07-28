"""End-to-end test of the whole hi-res chain.

The per-module tests all fake their neighbours, so they prove each part works
against an *assumed* interface. This file wires the real modules together --
matcher, converter, queue store and the gui.py bridge -- and only fakes the two
genuine outside edges: the Spotify API and the TIDAL API.

If the modules ever drift apart, this is the test that notices.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.controllers import SpotifyImportController, TidalBrowserController, UiDispatcher  # noqa: E402
from hires.converter import PlaylistConverter  # noqa: E402
from hires.integration import HiresRuntime  # noqa: E402
from hires.matcher import TrackMatcher  # noqa: E402
from hires.models import (  # noqa: E402
    MatchDecision,
    PlaylistRef,
    QueueItem,
    QueueStatus,
    TrackRef,
)
from hires.queue_store import QueueStore  # noqa: E402
from hires.tidal_library import TidalLibrary  # noqa: E402

from test_integration import FakeGui, write_audio  # noqa: E402


# ---------------------------------------------------------------------------
# Fake service edges
# ---------------------------------------------------------------------------

class FakeTidalApi:
    """Speaks the shape of modules/tidal/tidal_api.py TidalApi."""

    def __init__(self, catalogue):
        self.catalogue = catalogue  # list of raw TIDAL track dicts
        self.isrc_calls = []
        self.search_calls = []

    class _Session:
        user_id = "user-1"

    def authenticated_session(self):
        return self._Session()

    def get_tracks_by_isrc(self, isrc):
        self.isrc_calls.append(isrc)
        items = [t for t in self.catalogue if t.get("isrc") == isrc]
        if not items:
            return {"items": []}
        return {"items": items}

    def get_search_data(self, term, limit=20):
        self.search_calls.append(term)
        needle = term.lower()
        hits = [
            t
            for t in self.catalogue
            if needle in t["title"].lower()
            or any(needle in a["name"].lower() for a in t.get("artists", []))
            or all(w in f"{t['title']} {' '.join(a['name'] for a in t.get('artists', []))}".lower()
                   for w in needle.split())
        ]
        return {"tracks": {"items": hits[:limit]}}

    def iter_user_playlist_entries(self, user_id):
        return iter([])

    def get_playlist_items(self, playlist_id):
        return {"items": [{"type": "track", "item": t} for t in self.catalogue]}

    def get_playlist(self, playlist_id):
        return {
            "uuid": playlist_id,
            "title": "TIDAL Mix",
            "numberOfTracks": len(self.catalogue),
            "creator": {"name": "Me"},
        }


def tidal_raw(tid, title, artist, duration, isrc=None, version=None):
    data = {
        "id": tid,
        "title": title,
        "duration": duration,
        "artists": [{"name": artist}],
        "album": {"title": "An Album", "cover": "aaaa-bbbb"},
        "explicit": False,
    }
    if isrc:
        data["isrc"] = isrc
    if version:
        data["version"] = version
    return data


class FakeSpotifySource:
    """Speaks the shape of hires.spotify_source.SpotifySource."""

    def __init__(self, playlist, tracks):
        self._playlist = playlist
        self._tracks = tracks

    def list_user_playlists(self):
        return [self._playlist]

    def get_playlist(self, ref):
        return self._playlist

    def get_playlist_tracks(self, ref):
        return list(self._tracks)


def spotify_track(sid, title, artist, duration, isrc=None):
    return TrackRef(
        id=sid,
        title=title,
        artists=[artist],
        duration_sec=duration,
        isrc=isrc,
        platform="Spotify",
    )


# ---------------------------------------------------------------------------
# Fixtures: one small, deliberately awkward catalogue
# ---------------------------------------------------------------------------

@pytest.fixture
def catalogue():
    return [
        # Matches by ISRC.
        tidal_raw("100", "Midnight Drive", "Neon Harbour", 214, isrc="USABC1200001"),
        # Same song, live version -- must NOT be picked for the studio track.
        tidal_raw("101", "Midnight Drive", "Neon Harbour", 265, version="Live"),
        # Matches only by fuzzy search (no ISRC on the Spotify side).
        tidal_raw("200", "Paper Lanterns", "Ivory Coastline", 188),
        # A near-miss that should not win.
        tidal_raw("201", "Paper Planes", "Ivory Coastline", 190),
    ]


@pytest.fixture
def library(catalogue):
    return TidalLibrary(FakeTidalApi(catalogue))


@pytest.fixture
def queue(tmp_path):
    return QueueStore(str(tmp_path / "queue.json"))


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

class TestConversionChain:
    def test_isrc_track_is_matched_exactly_and_queued_in_hi_res(self, library, queue, tmp_path):
        playlist = PlaylistRef(id="pl1", name="Road Trip", platform="Spotify")
        source = FakeSpotifySource(
            playlist, [spotify_track("s1", "Midnight Drive", "Neon Harbour", 214, "USABC1200001")]
        )
        converter = PlaylistConverter(
            source, TrackMatcher(library), queue, output_root=str(tmp_path)
        )

        report, items = converter.convert_and_enqueue(playlist)

        assert report.counts[MatchDecision.AUTO_ACCEPT.value] == 1
        assert len(items) == 1
        item = items[0]
        assert item.url == "https://tidal.com/browse/track/100"
        assert item.quality == "hifi"
        assert item.source["match_method"] == "isrc"
        assert item.output_path == os.path.join(str(tmp_path), "Road Trip")

    def test_live_version_is_not_confused_with_the_studio_take(self, library, queue):
        """The regression that matters most: a Live cut must not pass as the original."""
        playlist = PlaylistRef(id="pl1", name="Mix")
        # No ISRC, so it has to go through fuzzy matching, where both the
        # studio and the live take are plausible candidates.
        source = FakeSpotifySource(
            playlist, [spotify_track("s1", "Midnight Drive", "Neon Harbour", 214)]
        )
        converter = PlaylistConverter(source, TrackMatcher(library), queue)

        report = converter.convert(playlist)
        best = report.results[0].best

        assert best is not None
        assert best.track.id == "100", "picked the Live version over the studio track"

    def test_fuzzy_match_beats_the_near_miss(self, library, queue):
        playlist = PlaylistRef(id="pl1", name="Mix")
        source = FakeSpotifySource(
            playlist, [spotify_track("s1", "Paper Lanterns", "Ivory Coastline", 188)]
        )
        converter = PlaylistConverter(source, TrackMatcher(library), queue)

        report = converter.convert(playlist)

        assert report.results[0].best.track.id == "200"

    def test_unknown_track_is_reported_not_silently_dropped(self, library, queue):
        playlist = PlaylistRef(id="pl1", name="Mix")
        source = FakeSpotifySource(
            playlist, [spotify_track("s9", "Nothing Like This", "Unknown Band", 200)]
        )
        converter = PlaylistConverter(source, TrackMatcher(library), queue)

        report = converter.convert(playlist)
        items = converter.enqueue(report)

        assert report.results[0].decision != MatchDecision.AUTO_ACCEPT.value
        assert items == []  # nothing wrong gets queued

    def test_mixed_playlist_splits_into_matched_and_reviewable(self, library, queue):
        playlist = PlaylistRef(id="pl1", name="Mix")
        source = FakeSpotifySource(
            playlist,
            [
                spotify_track("s1", "Midnight Drive", "Neon Harbour", 214, "USABC1200001"),
                spotify_track("s2", "Paper Lanterns", "Ivory Coastline", 188),
                spotify_track("s3", "Totally Absent", "Nobody At All", 300),
            ],
        )
        converter = PlaylistConverter(source, TrackMatcher(library), queue)

        report = converter.convert(playlist)

        assert report.counts["total"] == 3
        assert report.counts[MatchDecision.AUTO_ACCEPT.value] >= 1
        # Everything is accounted for -- no track vanishes.
        assert sum(
            report.counts[d.value] for d in MatchDecision if d.value in report.counts
        ) == 3


# ---------------------------------------------------------------------------
# Conversion -> queue -> gui.py bridge
# ---------------------------------------------------------------------------

class TestQueueToGuiHandover:
    def test_converted_playlist_downloads_through_the_gui(self, library, queue, tmp_path):
        """Full round trip: Spotify playlist -> queue -> gui.py -> done."""
        playlist = PlaylistRef(id="pl1", name="Road Trip", platform="Spotify")
        source = FakeSpotifySource(
            playlist,
            [
                spotify_track("s1", "Midnight Drive", "Neon Harbour", 214, "USABC1200001"),
                spotify_track("s2", "Paper Lanterns", "Ivory Coastline", 188),
            ],
        )
        out_root = str(tmp_path / "music")
        converter = PlaylistConverter(
            source, TrackMatcher(library), queue, output_root=out_root
        )
        report = converter.convert(playlist)
        queued = converter.enqueue(report, include_review=True)
        assert queued, "nothing was queued, cannot test the handover"

        gui = FakeGui(output_path=out_root)
        runtime = HiresRuntime(gui, queue, logger=lambda _m: None)
        target = os.path.join(out_root, "Road Trip")

        # Drain the queue the way the poller would.
        for _ in range(len(queued)):
            runtime._pump()
            assert gui.calls, "runtime did not hand a URL to gui.py"
            write_audio(target, f"track-{len(gui.calls)}.flac")
            gui.download_process_active = False
            runtime._active_since = 0.0
            runtime._pump()

        assert len(gui.calls) == len(queued)
        assert all(i.status == QueueStatus.DONE.value for i in queue.list())
        # Every dispatch pinned hi-res quality.
        assert all(
            c[2]["extra_kwargs"]["download_quality_override"] == "hifi" for c in gui.calls
        )

    def test_queue_survives_a_restart(self, library, queue, tmp_path):
        playlist = PlaylistRef(id="pl1", name="Road Trip")
        source = FakeSpotifySource(
            playlist, [spotify_track("s1", "Midnight Drive", "Neon Harbour", 214, "USABC1200001")]
        )
        converter = PlaylistConverter(source, TrackMatcher(library), queue)
        converter.convert_and_enqueue(playlist)

        # Simulate a crash mid-download, then a restart.
        gui = FakeGui(output_path=str(tmp_path))
        HiresRuntime(gui, queue, logger=lambda _m: None)._pump()
        assert queue.list()[0].status == QueueStatus.ACTIVE.value

        reopened = QueueStore(queue.path)

        assert len(reopened.list()) == 1
        assert reopened.list()[0].status == QueueStatus.PENDING.value  # re-queued
        assert reopened.list()[0].url == "https://tidal.com/browse/track/100"

    def test_failed_download_is_retryable(self, library, queue, tmp_path):
        queue.add(
            QueueItem(url="https://tidal.com/browse/track/100", title="T", quality="hifi")
        )
        gui = FakeGui(output_path=str(tmp_path / "out"))
        runtime = HiresRuntime(gui, queue, logger=lambda _m: None)

        runtime._pump()
        gui.download_process_active = False
        runtime._active_since = 0.0
        runtime._pump()  # no file appeared -> failed

        item = queue.list()[0]
        assert item.status == QueueStatus.FAILED.value
        assert queue.requeue_failed() == 1
        assert queue.list()[0].status == QueueStatus.PENDING.value


# ---------------------------------------------------------------------------
# Controllers over the real stack
# ---------------------------------------------------------------------------

class TestControllersOverRealStack:
    def test_spotify_controller_converts_and_queues(self, library, queue, tmp_path):
        playlist = PlaylistRef(id="pl1", name="Road Trip")
        source = FakeSpotifySource(
            playlist, [spotify_track("s1", "Midnight Drive", "Neon Harbour", 214, "USABC1200001")]
        )
        controller = SpotifyImportController(
            lambda: source,
            lambda: TrackMatcher(library),
            queue,
            dispatch=UiDispatcher(),
            output_provider=lambda: str(tmp_path),
        )
        done, errors = [], []

        thread = controller.convert(playlist, lambda *a: None, done.append, errors.append)
        thread.join(timeout=5)

        assert errors == []
        items = controller.enqueue_confident_only(done[0])
        assert len(items) == 1
        assert items[0].quality == "hifi"

    def test_tidal_controller_queues_a_playlist(self, library, queue, tmp_path):
        controller = TidalBrowserController(
            lambda: library,
            queue,
            dispatch=UiDispatcher(),
            output_provider=lambda: str(tmp_path),
        )

        item = controller.enqueue_playlist_as_one(
            PlaylistRef(id="pl-abc", name="My TIDAL Mix", platform="TIDAL")
        )

        assert item.url == "https://tidal.com/browse/playlist/pl-abc"
        assert item.quality == "hifi"
        assert queue.list()[0].media_kind == "playlist"

    def test_tidal_controller_queues_individual_tracks(self, library, queue, tmp_path):
        controller = TidalBrowserController(
            lambda: library,
            queue,
            dispatch=UiDispatcher(),
            output_provider=lambda: str(tmp_path),
        )
        tracks = library.get_playlist_tracks("pl-abc")
        assert tracks, "fixture produced no tracks"

        items = controller.enqueue_tracks(PlaylistRef(id="pl-abc", name="My Mix"), tracks)

        assert len(items) == len(tracks)
        assert all(i.url.startswith("https://tidal.com/browse/track/") for i in items)
