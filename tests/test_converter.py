"""Tests for hires.converter."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.converter import (  # noqa: E402
    PlaylistConverter,
    apply_review,
    queue_items_for_tracks,
    report_summary,
    sanitize_folder_name,
    save_report,
    unmatched_lines,
)
from hires.models import (  # noqa: E402
    ConversionReport,
    MatchCandidate,
    MatchDecision,
    MatchResult,
    PlaylistRef,
    SourceUnavailableError,
    TrackRef,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def sp_track(tid, title="Song", artist="Artist", isrc=None):
    return TrackRef(id=tid, title=title, artists=[artist], isrc=isrc, platform="Spotify")


def td_track(tid, title="Song", artist="Artist"):
    return TrackRef(
        id=tid,
        title=title,
        artists=[artist],
        url=f"https://tidal.com/browse/track/{tid}",
        platform="TIDAL",
    )


class FakeSpotify:
    def __init__(self, playlist, tracks):
        self._playlist = playlist
        self._tracks = tracks
        self.calls = []

    def get_playlist(self, ref):
        self.calls.append(("get_playlist", ref))
        return self._playlist

    def get_playlist_tracks(self, ref):
        self.calls.append(("get_playlist_tracks", ref))
        return list(self._tracks)


class FakeMatcher:
    def __init__(self, results):
        self._results = results
        self.progress_seen = []

    def match_many(self, sources, progress_callback=None):
        if progress_callback:
            for i, r in enumerate(self._results, 1):
                progress_callback(i, len(self._results), r)
                self.progress_seen.append(i)
        return list(self._results)


class FakeQueue:
    def __init__(self):
        self.items = []

    def add_many(self, items):
        self.items.extend(items)
        return list(items)


def accepted(source, target, score=0.99, method="isrc"):
    return MatchResult(
        source=source,
        best=MatchCandidate(track=target, score=score, method=method),
        decision=MatchDecision.AUTO_ACCEPT.value,
    )


def needs_review(source, target, score=0.7):
    return MatchResult(
        source=source,
        best=MatchCandidate(track=target, score=score, method="fuzzy"),
        decision=MatchDecision.NEEDS_REVIEW.value,
    )


def no_match(source):
    return MatchResult(source=source, best=None, decision=MatchDecision.NO_MATCH.value)


@pytest.fixture
def playlist():
    return PlaylistRef(id="pl1", name="My Mix", platform="Spotify", track_count=3)


# ---------------------------------------------------------------------------
# sanitize_folder_name
# ---------------------------------------------------------------------------

class TestSanitizeFolderName:
    def test_keeps_normal_names(self):
        assert sanitize_folder_name("My Mix 2024") == "My Mix 2024"

    def test_replaces_illegal_characters(self):
        assert sanitize_folder_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_strips_control_characters(self):
        assert "\x00" not in sanitize_folder_name("bad\x00name")

    def test_collapses_whitespace(self):
        assert sanitize_folder_name("a   b\t c") == "a b c"

    def test_strips_trailing_dots_and_spaces(self):
        # Windows silently drops these, which breaks path round-trips.
        assert sanitize_folder_name("name. . ") == "name"

    def test_windows_reserved_names_get_fallback(self):
        assert sanitize_folder_name("CON") == "Playlist"
        assert sanitize_folder_name("com1") == "Playlist"

    def test_empty_gets_fallback(self):
        assert sanitize_folder_name("") == "Playlist"
        assert sanitize_folder_name("   ", fallback="X") == "X"

    def test_truncates_long_names(self):
        out = sanitize_folder_name("a" * 400, max_length=50)
        assert len(out) <= 50

    def test_unicode_survives(self):
        assert sanitize_folder_name("Grüße & Küsse") == "Grüße & Küsse"


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------

class TestConvert:
    def test_builds_report_from_matcher(self, playlist):
        s1, s2 = sp_track("s1"), sp_track("s2")
        results = [accepted(s1, td_track("t1")), no_match(s2)]
        conv = PlaylistConverter(FakeSpotify(playlist, [s1, s2]), FakeMatcher(results), FakeQueue())

        report = conv.convert(playlist)

        assert report.playlist is playlist
        assert len(report.results) == 2
        assert report.finished_at is not None
        assert report.counts["total"] == 2
        assert report.counts[MatchDecision.AUTO_ACCEPT.value] == 1
        assert report.counts[MatchDecision.NO_MATCH.value] == 1

    def test_resolves_a_url_into_a_playlist_ref(self, playlist):
        spotify = FakeSpotify(playlist, [sp_track("s1")])
        conv = PlaylistConverter(spotify, FakeMatcher([accepted(sp_track("s1"), td_track("t1"))]), FakeQueue())

        report = conv.convert("https://open.spotify.com/playlist/pl1")

        assert report.playlist is playlist
        assert ("get_playlist", "https://open.spotify.com/playlist/pl1") in spotify.calls

    def test_playlist_ref_skips_the_lookup(self, playlist):
        spotify = FakeSpotify(playlist, [sp_track("s1")])
        conv = PlaylistConverter(spotify, FakeMatcher([accepted(sp_track("s1"), td_track("t1"))]), FakeQueue())

        conv.convert(playlist)

        assert not any(c[0] == "get_playlist" for c in spotify.calls)

    def test_empty_playlist_raises(self, playlist):
        conv = PlaylistConverter(FakeSpotify(playlist, []), FakeMatcher([]), FakeQueue())
        with pytest.raises(SourceUnavailableError):
            conv.convert(playlist)

    def test_progress_callback_is_forwarded(self, playlist):
        s1 = sp_track("s1")
        matcher = FakeMatcher([accepted(s1, td_track("t1"))])
        conv = PlaylistConverter(FakeSpotify(playlist, [s1]), matcher, FakeQueue())

        seen = []
        conv.convert(playlist, progress_callback=lambda d, t, r: seen.append((d, t)))

        assert seen == [(1, 1)]

    def test_convert_does_not_queue_anything(self, playlist):
        s1 = sp_track("s1")
        queue = FakeQueue()
        conv = PlaylistConverter(FakeSpotify(playlist, [s1]), FakeMatcher([accepted(s1, td_track("t1"))]), queue)

        conv.convert(playlist)

        assert queue.items == []


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------

class TestEnqueue:
    def _converter(self, playlist, results, **kwargs):
        sources = [r.source for r in results]
        return PlaylistConverter(
            FakeSpotify(playlist, sources), FakeMatcher(results), FakeQueue(), **kwargs
        )

    def test_queues_only_accepted_by_default(self, playlist):
        results = [
            accepted(sp_track("s1"), td_track("t1")),
            needs_review(sp_track("s2"), td_track("t2")),
            no_match(sp_track("s3")),
        ]
        conv = self._converter(playlist, results)
        report = conv.convert(playlist)

        items = conv.enqueue(report)

        assert len(items) == 1
        assert items[0].source["tidal_track_id"] == "t1"

    def test_include_review_adds_the_uncertain_ones(self, playlist):
        results = [
            accepted(sp_track("s1"), td_track("t1")),
            needs_review(sp_track("s2"), td_track("t2")),
            no_match(sp_track("s3")),
        ]
        conv = self._converter(playlist, results)
        report = conv.convert(playlist)

        items = conv.enqueue(report, include_review=True)

        assert {i.source["tidal_track_id"] for i in items} == {"t1", "t2"}

    def test_items_are_pinned_to_hi_res(self, playlist):
        results = [accepted(sp_track("s1"), td_track("t1"))]
        conv = self._converter(playlist, results)
        items = conv.enqueue(conv.convert(playlist))

        assert items[0].quality == "hifi"
        assert items[0].platform == "TIDAL"
        assert items[0].url == "https://tidal.com/browse/track/t1"

    def test_item_records_the_match_provenance(self, playlist):
        source = sp_track("s1", title="Original", artist="Band", isrc="US1234567890")
        results = [accepted(source, td_track("t1"), score=1.0, method="isrc")]
        conv = self._converter(playlist, results)
        items = conv.enqueue(conv.convert(playlist))

        src = items[0].source
        assert src["kind"] == "spotify_convert"
        assert src["spotify_track_id"] == "s1"
        assert src["match_method"] == "isrc"
        assert src["match_score"] == 1.0

    def test_all_items_share_one_group(self, playlist):
        results = [
            accepted(sp_track("s1"), td_track("t1")),
            accepted(sp_track("s2"), td_track("t2")),
        ]
        conv = self._converter(playlist, results)
        items = conv.enqueue(conv.convert(playlist))

        assert len({i.group_id for i in items}) == 1
        assert items[0].group_label == "My Mix"

    def test_explicit_group_id_is_reused(self, playlist):
        results = [accepted(sp_track("s1"), td_track("t1"))]
        conv = self._converter(playlist, results)
        items = conv.enqueue(conv.convert(playlist), group_id="fixed-group")

        assert items[0].group_id == "fixed-group"

    def test_folder_per_playlist(self, playlist, tmp_path):
        results = [accepted(sp_track("s1"), td_track("t1"))]
        conv = self._converter(playlist, results, output_root=str(tmp_path))
        items = conv.enqueue(conv.convert(playlist))

        assert items[0].output_path == os.path.join(str(tmp_path), "My Mix")

    def test_folder_per_playlist_can_be_disabled(self, playlist, tmp_path):
        results = [accepted(sp_track("s1"), td_track("t1"))]
        conv = self._converter(
            playlist, results, output_root=str(tmp_path), folder_per_playlist=False
        )
        items = conv.enqueue(conv.convert(playlist))

        assert items[0].output_path == str(tmp_path)

    def test_no_output_root_leaves_path_unset(self, playlist):
        results = [accepted(sp_track("s1"), td_track("t1"))]
        conv = self._converter(playlist, results)
        items = conv.enqueue(conv.convert(playlist))

        assert items[0].output_path is None

    def test_playlist_name_is_sanitized_for_the_folder(self, tmp_path):
        pl = PlaylistRef(id="p", name='Rock/Metal: "Best"', platform="Spotify")
        results = [accepted(sp_track("s1"), td_track("t1"))]
        conv = self._converter(pl, results, output_root=str(tmp_path))
        items = conv.enqueue(conv.convert(pl))

        folder = os.path.basename(items[0].output_path)
        assert "/" not in folder and ":" not in folder and '"' not in folder

    def test_nothing_accepted_queues_nothing(self, playlist):
        results = [no_match(sp_track("s1"))]
        conv = self._converter(playlist, results)

        assert conv.enqueue(conv.convert(playlist)) == []

    def test_result_without_best_is_skipped(self, playlist):
        # An AUTO_ACCEPT with no candidate would crash a naive implementation.
        broken = MatchResult(
            source=sp_track("s1"), best=None, decision=MatchDecision.AUTO_ACCEPT.value
        )
        conv = self._converter(playlist, [broken])

        assert conv.enqueue(conv.convert(playlist)) == []

    def test_convert_and_enqueue_round_trip(self, playlist):
        results = [accepted(sp_track("s1"), td_track("t1"))]
        conv = self._converter(playlist, results)

        report, items = conv.convert_and_enqueue(playlist)

        assert len(report.results) == 1
        assert len(items) == 1


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

class TestApplyReview:
    def _report(self):
        s1, s2, s3 = sp_track("s1"), sp_track("s2"), sp_track("s3")
        alt = MatchCandidate(track=td_track("alt1"), score=0.7, method="fuzzy")
        r1 = needs_review(s1, td_track("t1"))
        r1.alternatives = [alt]
        return ConversionReport(
            playlist=PlaylistRef(id="p", name="P"),
            results=[r1, needs_review(s2, td_track("t2")), no_match(s3)],
        )

    def test_true_accepts_the_best_match(self):
        report = apply_review(self._report(), {"s1": True})
        assert report.results[0].decision == MatchDecision.AUTO_ACCEPT.value

    def test_false_rejects(self):
        report = apply_review(self._report(), {"s2": False})
        assert report.results[1].decision == MatchDecision.REJECTED.value
        assert report.results[1].note

    def test_track_id_picks_an_alternative(self):
        report = apply_review(self._report(), {"s1": "alt1"})
        assert report.results[0].best.track.id == "alt1"
        assert report.results[0].decision == MatchDecision.AUTO_ACCEPT.value
        assert report.results[0].best.method == "manual"

    def test_unknown_alternative_id_changes_nothing(self):
        report = apply_review(self._report(), {"s1": "does-not-exist"})
        assert report.results[0].best.track.id == "t1"
        assert report.results[0].decision == MatchDecision.NEEDS_REVIEW.value

    def test_unknown_source_id_is_ignored(self):
        report = apply_review(self._report(), {"nope": True})
        assert report.results[0].decision == MatchDecision.NEEDS_REVIEW.value

    def test_empty_decisions_is_a_no_op(self):
        report = apply_review(self._report(), {})
        assert report.results[0].decision == MatchDecision.NEEDS_REVIEW.value

    def test_rejected_items_do_not_get_queued(self):
        report = apply_review(self._report(), {"s1": False, "s2": True})
        queue = FakeQueue()
        conv = PlaylistConverter(FakeSpotify(report.playlist, []), FakeMatcher([]), queue)

        items = conv.enqueue(report)

        assert len(items) == 1
        assert items[0].source["spotify_track_id"] == "s2"


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

class TestReporting:
    def _report(self):
        r1 = accepted(sp_track("s1", title="A"), td_track("t1"))
        r2 = needs_review(sp_track("s2", title="B"), td_track("t2"))
        r3 = no_match(sp_track("s3", title="C", artist="Nobody"))
        return ConversionReport(playlist=PlaylistRef(id="p", name="Mix"), results=[r1, r2, r3])

    def test_summary_counts(self):
        text = report_summary(self._report())
        assert "Mix" in text
        assert "3 tracks" in text
        assert "1 matched" in text
        assert "1 need review" in text
        assert "1 not found" in text

    def test_unmatched_lines_lists_misses(self):
        lines = unmatched_lines(self._report())
        assert len(lines) == 1
        assert "Nobody - C" in lines[0]

    def test_unmatched_lines_includes_rejected(self):
        report = apply_review(self._report(), {"s2": False})
        lines = unmatched_lines(report)
        assert len(lines) == 2

    def test_save_report_writes_valid_json(self, tmp_path):
        path = tmp_path / "sub" / "report.json"
        save_report(self._report(), str(path))

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["playlist"]["name"] == "Mix"
        assert len(data["results"]) == 3
        assert data["counts"]["total"] == 3
        assert isinstance(data["unmatched"], list)

    def test_save_report_leaves_no_temp_files(self, tmp_path):
        path = tmp_path / "report.json"
        save_report(self._report(), str(path))

        assert [p.name for p in tmp_path.iterdir()] == ["report.json"]

    def test_report_round_trips(self, tmp_path):
        path = tmp_path / "report.json"
        save_report(self._report(), str(path))

        data = json.loads(path.read_text(encoding="utf-8"))
        restored = ConversionReport.from_dict(data)

        assert restored.playlist.name == "Mix"
        assert len(restored.results) == 3
        assert restored.results[0].best.track.id == "t1"


# ---------------------------------------------------------------------------
# queue_items_for_tracks (TIDAL browser path)
# ---------------------------------------------------------------------------

class TestQueueItemsForTracks:
    def test_builds_hi_res_items(self):
        items = queue_items_for_tracks([td_track("t1"), td_track("t2")], quality="hifi")
        assert len(items) == 2
        assert all(i.quality == "hifi" and i.platform == "TIDAL" for i in items)
        assert items[0].source["kind"] == "tidal_playlist"

    def test_skips_tracks_without_a_url(self):
        items = queue_items_for_tracks([td_track("t1"), TrackRef(id="t2", url="")])
        assert len(items) == 1

    def test_group_and_output_path_are_applied(self, tmp_path):
        items = queue_items_for_tracks(
            [td_track("t1")],
            output_path=str(tmp_path),
            group_id="g1",
            group_label="My TIDAL Playlist",
        )
        assert items[0].output_path == str(tmp_path)
        assert items[0].group_id == "g1"
        assert items[0].group_label == "My TIDAL Playlist"

    def test_quality_is_normalized(self):
        items = queue_items_for_tracks([td_track("t1")], quality="Hi-Res")
        assert items[0].quality == "hifi"

    def test_empty_input(self):
        assert queue_items_for_tracks([]) == []
