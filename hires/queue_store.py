"""Persistent, thread-safe download queue.

The store is the single source of truth for everything that should be
downloaded.  The GUI polls it for display, a background pump calls
:meth:`QueueStore.claim_next` to pick up the next unit of work.

Design notes:

* Every public method takes a :class:`threading.RLock`, so the store can be
  used from the GUI thread and the download thread at the same time.
* The file on disk is written atomically (temp file in the same directory +
  ``os.replace``), so a crash mid-write cannot leave a truncated queue behind.
* Loading never raises.  A missing, unreadable or corrupt file yields an empty
  queue plus a warning; individual broken entries are skipped.
* Subscriber callbacks fire *after* the lock has been released -- a callback
  that reaches back into the store from another thread would otherwise
  deadlock.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import QueueItem, QueueStats, QueueStatus

log = logging.getLogger(__name__)

#: Bumped whenever the on-disk layout changes in an incompatible way.
FILE_VERSION = 1

_STATS_FIELD = {
    QueueStatus.PENDING.value: "pending",
    QueueStatus.ACTIVE.value: "active",
    QueueStatus.DONE.value: "done",
    QueueStatus.FAILED.value: "failed",
    QueueStatus.CANCELLED.value: "cancelled",
}


def _status_value(status: Any) -> Optional[str]:
    """Accept either a :class:`QueueStatus` or a plain string."""
    if status is None:
        return None
    return getattr(status, "value", status)


class QueueStore:
    """A reorderable download queue backed by a JSON file."""

    def __init__(self, path: str, autosave: bool = True) -> None:
        self.path = str(path)
        self.autosave = autosave
        self._lock = threading.RLock()
        self._items: List[QueueItem] = []
        self._index: Dict[str, QueueItem] = {}
        self._paused = False
        self._subscribers: List[Callable[[], None]] = []
        self._load_locked()

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        """Write the queue to disk, regardless of the ``autosave`` setting."""
        with self._lock:
            self._save_locked()

    def reload(self) -> None:
        """Drop the in-memory state and read the file again."""
        with self._lock:
            self._load_locked()
        self._notify()

    def _maybe_save_locked(self) -> None:
        if self.autosave:
            self._save_locked()

    def _save_locked(self) -> None:
        payload = {
            "version": FILE_VERSION,
            "paused": self._paused,
            "items": [item.to_dict() for item in self._items],
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        tmp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".queue-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = None
        except OSError:
            # A failing disk must not take the whole download session down.
            log.exception("could not write queue file %s", self.path)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    log.warning("could not remove temp file %s", tmp_path)

    def _load_locked(self) -> None:
        self._items = []
        self._index = {}
        raw = self._read_file_locked()
        if raw is None:
            return

        if isinstance(raw, dict):
            self._paused = bool(raw.get("paused", False))
            entries = raw.get("items")
        elif isinstance(raw, list):
            # Tolerate the plain-list layout an older build may have written.
            entries = raw
        else:
            log.warning("queue file %s has an unexpected layout - ignoring it", self.path)
            return

        if not isinstance(entries, list):
            log.warning("queue file %s has no usable item list", self.path)
            return

        for entry in entries:
            if not isinstance(entry, dict):
                log.warning("skipping non-object queue entry: %r", entry)
                continue
            try:
                item = QueueItem.from_dict(entry)
            except (ValueError, TypeError) as exc:
                log.warning("skipping unusable queue entry (%s): %r", exc, entry)
                continue
            self._append_locked(item, requeue_active=True)

    def _read_file_locked(self) -> Any:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            # ValueError covers json.JSONDecodeError.
            log.warning("queue file %s is unreadable (%s) - starting empty", self.path, exc)
            return None

    # -- mutation -----------------------------------------------------------
    def add(self, item: QueueItem) -> QueueItem:
        """Append one item and return it (its ``id`` may have been replaced)."""
        with self._lock:
            self._append_locked(item)
            self._maybe_save_locked()
        self._notify()
        return item

    def add_many(self, items: Iterable[QueueItem]) -> List[QueueItem]:
        """Append several items with a single save and a single notification."""
        added: List[QueueItem] = []
        with self._lock:
            for item in items:
                self._append_locked(item)
                added.append(item)
            if added:
                self._maybe_save_locked()
        if added:
            self._notify()
        return added

    def _append_locked(self, item: QueueItem, requeue_active: bool = False) -> None:
        item.status = self._normalise_status(item.status, requeue_active=requeue_active)
        if requeue_active and item.status == QueueStatus.PENDING.value:
            item.started_at = None
        if not item.id or item.id in self._index:
            item.id = uuid.uuid4().hex
        self._items.append(item)
        self._index[item.id] = item

    @staticmethod
    def _normalise_status(status: Any, requeue_active: bool = False) -> str:
        """Map anything unexpected onto a status the rest of the code trusts."""
        try:
            known = QueueStatus(_status_value(status))
        except ValueError:
            return QueueStatus.PENDING.value
        if requeue_active and known is QueueStatus.ACTIVE:
            # The app died mid-download; queue it up again but keep ``attempts``
            # so the retry limit still applies.
            return QueueStatus.PENDING.value
        return known.value

    def remove(self, item_id: str) -> bool:
        with self._lock:
            item = self._index.pop(item_id, None)
            if item is None:
                return False
            self._items.remove(item)
            self._maybe_save_locked()
        self._notify()
        return True

    def clear_finished(self) -> int:
        """Drop every done/failed/cancelled item. Returns how many went away."""
        with self._lock:
            keep = [i for i in self._items if not i.is_finished]
            removed = len(self._items) - len(keep)
            if removed:
                self._items = keep
                self._index = {i.id: i for i in keep}
                self._maybe_save_locked()
        if removed:
            self._notify()
        return removed

    def clear_all(self) -> int:
        with self._lock:
            removed = len(self._items)
            if removed:
                self._items = []
                self._index = {}
                self._maybe_save_locked()
        if removed:
            self._notify()
        return removed

    # -- reading ------------------------------------------------------------
    def get(self, item_id: str) -> Optional[QueueItem]:
        with self._lock:
            return self._index.get(item_id)

    def list(self, status: Optional[str] = None, group_id: Optional[str] = None) -> List[QueueItem]:
        """Items in queue order, optionally filtered by status and/or group."""
        wanted = _status_value(status)
        with self._lock:
            return [
                item
                for item in self._items
                if (wanted is None or item.status == wanted)
                and (group_id is None or item.group_id == group_id)
            ]

    def stats(self) -> QueueStats:
        with self._lock:
            stats = QueueStats(total=len(self._items))
            for item in self._items:
                field = _STATS_FIELD.get(item.status)
                if field:
                    setattr(stats, field, getattr(stats, field) + 1)
            return stats

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    # -- work handout -------------------------------------------------------
    def next_pending(self) -> Optional[QueueItem]:
        """Peek at the next item a worker would pick up (``None`` if paused)."""
        with self._lock:
            return self._next_pending_locked()

    def _next_pending_locked(self) -> Optional[QueueItem]:
        if self._paused:
            return None
        for item in self._items:
            if item.status == QueueStatus.PENDING.value:
                return item
        return None

    def claim_next(self) -> Optional[QueueItem]:
        """Atomically take the next pending item and mark it active.

        Two callers can never receive the same item: selection and the status
        change happen under one lock.
        """
        with self._lock:
            item = self._next_pending_locked()
            if item is None:
                return None
            item.status = QueueStatus.ACTIVE.value
            item.started_at = time.time()
            item.finished_at = None
            item.error = None
            item.attempts += 1
            self._maybe_save_locked()
        self._notify()
        return item

    # -- outcome ------------------------------------------------------------
    def mark_done(self, item_id: str) -> bool:
        with self._lock:
            item = self._index.get(item_id)
            if item is None:
                return False
            item.status = QueueStatus.DONE.value
            item.error = None
            item.finished_at = time.time()
            self._maybe_save_locked()
        self._notify()
        return True

    def mark_failed(self, item_id: str, error: str) -> bool:
        """Record a failure. Re-queueing is left to the UI (see :meth:`retry`)."""
        with self._lock:
            item = self._index.get(item_id)
            if item is None:
                return False
            item.status = QueueStatus.FAILED.value
            item.error = str(error)
            item.finished_at = time.time()
            self._maybe_save_locked()
        self._notify()
        return True

    def mark_cancelled(self, item_id: str) -> bool:
        with self._lock:
            item = self._index.get(item_id)
            if item is None:
                return False
            item.status = QueueStatus.CANCELLED.value
            item.finished_at = time.time()
            self._maybe_save_locked()
        self._notify()
        return True

    def retry(self, item_id: str) -> bool:
        """Put a failed/cancelled item back in line. ``attempts`` is kept."""
        with self._lock:
            item = self._index.get(item_id)
            if item is None:
                return False
            if item.status not in (QueueStatus.FAILED.value, QueueStatus.CANCELLED.value):
                return False
            if item.attempts >= item.max_attempts:
                return False
            self._reset_to_pending(item)
            self._maybe_save_locked()
        self._notify()
        return True

    def requeue_failed(self) -> int:
        """Re-queue every failed item that has attempts left."""
        with self._lock:
            retryable = [item for item in self._items if item.can_retry]
            for item in retryable:
                self._reset_to_pending(item)
            if retryable:
                self._maybe_save_locked()
        if retryable:
            self._notify()
        return len(retryable)

    @staticmethod
    def _reset_to_pending(item: QueueItem) -> None:
        item.status = QueueStatus.PENDING.value
        item.error = None
        item.started_at = None
        item.finished_at = None

    # -- ordering -----------------------------------------------------------
    def move(self, item_id: str, delta: int) -> bool:
        """Shift an item by ``delta`` positions, clamped at both ends.

        Returns ``False`` only when the id is unknown; a move that would run
        past an edge is clamped and simply does nothing.
        """
        with self._lock:
            item = self._index.get(item_id)
            if item is None:
                return False
            moved = self._move_to_locked(item, self._items.index(item) + int(delta))
        if moved:
            self._notify()
        return True

    def move_to(self, item_id: str, index: int) -> bool:
        """Move an item to an absolute position, clamped to the queue bounds."""
        with self._lock:
            item = self._index.get(item_id)
            if item is None:
                return False
            moved = self._move_to_locked(item, int(index))
        if moved:
            self._notify()
        return True

    def _move_to_locked(self, item: QueueItem, target: int) -> bool:
        """Reposition ``item``; returns True when the order actually changed."""
        current = self._items.index(item)
        target = max(0, min(target, len(self._items) - 1))
        if target == current:
            return False
        self._items.pop(current)
        self._items.insert(target, item)
        self._maybe_save_locked()
        return True

    # -- pause --------------------------------------------------------------
    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_paused(self, paused: bool) -> None:
        """Stop handing out work. A running download is not interrupted."""
        with self._lock:
            paused = bool(paused)
            if paused == self._paused:
                return
            self._paused = paused
            self._maybe_save_locked()
        self._notify()

    # -- subscribers --------------------------------------------------------
    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a zero-argument callback; returns an unsubscribe callable."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _notify(self) -> None:
        """Fire all callbacks. Must be called with the lock released."""
        with self._lock:
            callbacks = list(self._subscribers)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # A broken listener must never roll back a queue mutation.
                log.exception("queue subscriber raised")
