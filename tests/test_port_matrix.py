"""Port tab logic in all 12 directions (B3).

Every direction runs the real chain -- PortController -> PlaylistConverter ->
TrackMatcher -> QueueStore -- against fake services that share one small
catalogue. Only the services are faked; matching, deciding and queueing are
the production code.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from hires.controllers import UiDispatcher  # noqa: E402
from hires.models import MatchDecision, MatchMethod, PlaylistRef, TrackRef  # noqa: E402
from hires.playlist_providers import (  # noqa: E402
    SERVICES,
    PlaylistProvider,
    ProviderStatus,
    UnavailableProvider,
    port_directions,
)
from hires.port_controller import PortController  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402

CATALOGUE = [
    ("Daft Punk", "Get Lucky", 248, "USQX91300108"),
    ("Avicii", "Levels", 199, "SE3AA1100001"),
    ("Kygo", "Firestone", 272, "GBCEN1400155"),
]
#: Only in the source playlists -- no service can find it.
UNRELEASED = ("Nobody", "Unreleased ID", 300, None)

#: Services whose catalogue carries ISRCs and that can look them up.
ISRC_LOOKUP = {"Spotify", "TIDAL"}
#: Services whose tracks carry an ISRC at all (SoundCloud: label uploads).
HAS_ISRC = {"Spotify", "TIDAL", "SoundCloud"}

READ_METHODS = {"status", "list_folders", "get_playlist", "get_playlist_tracks", "search_tracks", "find_by_isrc"}


class FakeService(PlaylistProvider):
    """A read-only service. Any write method would be an AttributeError."""

    def __init__(self, service):
        self.service = service
        self.quality = "hifi" if service == "TIDAL" else None
        self.calls = []

    def _track(self, index, song):
        artist, title, seconds, isrc = song
        return TrackRef(
            id=f"{self.service.lower()}-{index}",
            title=title,
            artists=[artist],
            duration_sec=seconds,
            isrc=isrc if self.service in HAS_ISRC else None,
            url=f"https://{self.service.lower()}.example/track/{index}",
            platform=self.service,
        )

    def status(self):
        self.calls.append("status")
        return ProviderStatus(self.service, can_list_own=True)

    def list_folders(self, include_favorites=True):
        from hires.models import flat_root

        self.calls.append("list_folders")
        return flat_root([PlaylistRef(id="set1", name=f"{self.service} Set")], platform=self.service)

    def get_playlist(self, playlist):
        self.calls.append("get_playlist")
        return PlaylistRef(id="set1", name=f"{self.service} Set", platform=self.service, url=str(playlist))

    def get_playlist_tracks(self, playlist):
        self.calls.append("get_playlist_tracks")
        return [self._track(i, s) for i, s in enumerate(CATALOGUE + [UNRELEASED])]

    def search_tracks(self, query, limit=10):
        self.calls.append("search_tracks")
        q = query.lower()
        return [self._track(i, s) for i, s in enumerate(CATALOGUE) if s[1].lower() in q]

    def find_by_isrc(self, isrc):
        self.calls.append("find_by_isrc")
        if self.service not in ISRC_LOOKUP:
            return []
        return [self._track(i, s) for i, s in enumerate(CATALOGUE) if s[3] == isrc]


def make_controller(tmp_path, services=None):
    services = services or {name: FakeService(name) for name in SERVICES}
    providers = {name: (lambda svc=svc: svc) for name, svc in services.items()}
    queue = QueueStore(str(tmp_path / "queue.json"))
    controller = PortController(
        providers,
        queue,
        spotify_source_provider=lambda: services.get("Spotify"),
        dispatch=UiDispatcher(None),  # runs callbacks inline
        output_provider=lambda: str(tmp_path),
    )
    return controller, queue, services


def convert(controller, playlist="https://example/playlist"):
    done, errors = [], []
    thread = controller.convert(playlist, lambda *_a: None, done.append, errors.append)
    thread.join(5)
    return done, errors


@pytest.mark.parametrize("source, target", port_directions(), ids=lambda v: v)
def test_port_matrix_direction(tmp_path, source, target):
    controller, queue, services = make_controller(tmp_path)
    controller.set_direction(source, target)

    done, errors = convert(controller)
    assert errors == [] and len(done) == 1
    report = done[0]
    assert (report.source_service, report.target_service) == (source, target)
    counts = report.counts
    assert counts[MatchDecision.AUTO_ACCEPT.value] == 3
    assert counts[MatchDecision.NO_MATCH.value] == 1  # the unreleased ID is reported, not dropped

    # ISRC where both ends have it, otherwise title, artist and length.
    expected = MatchMethod.ISRC.value if (source in HAS_ISRC and target in ISRC_LOOKUP) else MatchMethod.FUZZY.value
    assert {r.best.method for r in report.auto_accepted} == {expected}

    items = controller.enqueue_confident_only(report)
    assert len(items) == 3 and len(queue.list()) == 3
    assert {i.platform for i in items} == {target}
    assert all(i.url.startswith(f"https://{target.lower()}.example/") for i in items)
    assert {i.quality for i in items} == ({"hifi"} if target == "TIDAL" else {None})
    # One group per ported playlist, named after it.
    assert len({i.group_id for i in items}) == 1 and items[0].group_id.startswith(source.lower())
    assert {i.group_label for i in items} == {f"{source} Set"}
    assert items[0].output_path == os.path.join(str(tmp_path), f"{source} Set")

    # Reading at A, searching at B -- nothing else was ever called (N6).
    for service in services.values():
        assert set(service.calls) <= READ_METHODS
    assert "search_tracks" not in services[source].calls or source == target
    assert "get_playlist_tracks" not in services[target].calls


def test_port_matrix_has_twelve_directions(tmp_path):
    controller, _queue, _services = make_controller(tmp_path)
    assert len(controller.directions()) == 12


def test_port_matrix_default_keeps_the_old_spotify_to_tidal_entries(tmp_path):
    controller, _queue, _services = make_controller(tmp_path)
    assert controller.direction == ("Spotify", "TIDAL")
    done, _errors = convert(controller)
    item = controller.enqueue_confident_only(done[0])[0]
    assert item.source["kind"] == "spotify_convert"
    assert {"spotify_track_id", "spotify_title", "tidal_track_id"} <= set(item.source)
    assert item.quality == "hifi"


def test_port_matrix_review_is_queued_in_the_direction_that_produced_it(tmp_path):
    """Switching the direction while the review is open must not re-target the result."""
    controller, queue, _services = make_controller(tmp_path)
    controller.set_direction("YouTube", "SoundCloud")
    done, _errors = convert(controller)
    report = done[0]
    controller.set_direction("Spotify", "TIDAL")
    items = controller.apply_review_and_enqueue({}, report=report)
    assert {i.platform for i in items} == {"SoundCloud"}


def test_port_matrix_same_service_is_not_a_direction(tmp_path):
    controller, _queue, _services = make_controller(tmp_path)
    with pytest.raises(Exception, match="Cannot port"):
        controller.set_direction("TIDAL", "TIDAL")
    assert controller.direction == ("Spotify", "TIDAL")


def test_port_matrix_unusable_target_explains_instead_of_matching(tmp_path):
    services = {name: FakeService(name) for name in SERVICES}
    services["TIDAL"] = UnavailableProvider("TIDAL", "Sign in to TIDAL on the Accounts tab first.")
    controller, queue, services = make_controller(tmp_path, services)
    done, errors = convert(controller)
    assert done == [] and errors == ["Sign in to TIDAL on the Accounts tab first."]
    assert "get_playlist_tracks" not in services["Spotify"].calls, "read the source for nothing"
    assert queue.list() == []


def test_port_matrix_changing_direction_drops_the_old_result(tmp_path):
    controller, _queue, _services = make_controller(tmp_path)
    done, _errors = convert(controller)
    assert controller.report is done[0]
    controller.set_direction("SoundCloud", "YouTube")
    assert controller.report is None


def test_port_matrix_own_playlists_come_from_the_source(tmp_path):
    controller, _queue, services = make_controller(tmp_path)
    controller.set_direction("SoundCloud", "TIDAL")
    roots, errors = [], []
    controller.load_playlists(roots.append, errors.append).join(5)
    assert errors == [] and [p.name for p in roots[0].all_playlists()] == ["SoundCloud Set"]
    assert services["SoundCloud"].calls == ["list_folders"]
    assert services["TIDAL"].calls == []


# ---------------------------------------------------------------------------
# Port tab widgets, through the real assembly (setup_tabs)
# ---------------------------------------------------------------------------

from test_playlists_tab import assemble, pump_until, window  # noqa: E402,F401  (fixture)


class TestPortMatrixTab:
    def test_port_matrix_tab_starts_as_spotify_to_tidal(self, window, tmp_path):
        _view, result = assemble(window, tmp_path)
        tab = result["tabs"]["port"]
        assert (tab.source_menu.get(), tab.target_menu.get()) == ("Spotify", "TIDAL")
        assert "Hi-Res" in tab.quality_label.cget("text")
        # The Accounts tab signs in through the same controller.
        assert result["tabs"]["accounts"].controller.spotify_controller is tab.controller

    def test_port_matrix_tab_picking_the_other_end_swaps(self, window, tmp_path):
        _view, result = assemble(window, tmp_path)
        tab = result["tabs"]["port"]
        tab.set_direction("YouTube", None)
        assert tab.controller.direction == ("YouTube", "TIDAL")
        tab.set_direction(None, "YouTube")  # same as the source: the ends swap
        assert tab.controller.direction == ("TIDAL", "YouTube")
        assert (tab.source_menu.get(), tab.target_menu.get()) == ("TIDAL", "YouTube")
        assert "YouTube" in tab.quality_label.cget("text")
        tab.swap()
        assert tab.controller.direction == ("YouTube", "TIDAL")

    def test_port_matrix_tab_explains_an_unreachable_target(self, window, tmp_path):
        """N3: FakeGui has no TIDAL module -- the tab says so before anything runs."""
        _view, result = assemble(window, tmp_path)
        tab = result["tabs"]["port"]
        assert pump_until(window, lambda: "TIDAL" in tab.hint_label.cget("text"))

    def test_port_matrix_tab_runs_a_port_and_queues_one_group(self, window, tmp_path):
        _view, result = assemble(window, tmp_path)
        tab, queue = result["tabs"]["port"], result["queue"]
        fakes = {name: FakeService(name) for name in SERVICES}
        tab.controller.providers.update({name: (lambda s=s: s) for name, s in fakes.items()})
        tab.set_direction("YouTube", "SoundCloud")

        tab.url_entry.insert(0, "https://youtube.example/playlist?list=PL1")
        tab.convert_from_url()
        assert pump_until(window, lambda: tab.report is not None), "the port never finished"
        assert "3 matched" in tab.status_label.cget("text")

        tab.queue_confident()
        items = queue.list()
        assert len(items) == 3 and {i.platform for i in items} == {"SoundCloud"}
        assert "from SoundCloud" in tab.status_label.cget("text")

    def test_port_matrix_tab_redraws_progress_at_most_every_100_ms(self, window, tmp_path, monkeypatch):
        """500 progress events per poll froze the window for 540 ms; now only some draw."""
        import hires.port_panel as port_panel

        _view, result = assemble(window, tmp_path)
        tab = result["tabs"]["port"]
        now = [1000.0]
        monkeypatch.setattr(port_panel.time, "monotonic", lambda: now[0])
        drawn = []
        monkeypatch.setattr(tab.progress, "set", drawn.append)

        for done in range(1, 500):
            tab._on_progress(done, 500, None)  # all within the same instant
        assert drawn == [1 / 500], "every event redrew the bar"
        now[0] += 0.2
        tab._on_progress(250, 500, None)
        tab._on_progress(500, 500, None)  # the last one always draws
        assert drawn == [1 / 500, 0.5, 1.0]
        assert "500/500" in tab.status_label.cget("text")
