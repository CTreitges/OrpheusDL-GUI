"""Queue groups, moving a group to the front, and "Clear all".

Headless: the store, the controller and the real runtime against the fake GUI
from ``test_integration``. The widget side lives in ``test_queue_panel.py``.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.controllers import QueueController  # noqa: E402
from hires.integration import HiresRuntime  # noqa: E402
from hires.models import QueueItem, QueueStatus  # noqa: E402
from hires.queue_groups import (  # noqa: E402
    SINGLES_KEY,
    SINGLES_LABEL,
    build_groups,
    group_key,
)
from hires.queue_store import QueueStore  # noqa: E402
from test_integration import FakeGui, finish_download  # noqa: E402


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "queue.json")


@pytest.fixture
def queue(path):
    return QueueStore(path)


def playlist(group, count, label=None):
    """``count`` tracks of one playlist run, the way the converter queues them."""
    return [
        QueueItem(
            url=f"https://tidal.com/browse/track/{group}{n}",
            title=f"{group}{n}",
            group_id=group,
            group_label=label or f"Playlist {group}",
        )
        for n in range(1, count + 1)
    ]


def titles(items):
    return [i.title for i in items]


# ---------------------------------------------------------------------------
# What belongs together
# ---------------------------------------------------------------------------

class TestGroupKey:
    def test_group_id_wins(self):
        item = QueueItem(url="u", group_id="run-1", group_label="Mix")
        assert group_key(item) == "id:run-1"

    def test_label_groups_playlists_queued_as_one_item(self):
        """A TIDAL playlist queued whole carries only its name."""
        a = QueueItem(url="u1", group_label="Warm-up")
        b = QueueItem(url="u2", group_label="Warm-up")
        assert group_key(a) == group_key(b) == "label:Warm-up"

    def test_two_runs_of_the_same_playlist_stay_apart(self):
        first = QueueItem(url="u", group_id="spotify-x-1", group_label="Mix")
        second = QueueItem(url="u", group_id="spotify-x-2", group_label="Mix")
        assert group_key(first) != group_key(second)

    def test_single_tracks_share_one_collective_group(self):
        assert group_key(QueueItem(url="u1")) == group_key(QueueItem(url="u2")) == SINGLES_KEY


class TestBuildGroups:
    def test_counts_and_progress(self, queue):
        queue.add_many(playlist("A", 3))
        ids = [i.id for i in queue.list()]
        queue.mark_done(ids[0])
        queue.mark_failed(ids[1], "boom")

        (group,) = build_groups(queue.list())
        assert (group.total, group.done, group.failed, group.pending) == (3, 1, 1, 1)
        assert group.label == "Playlist A"
        assert group.progress_text == "1 of 3 done · 1 failed"

    def test_running_group_first_then_waiting_then_finished(self, queue):
        queue.add_many(playlist("Done", 1) + playlist("Wait", 1) + playlist("Run", 2))
        items = queue.list()
        queue.mark_done(items[0].id)
        # Make "Run" the one downloading: move it first, claim, move it back.
        queue.move_group_to_front("id:Run")
        queue.claim_next()
        queue.move_to(queue.list()[0].id, 3)

        order = [g.label for g in build_groups(queue.list())]
        assert order == ["Playlist Run", "Playlist Wait", "Playlist Done"]

    def test_singles_have_a_readable_name(self, queue):
        queue.add(QueueItem(url="u1", title="Loose track"))
        (group,) = build_groups(queue.list())
        assert group.key == SINGLES_KEY
        assert group.label == SINGLES_LABEL

    def test_two_thousand_items_in_twenty_groups(self, queue):
        for g in range(20):
            queue.add_many(playlist(f"G{g:02d}-", 100))
        groups = build_groups(queue.list())
        assert len(groups) == 20
        assert sum(g.total for g in groups) == 2000


class TestLegacyQueueFileLoadsWithoutLoss:
    """A queue written before groups existed must load untouched (W8)."""

    def test_queue_group_legacy_file(self, path):
        legacy = {
            "version": 1,
            "paused": False,
            "items": [
                {"url": "https://tidal.com/browse/track/1", "title": "One", "status": "pending"},
                {"url": "https://tidal.com/browse/track/2", "title": "Two", "status": "done"},
                {
                    "url": "https://tidal.com/browse/playlist/abc",
                    "title": "Old playlist",
                    "media_kind": "playlist",
                    "status": "pending",
                    "an_unknown_future_field": 42,
                },
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)

        store = QueueStore(path)

        assert titles(store.list()) == ["One", "Two", "Old playlist"]
        assert [i.status for i in store.list()] == ["pending", "done", "pending"]
        (group,) = build_groups(store.list())
        assert group.key == SINGLES_KEY and group.total == 3

    def test_queue_group_plain_list_layout(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{"url": "u1", "title": "A"}, {"url": "u2", "title": "B"}], fh)
        assert titles(QueueStore(path).list()) == ["A", "B"]


# ---------------------------------------------------------------------------
# Moving a group to the front
# ---------------------------------------------------------------------------

class TestGroupPriorityInTheStore:
    def test_group_priority_next_claim_is_b_then_rest_of_a(self, queue):
        queue.add_many(playlist("A", 3) + playlist("B", 2))
        running = queue.claim_next()
        assert running.title == "A1"

        assert queue.move_group_to_front("id:B") == 2

        # The running track is untouched ...
        assert queue.get(running.id).status == QueueStatus.ACTIVE.value
        # ... and the claims go B first, then A carries on in its old order.
        queue.mark_done(running.id)
        claimed = []
        while True:
            item = queue.claim_next()
            if item is None:
                break
            claimed.append(item.title)
            queue.mark_done(item.id)
        assert claimed == ["B1", "B2", "A2", "A3"]

    def test_group_priority_survives_a_restart(self, queue, path):
        queue.add_many(playlist("A", 2) + playlist("B", 2))
        queue.move_group_to_front("id:B")
        assert titles(QueueStore(path).list()) == ["B1", "B2", "A1", "A2"]

    def test_group_priority_keeps_other_groups_in_order(self, queue):
        queue.add_many(playlist("A", 2) + playlist("C", 1) + playlist("B", 1))
        queue.move_group_to_front("id:B")
        assert titles(queue.list()) == ["B1", "A1", "A2", "C1"]

    def test_group_priority_unknown_group_changes_nothing(self, queue):
        queue.add_many(playlist("A", 2))
        assert queue.move_group_to_front("id:nope") == 0
        assert titles(queue.list()) == ["A1", "A2"]


class TestGroupPriorityInTheRuntime:
    """The rule through the real pump: what gui.py is actually handed."""

    def test_group_priority_running_track_finishes_then_b_then_a(self, queue):
        gui = FakeGui(output_path=None)  # no folder -> every finish counts as done
        runtime = HiresRuntime(gui, queue, logger=lambda _line: None)
        queue.add_many(playlist("A", 3) + playlist("B", 2))

        runtime._pump()
        assert [c[0][-2:] for c in gui.calls] == ["A1"]

        QueueController(queue, stop_active=runtime.stop_active).move_group_to_front("id:B")

        # Moving must not interrupt the running download.
        assert not gui.stop_event.is_set()
        assert runtime._active_id is not None
        runtime._pump()  # still busy: nothing new is handed over
        assert len(gui.calls) == 1

        for _ in range(4):
            finish_download(runtime, gui)  # current one ends ...
            runtime._pump()  # ... and the next is dispatched

        assert [c[0][-2:] for c in gui.calls] == ["A1", "B1", "B2", "A2", "A3"]


class TestRemoveGroup:
    def test_remove_group_keeps_the_running_track(self, queue):
        queue.add_many(playlist("A", 3) + playlist("B", 1))
        running = queue.claim_next()

        assert queue.remove_group("id:A") == 2
        assert titles(queue.list()) == ["A1", "B1"]
        assert queue.get(running.id).status == QueueStatus.ACTIVE.value

    def test_remove_group_of_singles(self, queue):
        queue.add_many([QueueItem(url="u1"), QueueItem(url="u2")] + playlist("A", 1))
        assert queue.remove_group(SINGLES_KEY) == 2
        assert titles(queue.list()) == ["A1"]


# ---------------------------------------------------------------------------
# Clear all
# ---------------------------------------------------------------------------

class TestClearAll:
    def test_clear_all_question_names_the_count(self, queue):
        controller = QueueController(queue)
        assert controller.clear_all_question() is None  # nothing to ask about

        queue.add_many(playlist("A", 3))
        question = controller.clear_all_question()
        assert "3 entries" in question
        assert "stopped" not in question

    def test_clear_all_question_warns_about_the_running_track(self, queue):
        queue.add_many(playlist("A", 2))
        queue.claim_next()
        assert "will be stopped" in QueueController(queue).clear_all_question()

    def test_clear_all_stops_the_running_download_then_empties(self, queue, path):
        gui = FakeGui(output_path=None)
        stops = []
        gui.stop_download = lambda: (stops.append(1), gui.stop_event.set())
        runtime = HiresRuntime(gui, queue, logger=lambda _line: None)
        queue.add_many(playlist("A", 3))
        runtime._pump()

        removed = QueueController(queue, stop_active=runtime.stop_active).clear_all()

        assert removed == 3
        assert stops == [1], "the running download was not stopped"
        assert QueueStore(path).list() == [], "the cleared queue came back after a reload"

        # The runner copes with its item having vanished.
        finish_download(runtime, gui)
        assert runtime._active_id is None
        assert queue.list() == []

    def test_clear_all_without_a_running_item_stops_nothing(self, queue):
        stops = []
        queue.add_many(playlist("A", 2))
        QueueController(queue, stop_active=lambda: stops.append(1)).clear_all()
        assert stops == []
        assert queue.list() == []

    def test_clear_all_runtime_never_stops_a_download_it_did_not_start(self, queue):
        gui = FakeGui()
        gui.stop_download = lambda: gui.stop_event.set()
        runtime = HiresRuntime(gui, queue, logger=lambda _line: None)
        assert runtime.stop_active() is False
        assert not gui.stop_event.is_set()
