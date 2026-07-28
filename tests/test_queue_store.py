"""Tests for the persistent download queue."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hires.models import QueueItem, QueueStatus  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402


def make_item(url: str = "https://tidal.com/track/1", **kwargs) -> QueueItem:
    kwargs.setdefault("title", url.rsplit("/", 1)[-1])
    return QueueItem(url=url, **kwargs)


def new_store(tmp_path, name: str = "queue.json", **kwargs) -> QueueStore:
    return QueueStore(str(tmp_path / name), **kwargs)


def urls(items) -> list:
    return [i.url for i in items]


# ---------------------------------------------------------------------------
# adding / reading
# ---------------------------------------------------------------------------

def test_add_keeps_queue_order(tmp_path):
    store = new_store(tmp_path)
    for n in range(3):
        store.add(make_item(f"u{n}"))
    assert urls(store.list()) == ["u0", "u1", "u2"]
    assert len(store) == 3


def test_add_many_returns_added_items(tmp_path):
    store = new_store(tmp_path)
    added = store.add_many([make_item("a"), make_item("b")])
    assert urls(added) == ["a", "b"]
    assert urls(store.list()) == ["a", "b"]


def test_add_replaces_duplicate_ids(tmp_path):
    store = new_store(tmp_path)
    first = store.add(make_item("a", id="dup"))
    second = store.add(make_item("b", id="dup"))
    assert first.id == "dup"
    assert second.id != "dup"
    assert store.get(first.id).url == "a"
    assert store.get(second.id).url == "b"


def test_list_filters_by_status_and_group(tmp_path):
    store = new_store(tmp_path)
    a = store.add(make_item("a", group_id="g1"))
    store.add(make_item("b", group_id="g1"))
    store.add(make_item("c", group_id="g2"))
    store.mark_done(a.id)

    assert urls(store.list(status=QueueStatus.PENDING)) == ["b", "c"]
    assert urls(store.list(status="done")) == ["a"]
    assert urls(store.list(group_id="g1")) == ["a", "b"]
    assert urls(store.list(status="pending", group_id="g1")) == ["b"]


def test_get_returns_none_for_unknown_id(tmp_path):
    store = new_store(tmp_path)
    assert store.get("nope") is None


# ---------------------------------------------------------------------------
# handing out work
# ---------------------------------------------------------------------------

def test_next_pending_returns_first_pending_in_order(tmp_path):
    store = new_store(tmp_path)
    a = store.add(make_item("a"))
    store.add(make_item("b"))
    assert store.next_pending().url == "a"

    store.mark_done(a.id)
    assert store.next_pending().url == "b"


def test_next_pending_is_none_on_empty_queue(tmp_path):
    assert new_store(tmp_path).next_pending() is None


def test_claim_next_sets_active_and_counts_attempt(tmp_path):
    store = new_store(tmp_path)
    store.add(make_item("a"))
    claimed = store.claim_next()

    assert claimed.status == QueueStatus.ACTIVE.value
    assert claimed.attempts == 1
    assert claimed.started_at is not None
    assert claimed.finished_at is None
    # No second pending item left to hand out.
    assert store.claim_next() is None


def test_claim_next_never_hands_out_the_same_item_twice(tmp_path):
    store = new_store(tmp_path)
    store.add_many([make_item(f"u{n}") for n in range(50)])

    claimed_per_thread = []
    lock = threading.Lock()
    start = threading.Event()

    def worker():
        mine = []
        start.wait(5)
        while True:
            item = store.claim_next()
            if item is None:
                break
            mine.append(item.id)
        with lock:
            claimed_per_thread.append(mine)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    all_ids = [item_id for mine in claimed_per_thread for item_id in mine]
    assert len(all_ids) == 50
    assert len(set(all_ids)) == 50
    for mine in claimed_per_thread:
        others = set(all_ids) - set(mine)
        assert others.isdisjoint(mine)
    assert all(i.status == QueueStatus.ACTIVE.value for i in store.list())
    assert all(i.attempts == 1 for i in store.list())


def test_pause_blocks_handout_without_touching_items(tmp_path):
    store = new_store(tmp_path)
    store.add(make_item("a"))
    running = store.claim_next()

    store.set_paused(True)
    store.add(make_item("b"))

    assert store.is_paused is True
    assert store.next_pending() is None
    assert store.claim_next() is None
    # The running download keeps its state.
    assert store.get(running.id).status == QueueStatus.ACTIVE.value

    store.set_paused(False)
    assert store.claim_next().url == "b"


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------

def test_mark_done_sets_finished_at(tmp_path):
    store = new_store(tmp_path)
    item = store.add(make_item("a"))
    assert store.mark_done(item.id) is True
    assert store.get(item.id).status == QueueStatus.DONE.value
    assert store.get(item.id).finished_at is not None
    assert store.mark_done("unknown") is False


def test_mark_failed_records_error_and_does_not_auto_retry(tmp_path):
    store = new_store(tmp_path)
    store.add(make_item("a"))
    item = store.claim_next()

    assert store.mark_failed(item.id, "403 forbidden") is True
    stored = store.get(item.id)
    assert stored.status == QueueStatus.FAILED.value
    assert stored.error == "403 forbidden"
    assert stored.finished_at is not None
    assert stored.attempts == 1
    # Retryable, but nothing re-queued itself behind the UI's back.
    assert stored.can_retry is True
    assert store.next_pending() is None


def test_mark_cancelled(tmp_path):
    store = new_store(tmp_path)
    item = store.add(make_item("a"))
    assert store.mark_cancelled(item.id) is True
    assert store.get(item.id).status == QueueStatus.CANCELLED.value
    assert store.next_pending() is None


def test_retry_requeues_failed_and_keeps_attempts(tmp_path):
    store = new_store(tmp_path)
    store.add(make_item("a"))
    item = store.claim_next()
    store.mark_failed(item.id, "boom")

    assert store.retry(item.id) is True
    stored = store.get(item.id)
    assert stored.status == QueueStatus.PENDING.value
    assert stored.error is None
    assert stored.finished_at is None
    assert stored.attempts == 1
    assert store.next_pending().id == item.id


def test_retry_refused_at_max_attempts(tmp_path):
    store = new_store(tmp_path)
    item = store.add(make_item("a", max_attempts=2))
    store.mark_failed(store.claim_next().id, "boom")
    assert store.retry(item.id) is True  # attempt 1 of 2 used
    store.mark_failed(store.claim_next().id, "boom again")

    assert store.get(item.id).attempts == 2
    assert store.retry(item.id) is False
    assert store.get(item.id).status == QueueStatus.FAILED.value


def test_retry_works_for_cancelled_and_ignores_pending(tmp_path):
    store = new_store(tmp_path)
    cancelled = store.add(make_item("a"))
    pending = store.add(make_item("b"))
    store.mark_cancelled(cancelled.id)

    assert store.retry(cancelled.id) is True
    assert store.get(cancelled.id).status == QueueStatus.PENDING.value
    assert store.retry(pending.id) is False
    assert store.retry("unknown") is False


def test_requeue_failed_only_touches_retryable_items(tmp_path):
    store = new_store(tmp_path)
    exhausted = store.add(make_item("a", max_attempts=1))
    retryable = store.add(make_item("b", max_attempts=3))
    cancelled = store.add(make_item("c"))
    store.mark_cancelled(cancelled.id)
    for _ in range(2):
        claimed = store.claim_next()
        store.mark_failed(claimed.id, "boom")

    assert store.requeue_failed() == 1
    assert store.get(retryable.id).status == QueueStatus.PENDING.value
    assert store.get(exhausted.id).status == QueueStatus.FAILED.value
    assert store.get(cancelled.id).status == QueueStatus.CANCELLED.value
    assert store.requeue_failed() == 0


# ---------------------------------------------------------------------------
# ordering / removal
# ---------------------------------------------------------------------------

def test_move_clamps_at_both_edges(tmp_path):
    store = new_store(tmp_path)
    a, b, c = store.add_many([make_item("a"), make_item("b"), make_item("c")])

    assert store.move(c.id, -1) is True
    assert urls(store.list()) == ["a", "c", "b"]
    assert store.move(a.id, -5) is True
    assert urls(store.list()) == ["a", "c", "b"]
    assert store.move(a.id, 99) is True
    assert urls(store.list()) == ["c", "b", "a"]
    assert store.move("unknown", 1) is False


def test_move_to_absolute_index(tmp_path):
    store = new_store(tmp_path)
    a, b, c = store.add_many([make_item("a"), make_item("b"), make_item("c")])

    assert store.move_to(c.id, 0) is True
    assert urls(store.list()) == ["c", "a", "b"]
    assert store.move_to(c.id, 42) is True
    assert urls(store.list()) == ["a", "b", "c"]
    assert store.move_to(c.id, -3) is True
    assert urls(store.list()) == ["c", "a", "b"]
    assert store.move_to("unknown", 0) is False


def test_remove_drops_item_and_index_entry(tmp_path):
    store = new_store(tmp_path)
    a = store.add(make_item("a"))
    store.add(make_item("b"))

    assert store.remove(a.id) is True
    assert store.get(a.id) is None
    assert urls(store.list()) == ["b"]
    assert store.remove(a.id) is False


def test_clear_finished_keeps_open_work(tmp_path):
    store = new_store(tmp_path)
    done = store.add(make_item("a"))
    failed = store.add(make_item("b"))
    cancelled = store.add(make_item("c"))
    store.add(make_item("d"))
    store.mark_done(done.id)
    store.mark_failed(failed.id, "boom")
    store.mark_cancelled(cancelled.id)

    assert store.clear_finished() == 3
    assert urls(store.list()) == ["d"]
    assert store.clear_finished() == 0


def test_clear_all(tmp_path):
    store = new_store(tmp_path)
    store.add_many([make_item("a"), make_item("b")])
    assert store.clear_all() == 2
    assert store.list() == []
    assert store.clear_all() == 0


def test_stats_counts_every_status(tmp_path):
    store = new_store(tmp_path)
    items = store.add_many([make_item(f"u{n}") for n in range(5)])
    store.mark_done(items[0].id)
    store.mark_failed(items[1].id, "boom")
    store.mark_cancelled(items[2].id)
    store.claim_next()  # picks items[3]

    stats = store.stats()
    assert (stats.total, stats.pending, stats.active) == (5, 1, 1)
    assert (stats.done, stats.failed, stats.cancelled) == (1, 1, 1)
    assert stats.remaining == 2


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def test_persistence_roundtrip_through_a_new_instance(tmp_path):
    store = new_store(tmp_path)
    done = store.add(make_item("a", title="A", group_id="g"))
    store.add(make_item("b", max_attempts=7))
    store.mark_done(done.id)
    store.set_paused(True)

    reopened = new_store(tmp_path)
    assert urls(reopened.list()) == ["a", "b"]
    assert reopened.get(done.id).status == QueueStatus.DONE.value
    assert reopened.get(done.id).group_id == "g"
    assert reopened.list()[1].max_attempts == 7
    assert reopened.is_paused is True


def test_autosave_off_writes_only_on_explicit_save(tmp_path):
    path = tmp_path / "queue.json"
    store = QueueStore(str(path), autosave=False)
    store.add(make_item("a"))
    assert not path.exists()

    store.save()
    assert path.exists()
    assert urls(QueueStore(str(path)).list()) == ["a"]


def test_save_creates_missing_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "queue.json"
    store = QueueStore(str(path))
    store.add(make_item("a"))
    assert path.exists()


def test_corrupt_file_yields_empty_queue_and_warns(tmp_path, caplog):
    path = tmp_path / "queue.json"
    path.write_text("{ this is not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        store = QueueStore(str(path))

    assert store.list() == []
    assert store.stats().total == 0
    assert any("unreadable" in record.message for record in caplog.records)
    # Still usable afterwards.
    store.add(make_item("a"))
    assert urls(QueueStore(str(path)).list()) == ["a"]


def test_unexpected_json_layout_yields_empty_queue(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text('"just a string"', encoding="utf-8")
    assert QueueStore(str(path)).list() == []


def test_entries_without_url_are_skipped(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"url": "a", "id": "1"},
                    {"title": "no url at all", "id": "2"},
                    "not even an object",
                    {"url": "c", "id": "3", "bogus_key": 1},
                ],
            }
        ),
        encoding="utf-8",
    )

    store = QueueStore(str(path))
    assert urls(store.list()) == ["a", "c"]


def test_active_items_come_back_as_pending_with_attempts_intact(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"url": "a", "id": "1", "status": "active", "attempts": 2, "started_at": 1.0},
                    {"url": "b", "id": "2", "status": "weird-status"},
                    {"url": "c", "id": "3", "status": "done", "attempts": 1},
                ],
            }
        ),
        encoding="utf-8",
    )

    store = QueueStore(str(path))
    crashed = store.get("1")
    assert crashed.status == QueueStatus.PENDING.value
    assert crashed.attempts == 2
    assert crashed.started_at is None
    # Unknown statuses fall back to pending, finished ones are kept.
    assert store.get("2").status == QueueStatus.PENDING.value
    assert store.get("3").status == QueueStatus.DONE.value
    assert store.next_pending().id == "1"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    store = new_store(tmp_path)
    items = store.add_many([make_item(f"u{n}") for n in range(5)])
    store.claim_next()
    store.mark_failed(items[0].id, "boom")
    store.move(items[4].id, -2)
    store.remove(items[3].id)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "queue.json"]
    assert leftovers == []
    assert os.path.exists(store.path)


def test_reload_discards_in_memory_changes(tmp_path):
    store = new_store(tmp_path)
    store.add(make_item("a"))
    other = QueueStore(store.path)
    other.add(make_item("b"))

    store.reload()
    assert urls(store.list()) == ["a", "b"]


# ---------------------------------------------------------------------------
# subscribers
# ---------------------------------------------------------------------------

def test_subscribe_fires_on_mutations_and_unsubscribe_stops_it(tmp_path):
    store = new_store(tmp_path)
    calls = []
    unsubscribe = store.subscribe(lambda: calls.append(1))

    item = store.add(make_item("a"))
    store.mark_done(item.id)
    assert len(calls) == 2

    # Read-only access must not notify.
    store.list()
    store.stats()
    store.get(item.id)
    assert len(calls) == 2

    unsubscribe()
    store.add(make_item("b"))
    assert len(calls) == 2


def test_callback_exception_does_not_break_the_mutation(tmp_path):
    store = new_store(tmp_path)
    calls = []

    def boom():
        raise RuntimeError("listener is broken")

    store.subscribe(boom)
    store.subscribe(lambda: calls.append(1))

    item = store.add(make_item("a"))
    assert urls(store.list()) == ["a"]
    assert store.mark_done(item.id) is True
    # The healthy listener still ran despite the broken one before it.
    assert len(calls) == 2


def test_callback_runs_without_the_store_lock_held(tmp_path):
    store = new_store(tmp_path)
    still_blocked = []
    seen = []

    def callback():
        # Another thread must be able to read the store from inside a callback.
        worker = threading.Thread(target=lambda: seen.append(len(store.list())))
        worker.start()
        worker.join(timeout=5)
        still_blocked.append(worker.is_alive())

    store.subscribe(callback)
    store.add(make_item("a"))

    assert still_blocked == [False]
    assert seen == [1]


def test_callback_may_mutate_the_store(tmp_path):
    store = new_store(tmp_path)
    seen = []

    def callback():
        # Re-entering from the notifying thread must not deadlock either.
        seen.append(len(store.list()))

    store.subscribe(callback)
    store.add(make_item("a"))
    store.add(make_item("b"))
    assert seen == [1, 2]


if __name__ == "__main__":  # pragma: no cover - convenience for manual runs
    raise SystemExit(pytest.main([__file__, "-q"]))
