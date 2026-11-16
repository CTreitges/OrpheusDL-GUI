"""Grouping of queue items by the playlist they came from.

Tkinter-free, so the rules are testable headlessly. Nothing here is stored:
a group is derived from fields every queue item already carries, which means
an old ``hires_queue.json`` written before groups existed loads unchanged --
its items simply land in the "Single tracks" group.

Which group an item belongs to, in order of precedence:

1. ``group_id`` -- set by the converter and the TIDAL track picker for one
   playlist run, so two runs of the same playlist stay apart.
2. ``group_label`` -- a playlist queued as one item, or several playlists
   queued together from one folder, carry the folder/playlist name here.
3. Neither -- a track queued on its own. All of these share one group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from .models import QueueItem, QueueStatus

#: Key and label of the collective group for tracks queued on their own.
SINGLES_KEY = "singles"
SINGLES_LABEL = "Single tracks"


def group_key(item: QueueItem) -> str:
    """The group ``item`` belongs to. Stable for the life of the item."""
    if item.group_id:
        return f"id:{item.group_id}"
    if item.group_label:
        return f"label:{item.group_label}"
    return SINGLES_KEY


def group_label(item: QueueItem) -> str:
    """What the group of ``item`` is called on screen."""
    if group_key(item) == SINGLES_KEY:
        return SINGLES_LABEL
    return item.group_label or item.group_id or SINGLES_LABEL


@dataclass
class QueueGroup:
    """One playlist (or the single tracks) as the queue tab shows it."""

    key: str
    label: str
    #: Item ids in queue order.
    item_ids: List[str] = field(default_factory=list)
    total: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0
    pending: int = 0
    active: int = 0
    #: Position of the group's first item in the queue -- the sort key.
    first_index: int = 0

    @property
    def is_running(self) -> bool:
        return self.active > 0

    @property
    def is_finished(self) -> bool:
        return self.pending == 0 and self.active == 0

    @property
    def finished(self) -> int:
        return self.done + self.failed + self.cancelled

    @property
    def progress_text(self) -> str:
        """``12 of 60 done · 1 failed`` -- read constantly, so kept short."""
        text = f"{self.done} of {self.total} done"
        if self.failed:
            text += f" · {self.failed} failed"
        if self.active:
            text += " · running"
        return text


_COUNTER = {
    QueueStatus.PENDING.value: "pending",
    QueueStatus.ACTIVE.value: "active",
    QueueStatus.DONE.value: "done",
    QueueStatus.FAILED.value: "failed",
    QueueStatus.CANCELLED.value: "cancelled",
}


def build_groups(items: Iterable[QueueItem]) -> List[QueueGroup]:
    """Group ``items`` (in queue order) and sort the groups for display.

    Display order: the group with the running download first -- it is what
    the user is looking for -- then every group that still has work, in queue
    order (which is also the order they will be downloaded in), then groups
    that are completely finished. One pass, so a queue of thousands of items
    costs next to nothing.
    """
    groups: dict = {}
    for index, item in enumerate(items):
        key = group_key(item)
        group = groups.get(key)
        if group is None:
            group = QueueGroup(key=key, label=group_label(item), first_index=index)
            groups[key] = group
        group.item_ids.append(item.id)
        group.total += 1
        counter = _COUNTER.get(item.status)
        if counter:
            setattr(group, counter, getattr(group, counter) + 1)

    def order(group: QueueGroup):
        if group.is_running:
            rank = 0
        elif not group.is_finished:
            rank = 1
        else:
            rank = 2
        return (rank, group.first_index)

    return sorted(groups.values(), key=order)


def find_group(groups: Iterable[QueueGroup], key: str) -> Optional[QueueGroup]:
    for group in groups:
        if group.key == key:
            return group
    return None
