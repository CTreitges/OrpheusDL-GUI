"""Bounded automatic re-checks of the account states.

A service that is not signed in when the Accounts tab opens is often just
slow -- the TIDAL module loads after the window is up, a token was entered a
moment ago. So the tab checks again by itself: a few times, with growing
waits, and then never again until the user presses Refresh. Never an endless
poll against a service's API.

Tkinter-free. Everything runs on one worker thread; results go out only
through the ``dispatch`` callable, which hands them to the UI thread. The
clock is injectable (``wait``), so tests run the whole schedule instantly.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional, Sequence

#: Waits before the re-checks: 2, 4, 8, 15, 30 s -- five re-checks within
#: about a minute.
DEFAULT_DELAYS = (2.0, 4.0, 8.0, 15.0, 30.0)


def _real_wait(event: threading.Event, seconds: float) -> bool:
    return event.wait(seconds)


class AutoRefresh:
    """Read, then re-read until ``is_done`` -- at most ``len(delays)`` times."""

    def __init__(
        self,
        read: Callable[[], Optional[List[Any]]],
        is_done: Callable[[List[Any]], bool],
        dispatch: Callable[..., None],
        *,
        delays: Optional[Sequence[float]] = None,
        wait: Optional[Callable[[threading.Event, float], bool]] = None,
        start_thread: Optional[Callable[[Callable[[], None]], threading.Thread]] = None,
    ):
        """
        Args:
            read: blocking read of all statuses; ``None`` when it failed.
            is_done: True when no further check is needed (all signed in).
            wait: ``wait(cancel_event, seconds) -> cancelled``.
            start_thread: runs the loop on a worker (daemon thread by default).
        """
        self._read = read
        self._is_done = is_done
        self._dispatch = dispatch
        self.delays = tuple(DEFAULT_DELAYS if delays is None else delays)
        self._wait = wait or _real_wait
        self._start_thread = start_thread or _daemon
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._generation = 0
        #: Number of the re-check in progress (0 = the first read).
        self.attempt = 0

    @property
    def limit(self) -> int:
        return len(self.delays)

    def start(
        self,
        on_update: Callable[[List[Any]], None],
        *,
        on_waiting: Optional[Callable[[int, int, float], None]] = None,
        on_checking: Optional[Callable[[int, int], None]] = None,
        on_finished: Optional[Callable[[bool, int], None]] = None,
    ) -> threading.Thread:
        """Start a fresh run; a run still going is cancelled first.

        Starting again is what Refresh does, so the count restarts at zero. A
        superseded run notices its generation is stale and stops before it
        dispatches anything, so two runs never interleave their updates.

        Callbacks: ``on_update(statuses)`` after every read,
        ``on_waiting(attempt, limit, seconds)`` before a wait,
        ``on_checking(attempt, limit)`` when a re-check starts,
        ``on_finished(done, rechecks)`` at the end of a run that was not
        cancelled.
        """
        with self._lock:
            self._cancel.set()
            cancel = threading.Event()
            self._cancel = cancel
            self._generation += 1
            generation = self._generation

        def current() -> bool:
            return not cancel.is_set() and generation == self._generation

        def finished(statuses) -> bool:
            return bool(statuses) and self._is_done(statuses)

        def work() -> None:
            self.attempt = 0
            statuses = self._read()
            if not current():
                return
            if statuses is not None:
                self._dispatch(on_update, statuses)
            attempt = 0
            while attempt < self.limit and not finished(statuses):
                delay = self.delays[attempt]
                attempt += 1
                if on_waiting is not None:
                    self._dispatch(on_waiting, attempt, self.limit, delay)
                if self._wait(cancel, delay) or not current():
                    return
                self.attempt = attempt
                if on_checking is not None:
                    self._dispatch(on_checking, attempt, self.limit)
                statuses = self._read()
                if not current():
                    return
                if statuses is not None:
                    self._dispatch(on_update, statuses)
            if on_finished is not None and current():
                self._dispatch(on_finished, finished(statuses), attempt)

        return self._start_thread(work)

    def stop(self) -> None:
        """Cancel the running run, e.g. because the tab is being destroyed."""
        with self._lock:
            self._cancel.set()
            self._generation += 1


def _daemon(fn: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=fn, name="hires-accounts-auto-refresh", daemon=True)
    thread.start()
    return thread
