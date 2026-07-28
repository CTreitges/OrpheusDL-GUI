"""Tests for hires.matcher.

No network: the matcher only ever talks to the injected search provider, so
:class:`FakeProvider` below is the whole outside world here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.matcher import (  # noqa: E402
    NEUTRAL_SCORE,
    TrackMatcher,
    artists_overlap,
    duration_score,
    extract_featured_artists,
    extract_version_tags,
    normalize_artist,
    normalize_title,
    score_candidate,
    token_set_ratio,
)
from hires.models import MatchDecision, MatchMethod, TrackRef  # noqa: E402

AUTO_ACCEPT = 0.88
REVIEW = 0.62


def track(track_id, title, artists=("The Weeknd",), duration=200, isrc=None, **kwargs):
    return TrackRef(
        id=track_id,
        title=title,
        artists=list(artists),
        duration_sec=duration,
        isrc=isrc,
        **kwargs,
    )


class FakeProvider:
    """Duck-typed stand-in for TidalLibrary (find_by_isrc + search_tracks)."""

    def __init__(self, isrc_hits=None, results=None, per_query=None,
                 search_error=None, isrc_error=None):
        self.isrc_hits = isrc_hits or {}
        self.results = list(results or [])
        self.per_query = per_query
        self.search_error = search_error
        self.isrc_error = isrc_error
        self.queries = []
        self.isrc_lookups = []

    def find_by_isrc(self, isrc):
        self.isrc_lookups.append(isrc)
        if self.isrc_error is not None:
            raise self.isrc_error
        return list(self.isrc_hits.get(isrc, []))

    def search_tracks(self, query, limit=20):
        self.queries.append(query)
        if self.search_error is not None:
            raise self.search_error
        if self.per_query is not None:
            for needle, hits in self.per_query.items():
                if needle in query.lower():
                    if isinstance(hits, Exception):
                        raise hits
                    return list(hits)
            return []
        return list(self.results)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

class TestNormalizeTitle:
    def test_lowercases_and_folds_accents(self):
        assert normalize_title("Café Del Mar") == "cafe del mar"
        assert normalize_title("Björk – Jóga") == "bjork joga"

    def test_unifies_typographic_marks(self):
        assert normalize_title("Don’t Stop Me Now") == "dont stop me now"
        assert normalize_title("Don't Stop Me Now") == normalize_title("Don’t Stop Me Now")
        assert normalize_title("Song — Part “Two”") == normalize_title('Song - Part "Two"')

    def test_ampersand_becomes_and(self):
        assert normalize_title("Rock & Roll") == "rock and roll"

    def test_drops_noise_and_version_brackets(self):
        assert normalize_title("Blinding Lights (Official Video)") == "blinding lights"
        assert normalize_title("Bohemian Rhapsody - Remastered 2011") == "bohemian rhapsody"
        assert normalize_title("Song (Acoustic Version)") == "song"

    def test_keeps_meaningful_brackets(self):
        assert normalize_title("Everything I Do (I Do It for You)") == "everything i do i do it for you"

    def test_drops_featured_artists(self):
        assert normalize_title("Sicko Mode (feat. Drake)") == "sicko mode"
        assert normalize_title("Song feat. Drake") == "song"

    def test_collapses_whitespace_and_punctuation(self):
        assert normalize_title("  Hello,   World!!  ") == "hello world"

    def test_keeps_non_latin_scripts(self):
        # Stripping these would make every Cyrillic/CJK title compare equal.
        assert normalize_title("Смерть") == "смерть"

    def test_non_string_input(self):
        assert normalize_title(None) == ""
        assert normalize_title(42) == ""


class TestNormalizeArtist:
    def test_drops_leading_the(self):
        assert normalize_artist("The Beatles") == "beatles"
        assert normalize_artist("the weeknd") == "weeknd"

    def test_keeps_inner_the(self):
        assert normalize_artist("Bring Me The Horizon") == "bring me the horizon"

    def test_folds_like_titles(self):
        assert normalize_artist("Beyoncé") == "beyonce"
        assert normalize_artist("Simon & Garfunkel") == "simon and garfunkel"


# ---------------------------------------------------------------------------
# Version tags
# ---------------------------------------------------------------------------

class TestExtractVersionTags:
    def test_live(self):
        assert extract_version_tags("Song (Live)") == {"live"}
        assert extract_version_tags("Hotel California - Live at The Forum") == {"live"}

    def test_remaster_with_year(self):
        assert extract_version_tags("Song (2011 Remaster)") == {"remaster"}
        assert extract_version_tags("Song - Remastered 2011") == {"remaster"}

    def test_radio_edit_suppresses_generic_edit(self):
        assert extract_version_tags("Song - Radio Edit") == {"radio edit"}

    def test_acoustic_version_suppresses_generic_version(self):
        # "(Acoustic Version)" and "(Acoustic)" must not look like different takes.
        assert extract_version_tags("Song (Acoustic Version)") == {"acoustic"}
        assert extract_version_tags("Song (Acoustic Version)") == extract_version_tags("Song (Acoustic)")

    def test_remix_and_extended(self):
        assert extract_version_tags("Song (Chromatics Remix)") == {"remix"}
        assert extract_version_tags("Song (Extended Mix)") == {"extended"}

    def test_clean_title_has_no_tags(self):
        assert extract_version_tags("Blinding Lights") == set()
        assert extract_version_tags("") == set()

    def test_only_version_segments_are_scanned(self):
        # The words are part of the title, not a version marker.
        assert extract_version_tags("Live and Let Die") == set()
        assert extract_version_tags("Live Forever") == set()

    def test_featured_artists_are_not_version_tags(self):
        assert extract_version_tags("Sicko Mode (feat. Drake)") == set()

    def test_extract_featured_artists(self):
        assert extract_featured_artists("Song (feat. Drake & Future)") == ["drake", "future"]
        assert extract_featured_artists("Song") == []


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

class TestTokenSetRatio:
    def test_identical_is_one(self):
        assert token_set_ratio("smells like teen spirit", "smells like teen spirit") == 1.0

    def test_word_order_does_not_matter(self):
        assert token_set_ratio("world hello", "hello world") == 1.0

    def test_case_and_punctuation_do_not_matter(self):
        assert token_set_ratio("Hello, World!", "hello world") == 1.0

    def test_disjoint_is_low(self):
        assert token_set_ratio("hello world", "foo bar") < 0.4

    def test_partial_overlap_is_in_between(self):
        value = token_set_ratio("dont stop me now", "dont stop")
        assert 0.4 < value < 0.9

    def test_empty_is_zero(self):
        assert token_set_ratio("", "anything") == 0.0
        assert token_set_ratio("", "") == 0.0


class TestArtistsOverlap:
    def test_full_match(self):
        assert artists_overlap(["Daft Punk"], ["daft punk"]) == 1.0
        assert artists_overlap(["The Beatles"], ["Beatles"]) == 1.0

    def test_contained_main_artist_scores_high(self):
        # Spotify lists the feature, TIDAL only the main artist.
        value = artists_overlap(["Daft Punk", "Pharrell Williams"], ["Daft Punk"])
        assert 0.7 < value < 1.0

    def test_no_overlap_is_low(self):
        assert artists_overlap(["ABBA"], ["Metallica"]) < 0.35

    def test_missing_side_is_neutral(self):
        assert artists_overlap([], ["Metallica"]) == NEUTRAL_SCORE
        assert artists_overlap(["ABBA"], []) == NEUTRAL_SCORE


class TestDurationScore:
    def test_exact(self):
        assert duration_score(200, 200) == 1.0

    def test_within_tolerance(self):
        assert duration_score(200, 203) == 1.0
        assert duration_score(200, 204) < 1.0

    def test_decays_linearly(self):
        assert duration_score(200, 209) == 0.5

    def test_far_apart_is_zero(self):
        assert duration_score(200, 215) == 0.0
        assert duration_score(200, 400) == 0.0

    def test_missing_is_neutral(self):
        assert duration_score(None, 200) == NEUTRAL_SCORE
        assert duration_score(200, None) == NEUTRAL_SCORE
        assert duration_score(200, 0) == NEUTRAL_SCORE
        assert duration_score("nonsense", 200) == NEUTRAL_SCORE


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestScoreCandidate:
    def test_perfect_match_scores_top(self):
        source = track("s1", "Blinding Lights", duration=200)
        candidate = track("t1", "Blinding Lights", duration=201)
        score, reasons = score_candidate(source, candidate)
        assert score >= AUTO_ACCEPT
        assert "titles match" in reasons
        assert "same artist" in reasons

    def test_live_version_is_penalised_below_auto_accept(self):
        """Central regression: a live take must never pass as the studio version."""
        source = track("s1", "Blinding Lights", duration=200)
        live = track("t1", "Blinding Lights (Live)", duration=200)
        score, reasons = score_candidate(source, live)
        assert score < AUTO_ACCEPT
        assert any("different version" in r for r in reasons)
        assert any("live" in r for r in reasons)

    def test_live_version_from_tidal_version_field_is_penalised(self):
        # The TIDAL module keeps the version in extra["version"] as well.
        source = track("s1", "Song", duration=200)
        live = TrackRef(id="t1", title="Song", artists=["The Weeknd"], duration_sec=200,
                        extra={"version": "Live"})
        score, _ = score_candidate(source, live)
        assert score < AUTO_ACCEPT

    def test_remix_is_penalised(self):
        source = track("s1", "Blinding Lights", duration=200)
        remix = track("t1", "Blinding Lights (Chromatics Remix)", duration=205)
        score, reasons = score_candidate(source, remix)
        assert score < REVIEW
        assert any("remix" in r for r in reasons)

    def test_matching_version_tags_are_not_penalised(self):
        source = track("s1", "Song (Live)", duration=200)
        candidate = track("t1", "Song (Live)", duration=200)
        score, reasons = score_candidate(source, candidate)
        assert score >= AUTO_ACCEPT
        assert any("same version" in r for r in reasons)

    def test_remaster_is_only_a_minor_difference(self):
        # A remaster is the same recording - it stays auto-acceptable.
        source = track("s1", "Bohemian Rhapsody", duration=354)
        candidate = track("t1", "Bohemian Rhapsody (2011 Remaster)", duration=354)
        score, reasons = score_candidate(source, candidate)
        assert score >= AUTO_ACCEPT
        assert any("minor version difference" in r for r in reasons)

    def test_wrong_artist_scores_low(self):
        source = track("s1", "Blinding Lights", artists=["The Weeknd"], duration=200)
        candidate = track("t1", "Blinding Lights", artists=["Rockabye Baby!"], duration=200)
        score, reasons = score_candidate(source, candidate)
        assert score < 0.75
        assert any("different artist" in r for r in reasons)

    def test_features_in_the_title_count_as_artists(self):
        source = track("s1", "Sicko Mode", artists=["Travis Scott", "Drake"], duration=312)
        candidate = track("t1", "Sicko Mode (feat. Drake)", artists=["Travis Scott"], duration=312)
        score, _ = score_candidate(source, candidate)
        assert score >= AUTO_ACCEPT

    def test_reasons_are_always_human_readable(self):
        score, reasons = score_candidate(track("s1", "A"), track("t1", "B"))
        assert reasons and all(isinstance(r, str) and r.strip() for r in reasons)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# TrackMatcher
# ---------------------------------------------------------------------------

class TestMatchByIsrc:
    def test_isrc_hit_is_auto_accepted(self):
        source = track("s1", "Blinding Lights", isrc="USUG11904206")
        hit = track("t1", "Blinding Lights")
        provider = FakeProvider(isrc_hits={"USUG11904206": [hit]})

        result = TrackMatcher(provider).match(source)

        assert result.decision == MatchDecision.AUTO_ACCEPT.value
        assert result.best is not None
        assert result.best.method == MatchMethod.ISRC.value
        assert result.best.score == 1.0
        assert result.best.reasons == ["exact ISRC match"]
        assert result.best.track.id == "t1"
        assert provider.queries == []  # no fuzzy search needed

    def test_multiple_isrc_hits_rank_the_closest_first(self):
        source = track("s1", "Blinding Lights", duration=200, isrc="X")
        far = track("t1", "Blinding Lights (Live)", duration=260)
        close = track("t2", "Blinding Lights", duration=200)
        provider = FakeProvider(isrc_hits={"X": [far, close]})

        result = TrackMatcher(provider).match(source)

        assert result.best.track.id == "t2"
        assert [c.track.id for c in result.alternatives] == ["t1"]
        assert result.best.reasons[0] == "exact ISRC match"
        assert all(c.method == MatchMethod.ISRC.value for c in result.alternatives)

    def test_isrc_alternatives_respect_max_alternatives(self):
        source = track("s1", "Song", isrc="X")
        hits = [track(f"t{i}", "Song") for i in range(6)]
        provider = FakeProvider(isrc_hits={"X": hits})

        result = TrackMatcher(provider, max_alternatives=2).match(source)

        assert len(result.alternatives) == 2

    def test_isrc_miss_falls_through_to_fuzzy(self):
        source = track("s1", "Blinding Lights", isrc="UNKNOWN")
        provider = FakeProvider(isrc_hits={}, results=[track("t1", "Blinding Lights")])

        result = TrackMatcher(provider).match(source)

        assert result.decision == MatchDecision.AUTO_ACCEPT.value
        assert result.best.method == MatchMethod.FUZZY.value
        assert provider.queries

    def test_isrc_lookup_error_falls_through_to_fuzzy(self):
        source = track("s1", "Blinding Lights", isrc="X")
        provider = FakeProvider(isrc_error=RuntimeError("isrc endpoint down"),
                                results=[track("t1", "Blinding Lights")])

        result = TrackMatcher(provider).match(source)

        assert result.decision == MatchDecision.AUTO_ACCEPT.value
        assert "isrc endpoint down" in result.note


class TestMatchFuzzy:
    def test_good_fuzzy_match_is_auto_accepted(self):
        source = track("s1", "Blinding Lights", duration=200)
        provider = FakeProvider(results=[track("t1", "Blinding Lights", duration=201)])

        result = TrackMatcher(provider).match(source)

        assert result.decision == MatchDecision.AUTO_ACCEPT.value
        assert result.best.track.id == "t1"
        assert result.best.method == MatchMethod.FUZZY.value

    def test_score_between_thresholds_needs_review(self):
        source = track("s1", "Blinding Lights", duration=200)
        # Same title and artist, but 22 seconds off -> uncertain, not wrong.
        provider = FakeProvider(results=[track("t1", "Blinding Lights", duration=222)])

        result = TrackMatcher(provider).match(source)

        assert result.decision == MatchDecision.NEEDS_REVIEW.value
        assert REVIEW <= result.best.score < AUTO_ACCEPT

    def test_weak_best_candidate_is_no_match_but_still_shown(self):
        source = track("s1", "Blinding Lights", artists=["The Weeknd"], duration=200)
        provider = FakeProvider(results=[track("t1", "Totally Different Song",
                                               artists=["Someone Else"], duration=300)])

        result = TrackMatcher(provider).match(source)

        assert result.decision == MatchDecision.NO_MATCH.value
        assert result.best is not None  # the review dialog still gets something to show
        assert "below the review threshold" in result.note

    def test_no_results_is_no_match(self):
        provider = FakeProvider(results=[])

        result = TrackMatcher(provider).match(track("s1", "Blinding Lights"))

        assert result.decision == MatchDecision.NO_MATCH.value
        assert result.best is None
        assert result.note

    def test_search_error_is_no_match_with_note(self):
        provider = FakeProvider(search_error=RuntimeError("boom"))

        result = TrackMatcher(provider).match(track("s1", "Blinding Lights"))

        assert result.decision == MatchDecision.NO_MATCH.value
        assert result.best is None
        assert "boom" in result.note
        assert "failed" in result.note

    def test_duplicate_ids_across_query_variants_are_merged(self):
        source = track("s1", "Blinding Lights", artists=["The Weeknd"], duration=200)
        # Same track id from two different query variants, plus one extra.
        dupe = track("t1", "Blinding Lights", duration=222)
        other = track("t2", "Blinding Lights", duration=224)
        provider = FakeProvider(per_query={
            "the weeknd blinding lights": [dupe],
            "blinding lights": [dupe, other],
        })

        result = TrackMatcher(provider).match(source)

        assert len(provider.queries) > 1
        ids = [result.best.track.id] + [c.track.id for c in result.alternatives]
        assert sorted(ids) == ["t1", "t2"]

    def test_max_alternatives_is_respected(self):
        source = track("s1", "Blinding Lights", duration=200)
        results = [track(f"t{i}", "Blinding Lights", duration=200 + i) for i in range(8)]
        provider = FakeProvider(results=results)

        result = TrackMatcher(provider, max_alternatives=3).match(source)

        assert len(result.alternatives) == 3

    def test_stops_searching_once_a_candidate_is_good_enough(self):
        source = track("s1", "Blinding Lights", duration=200)
        provider = FakeProvider(results=[track("t1", "Blinding Lights", duration=200)])

        TrackMatcher(provider).match(source)

        assert len(provider.queries) == 1

    def test_tries_several_query_variants_when_nothing_fits(self):
        provider = FakeProvider(results=[])

        TrackMatcher(provider).match(track("s1", "Song (Radio Edit)", artists=["Artist"]))

        assert len(provider.queries) > 1
        assert any("radio edit" not in q.lower() for q in provider.queries)

    def test_candidates_without_id_are_ignored(self):
        provider = FakeProvider(results=[track("", "Blinding Lights")])

        result = TrackMatcher(provider).match(track("s1", "Blinding Lights"))

        assert result.decision == MatchDecision.NO_MATCH.value
        assert result.best is None


class TestMatchMany:
    def test_returns_one_result_per_source(self):
        provider = FakeProvider(results=[track("t1", "Blinding Lights")])
        sources = [track("s1", "Blinding Lights"), track("s2", "Blinding Lights")]

        results = TrackMatcher(provider).match_many(sources)

        assert len(results) == 2
        assert [r.source.id for r in results] == ["s1", "s2"]

    def test_progress_callback_gets_done_total_and_result(self):
        provider = FakeProvider(results=[track("t1", "Blinding Lights")])
        sources = [track("s1", "Blinding Lights"), track("s2", "Blinding Lights")]
        seen = []

        results = TrackMatcher(provider).match_many(
            sources, progress_callback=lambda done, total, result: seen.append((done, total, result))
        )

        assert [(d, t) for d, t, _ in seen] == [(1, 2), (2, 2)]
        assert [r for _, _, r in seen] == results

    def test_broken_progress_callback_does_not_abort(self):
        provider = FakeProvider(results=[track("t1", "Blinding Lights")])
        sources = [track("s1", "Blinding Lights"), track("s2", "Blinding Lights")]

        def boom(done, total, result):
            raise RuntimeError("gui died")

        results = TrackMatcher(provider).match_many(sources, progress_callback=boom)

        assert len(results) == 2

    def test_empty_input(self):
        assert TrackMatcher(FakeProvider()).match_many([]) == []
        assert TrackMatcher(FakeProvider()).match_many(None) == []

    def test_one_broken_track_does_not_kill_the_playlist(self):
        provider = FakeProvider(search_error=RuntimeError("rate limited"))
        sources = [track("s1", "A"), track("s2", "B")]

        results = TrackMatcher(provider).match_many(sources)

        assert len(results) == 2
        assert all(r.decision == MatchDecision.NO_MATCH.value for r in results)

    def test_accepts_a_generator(self):
        provider = FakeProvider(results=[track("t1", "Song")])

        results = TrackMatcher(provider).match_many(track(f"s{i}", "Song") for i in range(3))

        assert len(results) == 3


# ---------------------------------------------------------------------------
# Robustness against malformed payloads
# ---------------------------------------------------------------------------

class TestMalformedPayloads:
    def test_artists_as_a_bare_string_counts_as_one_name(self):
        # A single artist sometimes arrives unwrapped; iterating it per
        # character would turn an exact match into a review case.
        source = TrackRef(id="s1", title="Song", artists="The Weeknd", duration_sec=200)
        candidate = track("t1", "Song", duration=200)

        score, reasons = score_candidate(source, candidate)

        assert score >= AUTO_ACCEPT
        assert "same artist" in reasons

    def test_unusable_artists_field_does_not_raise(self):
        candidate = track("t1", "Song", duration=200)
        for broken in (5, object(), None, ["ok", 5, None]):
            source = TrackRef(id="s1", title="Song", artists=broken, duration_sec=200)
            score, _ = score_candidate(source, candidate)
            assert 0.0 <= score <= 1.0

    def test_match_survives_a_broken_artists_field(self):
        # One malformed track must not abort a 200 track playlist.
        provider = FakeProvider(results=[track("t1", "Song")])
        sources = [
            TrackRef(id="s1", title="Song", artists=5, duration_sec=200),
            track("s2", "Song"),
        ]

        results = TrackMatcher(provider).match_many(sources)

        assert len(results) == 2

    def test_artists_overlap_tolerates_non_lists(self):
        assert artists_overlap(5, ["Metallica"]) == NEUTRAL_SCORE
        assert artists_overlap("Metallica", ["Metallica"]) == 1.0

    def test_punctuation_only_title_is_still_searched(self):
        # "!!!" normalises to the empty string - without a raw-title fallback
        # the provider would never be asked at all.
        provider = FakeProvider(results=[track("t1", "!!!")])

        result = TrackMatcher(provider).match(track("s1", "!!!"))

        assert provider.queries
        assert any("!!!" in q for q in provider.queries)
        assert result.best is not None  # shown for manual review

    def test_completely_empty_title_searches_nothing(self):
        provider = FakeProvider(results=[track("t1", "Song")])

        result = TrackMatcher(provider).match(TrackRef(id="s1", title="", artists=[]))

        assert provider.queries == []
        assert result.decision == MatchDecision.NO_MATCH.value

    def test_reasons_name_the_missing_pieces(self):
        source = TrackRef(id="s1", title="Song", artists=[], duration_sec=None)
        candidate = TrackRef(id="t1", title="Song", artists=["Someone"], duration_sec=None)

        _, reasons = score_candidate(source, candidate)

        assert "artist missing on one side" in reasons
        assert "duration unknown" in reasons

    def test_partial_artist_overlap_is_reported_as_such(self):
        source = track("s1", "Song", artists=["Daft Punk", "Pharrell Williams"])
        candidate = track("t1", "Song", artists=["Daft Punk"])

        _, reasons = score_candidate(source, candidate)

        assert any("artist partially matches" in r for r in reasons)


class TestProviderContract:
    def test_provider_without_find_by_isrc_falls_back_to_search(self):
        class SearchOnlyProvider:
            def __init__(self):
                self.queries = []

            def search_tracks(self, query, limit=20):
                self.queries.append(query)
                return [track("t1", "Song")]

        provider = SearchOnlyProvider()

        result = TrackMatcher(provider).match(track("s1", "Song", isrc="ABC"))

        assert result.decision == MatchDecision.AUTO_ACCEPT.value
        assert provider.queries

    def test_one_failing_query_variant_does_not_lose_the_match(self):
        provider = FakeProvider(per_query={
            "the weeknd blinding lights": RuntimeError("rate limit"),
            "blinding lights": [track("t1", "Blinding Lights", duration=200)],
        })

        result = TrackMatcher(provider).match(track("s1", "Blinding Lights", duration=200))

        assert result.decision == MatchDecision.AUTO_ACCEPT.value
        assert result.best.track.id == "t1"
        assert "rate limit" in result.note

    def test_thresholds_are_inclusive(self):
        source = track("s1", "Blinding Lights", duration=200)
        candidate = track("t1", "Blinding Lights", duration=222)
        score, _ = score_candidate(source, candidate)

        exact = TrackMatcher(FakeProvider(results=[candidate]),
                             auto_accept_threshold=score).match(source)
        just_above = TrackMatcher(FakeProvider(results=[candidate]),
                                  auto_accept_threshold=score + 1e-6).match(source)

        assert exact.decision == MatchDecision.AUTO_ACCEPT.value
        assert just_above.decision == MatchDecision.NEEDS_REVIEW.value

    def test_custom_duration_tolerance(self):
        assert duration_score(200, 208, tolerance=10) == 1.0
        assert duration_score(200, 201, tolerance=0) < 1.0
        assert duration_score(200, 250, tolerance=99) == 1.0
