"""Automatic re-checks of the account states (bounded, on a worker).

The clock is injected: ``wait`` records the requested pause and returns at
once, so the whole five-step schedule runs in milliseconds. Nothing sleeps.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.auto_refresh import DEFAULT_DELAYS, AutoRefresh  # noqa: E402
from hires.controllers import AccountsController  # noqa: E402
from hires.models import AccountState, AccountStatus  # noqa: E402
from test_controllers import SyncDispatcher, wait  # noqa: E402


class FakeClock:
    """``wait(event, seconds)`` that never sleeps; records what was asked."""

    def __init__(self):
        self.waits = []

    def __call__(self, event, seconds):
        self.waits.append(seconds)
        return event.is_set()


def statuses_from(states):
    """A provider that answers with the next state each time it is read."""
    states = list(states)
    reads = []

    def provider():
        state = states[min(len(reads), len(states) - 1)]
        reads.append(threading.current_thread())
        return AccountStatus(service="TIDAL", state=state)

    provider.reads = reads
    return provider


def controller(tidal, clock, **kwargs):
    return AccountsController(
        tidal_status_provider=tidal,
        spotify_status_provider=lambda: AccountStatus(service="Spotify", state=AccountState.SIGNED_IN),
        dispatch=SyncDispatcher(),
        auto_wait=clock,
        **kwargs,
    )


class Recorder:
    def __init__(self):
        self.updates = []
        self.checking = []
        self.waiting = []
        self.finished = []

    def callbacks(self):
        return {
            "on_waiting": lambda a, n, s: self.waiting.append((a, n, s)),
            "on_checking": lambda a, n: self.checking.append((a, n)),
            "on_finished": lambda done, n: self.finished.append((done, n)),
        }


class TestAutoRefreshSchedule:
    def test_auto_refresh_gives_up_after_five_rechecks(self):
        clock = FakeClock()
        tidal = statuses_from([AccountState.SIGNED_OUT])
        ctrl = controller(tidal, clock)
        rec = Recorder()

        wait(ctrl.start_auto_refresh(rec.updates.append, **rec.callbacks()))

        assert len(tidal.reads) == 1 + 5, "first read plus exactly five re-checks"
        assert clock.waits == list(DEFAULT_DELAYS) == [2.0, 4.0, 8.0, 15.0, 30.0]
        assert rec.checking == [(n, 5) for n in range(1, 6)]
        assert rec.finished == [(False, 5)]

    def test_auto_refresh_stops_early_once_everything_is_signed_in(self):
        clock = FakeClock()
        tidal = statuses_from([AccountState.SIGNED_OUT, AccountState.UNAVAILABLE, AccountState.SIGNED_IN])
        ctrl = controller(tidal, clock)
        rec = Recorder()

        wait(ctrl.start_auto_refresh(rec.updates.append, **rec.callbacks()))

        assert len(tidal.reads) == 3
        assert clock.waits == [2.0, 4.0]
        assert rec.finished == [(True, 2)]
        assert rec.updates[-1][0].is_signed_in

    def test_auto_refresh_never_starts_when_already_signed_in(self):
        clock = FakeClock()
        tidal = statuses_from([AccountState.SIGNED_IN])
        rec = Recorder()

        wait(controller(tidal, clock).start_auto_refresh(rec.updates.append, **rec.callbacks()))

        assert len(tidal.reads) == 1 and clock.waits == []
        assert rec.finished == [(True, 0)]

    def test_auto_refresh_runs_on_a_worker_thread(self):
        """The window must never wait for a status read."""
        release = threading.Event()
        entered = threading.Event()

        def slow():
            entered.set()
            release.wait(timeout=5)
            return AccountStatus(service="TIDAL", state=AccountState.SIGNED_IN)

        ctrl = controller(slow, FakeClock())
        thread = ctrl.start_auto_refresh(lambda _s: None)
        try:
            assert entered.wait(timeout=5)
            assert thread is not threading.current_thread()
            assert thread.daemon, "an open check must never keep the app from closing"
            assert ctrl.is_reading
        finally:
            release.set()
        wait(thread)
        assert not ctrl.is_reading


class TestAutoRefreshRestart:
    def test_auto_refresh_refresh_restarts_the_count(self):
        """Refresh cancels the running schedule and starts again from zero."""
        first_wait = threading.Event()

        class BlockingOnce(FakeClock):
            def __call__(self, event, seconds):
                self.waits.append(seconds)
                if len(self.waits) == 1:
                    first_wait.set()
                    return event.wait(timeout=5)  # parked until cancelled
                return event.is_set()

        clock = BlockingOnce()
        ctrl = controller(statuses_from([AccountState.SIGNED_OUT]), clock)
        old, new = Recorder(), Recorder()

        first = ctrl.start_auto_refresh(old.updates.append, **old.callbacks())
        assert first_wait.wait(timeout=5)
        second = ctrl.start_auto_refresh(new.updates.append, **new.callbacks())
        wait(first)
        wait(second)

        assert old.checking == [] and old.finished == [], "the cancelled run kept reporting"
        assert new.checking == [(n, 5) for n in range(1, 6)], "the count did not restart at 1"
        assert new.finished == [(False, 5)]

    def test_auto_refresh_stop_ends_the_run_silently(self):
        parked = threading.Event()

        def clock(event, _seconds):
            parked.set()
            return event.wait(timeout=5)

        ctrl = controller(statuses_from([AccountState.SIGNED_OUT]), clock)
        rec = Recorder()
        thread = ctrl.start_auto_refresh(rec.updates.append, **rec.callbacks())
        assert parked.wait(timeout=5)

        ctrl.stop_auto_refresh()
        wait(thread)

        assert rec.finished == [] and rec.checking == []

    def test_auto_refresh_a_failed_read_does_not_kill_the_schedule(self):
        """A read that failed (``None``) counts as "not done yet"."""
        answers = [None, [AccountStatus(service="X", state=AccountState.SIGNED_IN)]]
        updates, done = [], []
        refresher = AutoRefresh(
            lambda: answers.pop(0),
            lambda statuses: all(s.is_signed_in for s in statuses),
            lambda fn, *args: fn(*args),
            wait=FakeClock(),
        )

        wait(refresher.start(updates.append, on_finished=lambda ok, n: done.append((ok, n))))

        assert len(updates) == 1, "a failed read must not be painted"
        assert done == [(True, 1)]
