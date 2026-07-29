"""Tests for hires.controllers (the tkinter-free UI logic)."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.controllers import (  # noqa: E402
    QueueController,
    ReviewRow,
    SpotifyImportController,
    TidalBrowserController,
    UiDispatcher,
    run_in_background,
)
from hires.models import (  # noqa: E402
    AuthRequiredError,
    ConversionReport,
    MatchCandidate,
    MatchDecision,
    MatchResult,
    PlaylistRef,
    QueueItem,
    QueueStatus,
    TrackRef,
)
from hires.queue_store import QueueStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def td_track(tid, title="Song", artist="Artist"):
    return TrackRef(
        id=tid,
        title=title,
        artists=[artist],
        url=f"https://tidal.com/browse/track/{tid}",
        platform="TIDAL",
    )


def sp_track(tid, title="Song", artist="Artist"):
    return TrackRef(id=tid, title=title, artists=[artist], platform="Spotify")


class SyncDispatcher(UiDispatcher):
    """Runs callbacks inline so tests need no event loop."""

    def __init__(self):
        super().__init__(app=None)


class FakeApp:
    """Minimal stand-in for the tkinter root."""

    def __init__(self, explode=False):
        self.scheduled = []
        self.explode = explode

    def after(self, delay, fn=None):
        if self.explode:
            raise RuntimeError("application has been destroyed")
        self.scheduled.append(fn)
        return f"after#{len(self.scheduled)}"

    def run_pending(self):
        pending, self.scheduled = self.scheduled, []
        for fn in pending:
            fn()


@pytest.fixture
def queue(tmp_path):
    return QueueStore(str(tmp_path / "queue.json"))


def wait(thread, timeout=5):
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "worker thread did not finish"


# ---------------------------------------------------------------------------
# UiDispatcher
# ---------------------------------------------------------------------------

class TestUiDispatcher:
    def test_runs_inline_without_an_app(self):
        seen = []
        UiDispatcher()(seen.append, 42)
        assert seen == [42]

    def test_marshals_through_app_after(self):
        app = FakeApp()
        seen = []
        UiDispatcher(app)(seen.append, 1)

        assert seen == []  # not yet: it is queued on the UI thread
        app.run_pending()
        assert seen == [1]

    def test_swallows_callback_exceptions(self):
        def boom():
            raise ValueError("nope")

        UiDispatcher()(boom)  # must not raise

    def test_survives_a_destroyed_app(self):
        UiDispatcher(FakeApp(explode=True))(lambda: None)  # must not raise

    def test_passes_kwargs(self):
        seen = {}
        UiDispatcher()(lambda **kw: seen.update(kw), a=1)
        assert seen == {"a": 1}


class TestRunInBackground:
    def test_runs_and_is_a_daemon(self):
        done = threading.Event()
        thread = run_in_background(done.set)
        wait(thread)
        assert done.is_set()
        assert thread.daemon


# ---------------------------------------------------------------------------
# QueueController
# ---------------------------------------------------------------------------

class TestQueueController:
    def _fill(self, queue):
        queue.add_many(
            [
                QueueItem(url="u1", title="First", subtitle="A", quality="hifi"),
                QueueItem(url="u2", title="Second", subtitle="B", quality="hifi"),
                QueueItem(url="u3", title="Third", subtitle="C", quality="hifi"),
            ]
        )

    def test_rows_reflect_queue_order(self, queue):
        self._fill(queue)
        rows = QueueController(queue).rows()
        assert [r.title for r in rows] == ["First", "Second", "Third"]

    def test_row_symbols_per_status(self, queue):
        self._fill(queue)
        ctrl = QueueController(queue)
        items = queue.list()
        queue.mark_done(items[0].id)
        queue.mark_failed(items[1].id, "boom")

        by_title = {r.title: r for r in ctrl.rows()}
        assert by_title["First"].symbol == "✓"
        assert by_title["Second"].symbol == "✗"
        assert by_title["Second"].error == "boom"
        assert by_title["Third"].symbol == "•"

    def test_row_shows_readable_quality(self, queue):
        self._fill(queue)
        assert "24 bit" in QueueController(queue).rows()[0].quality

    def test_rows_can_filter_by_status(self, queue):
        self._fill(queue)
        ctrl = QueueController(queue)
        queue.mark_done(queue.list()[0].id)

        assert len(ctrl.rows(status=QueueStatus.DONE.value)) == 1
        assert len(ctrl.rows(status=QueueStatus.PENDING.value)) == 2

    def test_row_text_combines_title_and_detail(self, queue):
        queue.add(QueueItem(url="u", title="T", subtitle="Artist", group_label="Mix"))
        row = QueueController(queue).rows()[0]
        assert "T" in row.text and "Artist" in row.text and "Mix" in row.text

    def test_summary_when_empty(self, queue):
        assert QueueController(queue).summary() == "Queue is empty"

    def test_summary_counts_and_pause_flag(self, queue):
        self._fill(queue)
        ctrl = QueueController(queue)
        queue.mark_failed(queue.list()[0].id, "x")
        ctrl.toggle_pause()

        text = ctrl.summary()
        assert "3 items" in text and "1 failed" in text and "PAUSED" in text

    def test_toggle_pause_round_trip(self, queue):
        ctrl = QueueController(queue)
        assert ctrl.toggle_pause() is True
        assert queue.is_paused is True
        assert ctrl.toggle_pause() is False
        assert queue.is_paused is False

    def test_retry_all_failed(self, queue):
        self._fill(queue)
        ctrl = QueueController(queue)
        for item in queue.list():
            queue.mark_failed(item.id, "x")

        assert ctrl.retry_all_failed() == 3
        assert queue.stats().pending == 3

    def test_move_up_and_down(self, queue):
        self._fill(queue)
        ctrl = QueueController(queue)
        second = queue.list()[1].id

        ctrl.move_up(second)
        assert [r.title for r in ctrl.rows()] == ["Second", "First", "Third"]
        ctrl.move_down(second)
        assert [r.title for r in ctrl.rows()] == ["First", "Second", "Third"]

    def test_remove_and_clear(self, queue):
        self._fill(queue)
        ctrl = QueueController(queue)
        items = queue.list()

        assert ctrl.remove(items[0].id) is True
        queue.mark_done(items[1].id)
        assert ctrl.clear_finished() == 1
        assert ctrl.clear_all() == 1
        assert ctrl.rows() == []

    def test_subscribe_fires_on_change(self, queue):
        ctrl = QueueController(queue)
        calls = []
        ctrl.subscribe(lambda: calls.append(1))

        queue.add(QueueItem(url="u"))

        assert calls


# ---------------------------------------------------------------------------
# TidalBrowserController
# ---------------------------------------------------------------------------

class FakeTidalLibrary:
    def __init__(self, playlists=None, tracks=None, error=None):
        self._playlists = playlists or []
        self._tracks = tracks or []
        self._error = error

    def list_playlists(self, include_favorites=True):
        if self._error:
            raise self._error
        if include_favorites:
            return list(self._playlists)
        return [p for p in self._playlists if p.kind != "favorite"]

    def get_playlist_tracks(self, playlist_id):
        if self._error:
            raise self._error
        return list(self._tracks)


class TestTidalBrowserController:
    def _ctrl(self, queue, library, **kwargs):
        return TidalBrowserController(
            lambda: library, queue, dispatch=SyncDispatcher(), **kwargs
        )

    def test_load_playlists(self, queue):
        pls = [PlaylistRef(id="p1", name="Mine", kind="user")]
        ctrl = self._ctrl(queue, FakeTidalLibrary(playlists=pls))
        done, errors = [], []

        wait(ctrl.load_playlists(done.append, errors.append))

        assert errors == []
        assert [p.name for p in done[0].all_playlists()] == ["Mine"]
        assert ctrl.playlists == pls

    def test_load_playlists_can_exclude_favorites(self, queue):
        pls = [
            PlaylistRef(id="p1", name="Mine", kind="user"),
            PlaylistRef(id="p2", name="Saved", kind="favorite"),
        ]
        ctrl = self._ctrl(queue, FakeTidalLibrary(playlists=pls))
        done = []

        wait(ctrl.load_playlists(done.append, lambda e: None, include_favorites=False))

        assert [p.name for p in done[0].all_playlists()] == ["Mine"]

    def test_missing_module_reports_a_helpful_error(self, queue):
        ctrl = TidalBrowserController(lambda: None, queue, dispatch=SyncDispatcher())
        errors = []

        wait(ctrl.load_playlists(lambda p: None, errors.append))

        assert errors and "TIDAL module not available" in errors[0]

    def test_library_error_goes_to_on_error(self, queue):
        ctrl = self._ctrl(queue, FakeTidalLibrary(error=RuntimeError("login required")))
        errors = []

        wait(ctrl.load_playlists(lambda p: None, errors.append))

        assert errors == ["login required"]

    def test_error_without_message_still_reports_something(self, queue):
        ctrl = self._ctrl(queue, FakeTidalLibrary(error=RuntimeError()))
        errors = []

        wait(ctrl.load_playlists(lambda p: None, errors.append))

        assert errors == ["RuntimeError"]

    def test_load_tracks(self, queue):
        ctrl = self._ctrl(queue, FakeTidalLibrary(tracks=[td_track("t1"), td_track("t2")]))
        done = []

        wait(ctrl.load_tracks(PlaylistRef(id="p1"), done.append, lambda e: None))

        assert len(done[0]) == 2

    def test_enqueue_playlist_as_one_item(self, queue):
        ctrl = self._ctrl(queue, FakeTidalLibrary())
        pl = PlaylistRef(id="p1", name="Mix", owner="Me", platform="TIDAL")

        item = ctrl.enqueue_playlist_as_one(pl)

        assert item.media_kind == "playlist"
        assert item.url.endswith("/playlist/p1")
        assert item.quality == "hifi"
        assert len(queue.list()) == 1

    def test_enqueue_playlist_uses_existing_url(self, queue):
        ctrl = self._ctrl(queue, FakeTidalLibrary())
        pl = PlaylistRef(id="p1", name="Mix", url="https://listen.tidal.com/playlist/p1")

        assert ctrl.enqueue_playlist_as_one(pl).url == "https://listen.tidal.com/playlist/p1"

    def test_enqueue_tracks_makes_one_item_each(self, queue, tmp_path):
        ctrl = self._ctrl(
            queue, FakeTidalLibrary(), output_provider=lambda: str(tmp_path)
        )
        pl = PlaylistRef(id="p1", name="My Mix")

        items = ctrl.enqueue_tracks(pl, [td_track("t1"), td_track("t2")])

        assert len(items) == 2
        assert all(i.media_kind == "track" and i.quality == "hifi" for i in items)
        assert len({i.group_id for i in items}) == 1
        assert items[0].output_path == os.path.join(str(tmp_path), "My Mix")

    def test_enqueue_tracks_without_subfolder(self, queue, tmp_path):
        ctrl = self._ctrl(
            queue,
            FakeTidalLibrary(),
            output_provider=lambda: str(tmp_path),
            folder_per_playlist=False,
        )
        items = ctrl.enqueue_tracks(PlaylistRef(id="p1", name="Mix"), [td_track("t1")])

        assert items[0].output_path == str(tmp_path)

    def test_quality_provider_is_honoured(self, queue):
        ctrl = self._ctrl(queue, FakeTidalLibrary(), quality_provider=lambda: "lossless")
        item = ctrl.enqueue_playlist_as_one(PlaylistRef(id="p1", name="Mix"))
        assert item.quality == "lossless"


# ---------------------------------------------------------------------------
# SpotifyImportController
# ---------------------------------------------------------------------------

class FakeSpotifySource:
    def __init__(self, playlists=None, tracks=None, error=None):
        self._playlists = playlists or []
        self._tracks = tracks or []
        self._error = error

    def is_user_authorized(self):
        # Mirrors the real SpotifySource facade. AuthAwareSource further down
        # overrides this to exercise the sign-in path.
        return True

    def list_user_playlists(self):
        if self._error:
            raise self._error
        return list(self._playlists)

    def get_playlist(self, ref):
        return ref if isinstance(ref, PlaylistRef) else PlaylistRef(id=str(ref), name="Remote")

    def get_playlist_tracks(self, ref):
        if self._error:
            raise self._error
        return list(self._tracks)


class FakeMatcher:
    def __init__(self, results):
        self._results = results

    def match_many(self, sources, progress_callback=None):
        if progress_callback:
            for i, r in enumerate(self._results, 1):
                progress_callback(i, len(self._results), r)
        return list(self._results)


def accepted(source, target, score=0.99):
    return MatchResult(
        source=source,
        best=MatchCandidate(track=target, score=score, method="isrc", reasons=["exact ISRC match"]),
        decision=MatchDecision.AUTO_ACCEPT.value,
    )


def review(source, target, score=0.72, alternatives=None):
    return MatchResult(
        source=source,
        best=MatchCandidate(track=target, score=score, method="fuzzy", reasons=["title differs"]),
        alternatives=alternatives or [],
        decision=MatchDecision.NEEDS_REVIEW.value,
    )


class TestSpotifyImportController:
    def _ctrl(self, queue, source, matcher, **kwargs):
        return SpotifyImportController(
            lambda: source, lambda: matcher, queue, dispatch=SyncDispatcher(), **kwargs
        )

    def test_load_playlists(self, queue):
        pls = [PlaylistRef(id="liked", name="Liked Songs", kind="liked")]
        ctrl = self._ctrl(queue, FakeSpotifySource(playlists=pls), FakeMatcher([]))
        done = []

        wait(ctrl.load_playlists(done.append, lambda e: None))

        assert [p.name for p in done[0].all_playlists()] == ["Liked Songs"]

    def test_unconfigured_spotify_reports_an_error(self, queue):
        ctrl = SpotifyImportController(
            lambda: None, lambda: FakeMatcher([]), queue, dispatch=SyncDispatcher()
        )
        errors = []

        wait(ctrl.load_playlists(lambda p: None, errors.append))

        assert errors and "Spotify is not configured" in errors[0]

    def test_convert_reports_progress_and_result(self, queue):
        s1, s2 = sp_track("s1"), sp_track("s2")
        results = [accepted(s1, td_track("t1")), review(s2, td_track("t2"))]
        ctrl = self._ctrl(
            queue, FakeSpotifySource(tracks=[s1, s2]), FakeMatcher(results)
        )
        progress, done, errors = [], [], []

        wait(
            ctrl.convert(
                PlaylistRef(id="p1", name="Mix"),
                lambda d, t, r: progress.append((d, t)),
                done.append,
                errors.append,
            )
        )

        assert errors == []
        assert progress == [(1, 2), (2, 2)]
        assert done[0].counts["total"] == 2
        assert ctrl.report is done[0]

    def test_convert_queues_nothing_on_its_own(self, queue):
        s1 = sp_track("s1")
        ctrl = self._ctrl(
            queue, FakeSpotifySource(tracks=[s1]), FakeMatcher([accepted(s1, td_track("t1"))])
        )

        wait(ctrl.convert(PlaylistRef(id="p1"), lambda *a: None, lambda r: None, lambda e: None))

        assert queue.list() == []

    def test_convert_without_tidal_reports_an_error(self, queue):
        ctrl = SpotifyImportController(
            lambda: FakeSpotifySource(tracks=[sp_track("s1")]),
            lambda: None,
            queue,
            dispatch=SyncDispatcher(),
        )
        errors = []

        wait(ctrl.convert(PlaylistRef(id="p1"), lambda *a: None, lambda r: None, errors.append))

        assert errors and "TIDAL module not available" in errors[0]

    def test_cancel_mid_run_aborts_the_conversion(self, queue):
        sources = [sp_track(f"s{i}") for i in range(5)]
        results = [accepted(s, td_track(f"t{i}")) for i, s in enumerate(sources)]
        ctrl = self._ctrl(queue, FakeSpotifySource(tracks=sources), FakeMatcher(results))
        errors, seen = [], []

        def on_progress(done, total, result):
            seen.append(done)
            if done == 2:  # user hits Cancel while it runs
                ctrl.cancel()

        wait(ctrl.convert(PlaylistRef(id="p1"), on_progress, lambda r: None, errors.append))

        assert errors and "cancelled" in errors[0].lower()
        assert seen == [1, 2]  # stopped instead of walking all five
        assert queue.list() == []

    def test_cancel_does_not_poison_the_next_run(self, queue):
        s1 = sp_track("s1")
        ctrl = self._ctrl(
            queue, FakeSpotifySource(tracks=[s1]), FakeMatcher([accepted(s1, td_track("t1"))])
        )
        ctrl.cancel()  # left over from a previous, aborted import
        done, errors = [], []

        wait(ctrl.convert(PlaylistRef(id="p1"), lambda *a: None, done.append, errors.append))

        assert errors == []
        assert done and done[0].counts["total"] == 1

    def test_enqueue_confident_only_skips_review_items(self, queue):
        s1, s2 = sp_track("s1"), sp_track("s2")
        results = [accepted(s1, td_track("t1")), review(s2, td_track("t2"))]
        ctrl = self._ctrl(queue, FakeSpotifySource(tracks=[s1, s2]), FakeMatcher(results))
        wait(ctrl.convert(PlaylistRef(id="p1"), lambda *a: None, lambda r: None, lambda e: None))

        items = ctrl.enqueue_confident_only()

        assert len(items) == 1
        assert items[0].source["tidal_track_id"] == "t1"

    def test_review_rows_describe_the_uncertain_matches(self, queue):
        s1, s2 = sp_track("s1", title="Song A"), sp_track("s2", title="Song B")
        alt = MatchCandidate(track=td_track("alt", title="Alt"), score=0.6)
        results = [accepted(s1, td_track("t1")), review(s2, td_track("t2"), 0.72, [alt])]
        ctrl = self._ctrl(queue, FakeSpotifySource(tracks=[s1, s2]), FakeMatcher(results))
        wait(ctrl.convert(PlaylistRef(id="p1"), lambda *a: None, lambda r: None, lambda e: None))

        rows = ctrl.review_rows()

        assert len(rows) == 1
        assert rows[0].source_id == "s2"
        assert rows[0].score_text == "72%"
        assert "title differs" in rows[0].reasons
        assert rows[0].alternatives == [("alt", "Artist - Alt", 0.6)]

    def test_review_rows_empty_without_a_report(self, queue):
        ctrl = self._ctrl(queue, FakeSpotifySource(), FakeMatcher([]))
        assert ctrl.review_rows() == []

    def test_apply_review_queues_accepted_and_skips_rejected(self, queue):
        s1, s2 = sp_track("s1"), sp_track("s2")
        results = [review(s1, td_track("t1")), review(s2, td_track("t2"))]
        ctrl = self._ctrl(queue, FakeSpotifySource(tracks=[s1, s2]), FakeMatcher(results))
        wait(ctrl.convert(PlaylistRef(id="p1"), lambda *a: None, lambda r: None, lambda e: None))

        items = ctrl.apply_review_and_enqueue({"s1": True, "s2": False})

        assert len(items) == 1
        assert items[0].source["spotify_track_id"] == "s1"

    def test_apply_review_can_pick_an_alternative(self, queue):
        s1 = sp_track("s1")
        alt = MatchCandidate(track=td_track("alt"), score=0.6)
        ctrl = self._ctrl(
            queue,
            FakeSpotifySource(tracks=[s1]),
            FakeMatcher([review(s1, td_track("t1"), 0.7, [alt])]),
        )
        wait(ctrl.convert(PlaylistRef(id="p1"), lambda *a: None, lambda r: None, lambda e: None))

        items = ctrl.apply_review_and_enqueue({"s1": "alt"})

        assert items[0].source["tidal_track_id"] == "alt"
        assert items[0].source["match_method"] == "manual"

    def test_apply_review_without_report_is_a_no_op(self, queue):
        ctrl = self._ctrl(queue, FakeSpotifySource(), FakeMatcher([]))
        assert ctrl.apply_review_and_enqueue({"x": True}) == []

    def test_progress_text(self):
        assert SpotifyImportController.progress_text(0, 0) == "Matching…"
        assert SpotifyImportController.progress_text(5, 10) == "Matching 5/10 (50%)"

    def test_result_summary(self, queue):
        report = ConversionReport(
            playlist=PlaylistRef(id="p"),
            results=[
                accepted(sp_track("s1"), td_track("t1")),
                review(sp_track("s2"), td_track("t2")),
                MatchResult(source=sp_track("s3"), decision=MatchDecision.NO_MATCH.value),
            ],
        )
        text = SpotifyImportController.result_summary(report)
        assert "1 matched" in text and "1 to review" in text and "1 not found" in text


class TestReviewRow:
    def test_score_text_rounds(self):
        row = ReviewRow("s", "a", "b", 0.876, "", [])
        assert row.score_text == "88%"


# ---------------------------------------------------------------------------
# Spotify sign-in (PKCE)
# ---------------------------------------------------------------------------

class FakeWebBackend:
    def __init__(self, client_id="cid", redirect_uri="http://127.0.0.1:8888/callback"):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.states = []
        self.exchanged = []

    def begin_authorization(self, state=""):
        self.states.append(state)
        return f"https://accounts.spotify.com/authorize?state={state}", "VERIFIER"

    def exchange_code(self, code, verifier):
        self.exchanged.append((code, verifier))
        return {"access_token": "AT", "refresh_token": "RT"}


class AuthAwareSource(FakeSpotifySource):
    """Spotify source that only lists playlists once authorized."""

    def __init__(self, playlists=None, web=None, authorized=False):
        super().__init__(playlists=playlists)
        self.web = web if web is not None else FakeWebBackend()
        self._authorized = authorized

    def is_user_authorized(self):
        return self._authorized

    def list_user_playlists(self):
        if not self._authorized:
            raise AuthRequiredError("Spotify sign-in required")
        return list(self._playlists)


class TestSpotifySignIn:
    def _ctrl(self, queue, source, *, opened=None, code="THECODE", fail=None):
        return SpotifyImportController(
            lambda: source,
            lambda: FakeMatcher([]),
            queue,
            dispatch=SyncDispatcher(),
            open_url=(opened.append if opened is not None else (lambda _u: None)),
            wait_for_code=(
                (lambda _uri, _state: (_ for _ in ()).throw(fail))
                if fail
                else (lambda _uri, _state: code)
            ),
        )

    def test_load_playlists_signs_in_first_when_unauthorized(self, queue):
        """The regression: this path previously could not work at all."""
        source = AuthAwareSource(
            playlists=[PlaylistRef(id="liked", name="Liked Songs", kind="liked")],
            authorized=False,
        )
        opened, done, errors, status = [], [], [], []

        def on_done(playlists):
            source._authorized = True  # token now stored
            done.append(playlists)

        # Authorizing must flip the source to authorized before listing.
        original = source.web.exchange_code

        def exchange(code, verifier):
            source._authorized = True
            return original(code, verifier)

        source.web.exchange_code = exchange

        ctrl = self._ctrl(queue, source, opened=opened)
        wait(ctrl.load_playlists(done.append, errors.append, on_status=status.append))

        assert errors == []
        assert opened and "accounts.spotify.com" in opened[0]
        assert source.web.exchanged == [("THECODE", "VERIFIER")]
        assert [p.name for p in done[0].all_playlists()] == ["Liked Songs"]
        assert any("browser" in s.lower() for s in status)

    def test_already_authorized_skips_the_browser(self, queue):
        source = AuthAwareSource(
            playlists=[PlaylistRef(id="p1", name="Mine")], authorized=True
        )
        opened, done = [], []

        ctrl = self._ctrl(queue, source, opened=opened)
        wait(ctrl.load_playlists(done.append, lambda e: None))

        assert opened == []
        assert source.web.exchanged == []
        assert [p.name for p in done[0].all_playlists()] == ["Mine"]

    def test_state_is_random_per_attempt(self, queue):
        source = AuthAwareSource(authorized=False)
        ctrl = self._ctrl(queue, source)

        wait(ctrl.sign_in(lambda: None, lambda e: None))
        wait(ctrl.sign_in(lambda: None, lambda e: None))

        assert len(source.web.states) == 2
        assert source.web.states[0] != source.web.states[1]
        assert all(len(s) >= 16 for s in source.web.states)

    def test_missing_client_id_explains_what_to_do(self, queue):
        source = AuthAwareSource(web=FakeWebBackend(client_id=""), authorized=False)
        errors = []

        ctrl = self._ctrl(queue, source)
        wait(ctrl.sign_in(lambda: None, errors.append))

        assert errors and "Client ID" in errors[0]
        assert "Settings" in errors[0]

    def test_denied_authorization_surfaces_the_reason(self, queue):
        source = AuthAwareSource(authorized=False)
        errors = []

        ctrl = self._ctrl(
            queue, source, fail=AuthRequiredError("Spotify denied the request: access_denied")
        )
        wait(ctrl.sign_in(lambda: None, errors.append))

        assert errors and "access_denied" in errors[0]
        assert source.web.exchanged == []

    def test_sign_in_reports_success(self, queue):
        source = AuthAwareSource(authorized=False)
        done, errors = [], []

        ctrl = self._ctrl(queue, source)
        wait(ctrl.sign_in(lambda: done.append(True), errors.append))

        assert done == [True]
        assert errors == []


class MinimalSource:
    """A source that cannot report its auth state (no is_user_authorized).

    Listing must still be attempted rather than crashing -- the source gets to
    raise AuthRequiredError on its own terms.
    """

    def __init__(self, playlists=None):
        self._playlists = playlists or []

    def list_user_playlists(self):
        return list(self._playlists)


def test_source_without_auth_reporting_is_listed_anyway(queue):
    opened = []
    ctrl = SpotifyImportController(
        lambda: MinimalSource([PlaylistRef(id="p1", name="Mine")]),
        lambda: None,
        queue,
        dispatch=SyncDispatcher(),
        open_url=opened.append,
        wait_for_code=lambda _u, _s: "CODE",
    )
    done, errors = [], []

    wait(ctrl.load_playlists(done.append, errors.append))

    assert errors == []
    assert opened == [], "must not open a browser for a source that never asked"
    assert [p.name for p in done[0].all_playlists()] == ["Mine"]
