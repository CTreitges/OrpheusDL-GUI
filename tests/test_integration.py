"""Tests for hires.integration -- the bridge into gui.py.

The GUI is faked here: a plain object carrying the same globals and functions
that gui.py exposes. That is exactly the surface the runtime is allowed to
touch, so these tests double as a contract check -- if gui.py ever renames one
of them, the fake stops matching reality and this file needs updating too.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires import integration  # noqa: E402
from hires.integration import (  # noqa: E402
    DISPATCH_GRACE_SEC,
    HiresRuntime,
    audio_snapshot,
    default_queue_path,
    install,
)
from hires.models import QueueItem, QueueStatus  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402


class FakeApp:
    """Stands in for the tkinter root: records `after` callbacks."""

    def __init__(self):
        self.scheduled = []
        self.cancelled = []

    def after(self, delay, fn=None):
        self.scheduled.append((delay, fn))
        return f"after#{len(self.scheduled)}"

    def after_cancel(self, token):
        self.cancelled.append(token)


class FakeGui:
    """The subset of gui.py globals the runtime is allowed to use."""

    def __init__(self, output_path=None, start_result=None, start_raises=None):
        self.app = FakeApp()
        self.download_process_active = False
        self.file_download_queue = []
        #: Non-None from the moment a batch starts until it is fully finished --
        #: including the pause between two batch items.
        self.current_batch_output_path = None
        #: gui.py sets this once the library is up. Downloads refuse to start
        #: (with a modal error box) while it is None.
        self.orpheus_instance = object()
        self.stop_event = threading.Event()
        self.application_path = "/tmp/fake-app"
        self.current_settings = {
            "globals": {"general": {"output_path": output_path, "quality": "hifi"}},
            "credentials": {"Spotify": {"client_id": "cid", "client_secret": "secret"}},
        }
        self.calls = []
        self._start_result = start_result
        self._start_raises = start_raises

    def _start_single_download(self, url, path, search_result_data=None):
        self.calls.append((url, path, search_result_data))
        if self._start_raises:
            raise self._start_raises
        # Mimic gui.py: the download runs on its own thread, so the flag flips
        # to True and only clears once it is done.
        if self._start_result is not False:
            self.download_process_active = True
        return self._start_result


@pytest.fixture
def queue(tmp_path):
    return QueueStore(str(tmp_path / "queue.json"))


def make_runtime(gui, queue, **kwargs):
    logs = []
    runtime = HiresRuntime(gui, queue, logger=logs.append, **kwargs)
    runtime.logs = logs
    return runtime


def write_audio(folder, name="track.flac"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 32)
    return path


def finish_download(runtime, gui):
    """Simulate the GUI's download thread finishing."""
    gui.download_process_active = False
    runtime._active_since = 0.0  # skip the dispatch grace period
    runtime._pump()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestAudioSnapshot:
    def test_finds_audio_recursively(self, tmp_path):
        write_audio(str(tmp_path), "a.flac")
        write_audio(str(tmp_path / "sub"), "b.mp3")
        assert audio_snapshot(str(tmp_path))[0] == 2

    def test_ignores_non_audio(self, tmp_path):
        write_audio(str(tmp_path), "cover.jpg")
        write_audio(str(tmp_path), "playlist.m3u")
        assert audio_snapshot(str(tmp_path)) == (0, 0.0)

    def test_missing_folder_is_empty(self, tmp_path):
        assert audio_snapshot(str(tmp_path / "nope")) == (0, 0.0)

    def test_empty_path_is_empty(self):
        assert audio_snapshot("") == (0, 0.0)

    def test_extension_matching_is_case_insensitive(self, tmp_path):
        write_audio(str(tmp_path), "TRACK.FLAC")
        assert audio_snapshot(str(tmp_path))[0] == 1

    def test_reports_the_newest_mtime(self, tmp_path):
        write_audio(str(tmp_path), "old.flac")
        os.utime(str(tmp_path / "old.flac"), (1_000_000, 1_000_000))
        write_audio(str(tmp_path / "sub"), "new.flac")
        os.utime(str(tmp_path / "sub" / "new.flac"), (2_000_000, 2_000_000))

        count, newest = audio_snapshot(str(tmp_path))
        assert count == 2
        assert newest == pytest.approx(2_000_000, abs=1)

    def test_a_non_audio_file_does_not_set_the_mtime(self, tmp_path):
        """Otherwise a cover.jpg written by the tagger reads as 'audio appeared'."""
        write_audio(str(tmp_path), "cover.jpg")
        os.utime(str(tmp_path / "cover.jpg"), (9_000_000, 9_000_000))
        assert audio_snapshot(str(tmp_path)) == (0, 0.0)


class TestDefaultQueuePath:
    def test_uses_the_app_config_folder(self):
        gui = FakeGui()
        assert default_queue_path(gui) == os.path.join(
            "/tmp/fake-app", "config", "hires_queue.json"
        )

    def test_falls_back_to_cwd(self):
        assert default_queue_path(None).endswith(os.path.join("config", "hires_queue.json"))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_schedules_a_poll(self, queue):
        gui = FakeGui()
        runtime = make_runtime(gui, queue)

        assert runtime.start() is True
        assert len(gui.app.scheduled) == 1

    def test_start_is_idempotent(self, queue):
        gui = FakeGui()
        runtime = make_runtime(gui, queue)
        runtime.start()
        runtime.start()

        assert len(gui.app.scheduled) == 1

    def test_start_without_an_app_fails_gracefully(self, queue):
        gui = FakeGui()
        gui.app = None
        runtime = make_runtime(gui, queue)

        assert runtime.start() is False
        assert any("not available" in line for line in runtime.logs)

    def test_stop_cancels_the_timer(self, queue):
        gui = FakeGui()
        runtime = make_runtime(gui, queue)
        runtime.start()
        runtime.stop()

        assert gui.app.cancelled

    def test_tick_reschedules_even_after_an_error(self, queue, monkeypatch):
        gui = FakeGui()
        runtime = make_runtime(gui, queue)
        runtime.start()
        gui.app.scheduled.clear()

        monkeypatch.setattr(runtime, "_pump", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        runtime._tick()

        assert len(gui.app.scheduled) == 1  # timer survived
        assert any("boom" in line for line in runtime.logs)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_claims_and_starts_the_next_item(self, queue, tmp_path):
        gui = FakeGui(output_path=str(tmp_path))
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="https://tidal.com/browse/track/1", title="T", quality="hifi"))

        runtime._pump()

        assert len(gui.calls) == 1
        url, path, data = gui.calls[0]
        assert url == "https://tidal.com/browse/track/1"
        assert path == str(tmp_path)
        assert data["extra_kwargs"]["download_quality_override"] == "hifi"
        assert queue.list()[0].status == QueueStatus.ACTIVE.value

    def test_item_output_path_wins_over_the_default(self, queue, tmp_path):
        gui = FakeGui(output_path=str(tmp_path / "default"))
        runtime = make_runtime(gui, queue)
        target = str(tmp_path / "custom")
        queue.add(QueueItem(url="u", output_path=target))

        runtime._pump()

        assert gui.calls[0][1] == target
        assert os.path.isdir(target)  # created up front

    def test_does_not_dispatch_while_a_download_runs(self, queue):
        gui = FakeGui()
        gui.download_process_active = True
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()

        assert gui.calls == []
        assert queue.list()[0].status == QueueStatus.PENDING.value

    def test_does_not_dispatch_while_a_batch_is_pending(self, queue):
        gui = FakeGui()
        gui.file_download_queue = ["https://example.com/other"]
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()

        assert gui.calls == []

    def test_paused_queue_dispatches_nothing(self, queue):
        gui = FakeGui()
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))
        queue.set_paused(True)

        runtime._pump()

        assert gui.calls == []

    def test_empty_queue_is_a_no_op(self, queue):
        gui = FakeGui()
        make_runtime(gui, queue)._pump()
        assert gui.calls == []

    def test_only_one_item_at_a_time(self, queue):
        gui = FakeGui()
        runtime = make_runtime(gui, queue)
        queue.add_many([QueueItem(url="u1"), QueueItem(url="u2")])

        runtime._pump()
        runtime._pump()  # still busy

        assert len(gui.calls) == 1

    def test_missing_entry_point_fails_the_item(self, queue):
        gui = FakeGui()
        gui._start_single_download = None  # gui.py renamed or not loaded yet
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()

        item = queue.list()[0]
        assert item.status == QueueStatus.FAILED.value
        assert "entry point" in item.error

    def test_exception_on_start_fails_the_item(self, queue):
        gui = FakeGui(start_raises=RuntimeError("kaboom"))
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()

        item = queue.list()[0]
        assert item.status == QueueStatus.FAILED.value
        assert "kaboom" in item.error
        assert runtime._active_id is None  # not stuck

    def test_gui_refusal_requeues_the_item(self, queue):
        gui = FakeGui(start_result=False)
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u", max_attempts=3))

        runtime._pump()

        item = queue.list()[0]
        assert item.status == QueueStatus.PENDING.value
        assert item.attempts == 1
        assert runtime._active_id is None

    def test_persistent_refusal_gives_up_after_max_attempts(self, queue):
        gui = FakeGui(start_result=False)
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u", max_attempts=2))

        for _ in range(5):
            runtime._pump()

        item = queue.list()[0]
        assert item.status == QueueStatus.FAILED.value
        assert item.attempts == 2  # stopped instead of spinning forever


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

class TestCompletion:
    def test_new_audio_file_means_success(self, queue, tmp_path):
        target = str(tmp_path / "out")
        gui = FakeGui(output_path=target)
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()
        write_audio(target, "downloaded.flac")
        finish_download(runtime, gui)

        assert queue.list()[0].status == QueueStatus.DONE.value

    def test_no_audio_file_means_failure(self, queue, tmp_path):
        gui = FakeGui(output_path=str(tmp_path / "out"))
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()
        finish_download(runtime, gui)

        item = queue.list()[0]
        assert item.status == QueueStatus.FAILED.value
        assert "No audio file" in item.error

    def test_pre_existing_files_do_not_count_as_success(self, queue, tmp_path):
        target = str(tmp_path / "out")
        write_audio(target, "old.flac")
        gui = FakeGui(output_path=target)
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()
        finish_download(runtime, gui)

        assert queue.list()[0].status == QueueStatus.FAILED.value

    def test_overwriting_an_existing_file_counts_as_success(self, queue, tmp_path):
        target = str(tmp_path / "out")
        existing = write_audio(target, "same.flac")
        old = os.path.getmtime(existing) - 3600
        os.utime(existing, (old, old))

        gui = FakeGui(output_path=target)
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()
        os.utime(existing, None)  # re-downloaded, same filename
        finish_download(runtime, gui)

        assert queue.list()[0].status == QueueStatus.DONE.value

    def test_cancellation_marks_cancelled_not_failed(self, queue, tmp_path):
        gui = FakeGui(output_path=str(tmp_path / "out"))
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()
        gui.stop_event.set()
        finish_download(runtime, gui)

        assert queue.list()[0].status == QueueStatus.CANCELLED.value

    def test_unknown_target_folder_gets_benefit_of_the_doubt(self, queue):
        gui = FakeGui(output_path=None)
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()
        finish_download(runtime, gui)

        assert queue.list()[0].status == QueueStatus.DONE.value

    def test_grace_period_prevents_a_premature_verdict(self, queue, tmp_path):
        # gui.py starts the download on a thread; for a moment the flag is
        # still False. Without the grace period we would call it finished.
        gui = FakeGui(output_path=str(tmp_path))
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))

        runtime._pump()
        gui.download_process_active = False  # thread has not flipped it yet
        runtime._pump()

        assert queue.list()[0].status == QueueStatus.ACTIVE.value
        assert time.time() - runtime._active_since < DISPATCH_GRACE_SEC

    def test_next_item_starts_after_the_previous_finishes(self, queue, tmp_path):
        target = str(tmp_path / "out")
        gui = FakeGui(output_path=target)
        runtime = make_runtime(gui, queue)
        queue.add_many([QueueItem(url="u1"), QueueItem(url="u2")])

        runtime._pump()
        write_audio(target, "one.flac")
        finish_download(runtime, gui)
        runtime._pump()

        assert [c[0] for c in gui.calls] == ["u1", "u2"]


class TestReportResult:
    def test_success_overrides_the_heuristic(self, queue, tmp_path):
        gui = FakeGui(output_path=str(tmp_path / "out"))
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))
        runtime._pump()

        runtime.report_result(True)

        assert queue.list()[0].status == QueueStatus.DONE.value
        assert runtime._active_id is None

    def test_failure_records_the_message(self, queue, tmp_path):
        gui = FakeGui(output_path=str(tmp_path))
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="u"))
        runtime._pump()

        runtime.report_result(False, "quota exceeded")

        assert queue.list()[0].error == "quota exceeded"

    def test_without_an_active_item_it_is_a_no_op(self, queue):
        runtime = make_runtime(FakeGui(), queue)
        runtime.report_result(True)  # must not raise


# ---------------------------------------------------------------------------
# Settings access
# ---------------------------------------------------------------------------

class TestSettings:
    def test_reads_output_path_and_quality(self, queue, tmp_path):
        gui = FakeGui(output_path=str(tmp_path))
        runtime = make_runtime(gui, queue)

        assert runtime.default_output_path() == str(tmp_path)
        assert runtime.default_quality() == "hifi"

    def test_accepts_the_orpheus_key_name(self, queue):
        gui = FakeGui()
        gui.current_settings = {"globals": {"general": {"download_path": "/music", "download_quality": "lossless"}}}
        runtime = make_runtime(gui, queue)

        assert runtime.default_output_path() == "/music"
        assert runtime.default_quality() == "lossless"

    def test_missing_settings_return_none(self, queue):
        gui = FakeGui()
        gui.current_settings = None
        runtime = make_runtime(gui, queue)

        assert runtime.default_output_path() is None
        assert runtime.default_quality() is None

    def test_blank_values_are_treated_as_missing(self, queue):
        gui = FakeGui()
        gui.current_settings = {"globals": {"general": {"output_path": "   "}}}
        assert make_runtime(gui, queue).default_output_path() is None

    def test_spotify_credentials_are_reused_from_gui_settings(self):
        gui = FakeGui()
        assert integration._spotify_credentials(gui) == ("cid", "secret")

    def test_spotify_credentials_missing(self):
        gui = FakeGui()
        gui.current_settings = {}
        assert integration._spotify_credentials(gui) == ("", "")


# ---------------------------------------------------------------------------
# install / setup_tabs must never break the GUI
# ---------------------------------------------------------------------------

class TestInstall:
    def test_install_returns_a_running_runtime(self, tmp_path):
        gui = FakeGui()
        runtime = install(gui, queue_path=str(tmp_path / "q.json"))

        assert isinstance(runtime, HiresRuntime)
        assert gui.app.scheduled

    def test_install_never_raises(self, tmp_path):
        gui = FakeGui()
        gui.app = None  # start() will refuse
        assert install(gui, queue_path=str(tmp_path / "q.json")) is not None

    def test_install_survives_an_unwritable_queue_path(self):
        gui = FakeGui()
        # A path that cannot exist (a file used as a directory). The store
        # degrades to in-memory rather than taking the GUI down with it.
        runtime = install(gui, queue_path="/dev/null/queue.json")

        assert runtime is not None
        assert runtime.queue.list() == []

    def test_setup_tabs_degrades_without_customtkinter(self, tmp_path, monkeypatch):
        # tkinter is genuinely absent on the build machine, so this is the
        # real-world path: the GUI must still start, just without the tabs.
        gui = FakeGui()
        result = integration.setup_tabs(gui, object(), queue_path=str(tmp_path / "q.json"))
        assert result is None

    def test_providers_are_lazy_and_do_not_raise_at_build_time(self):
        gui = FakeGui()
        # Building a provider must not import or contact anything.
        assert callable(integration.make_tidal_library_provider(gui))
        assert callable(integration.make_matcher_provider(gui))
        assert callable(integration.make_spotify_source_provider(gui))

    def test_tidal_provider_returns_none_without_orpheus(self):
        gui = FakeGui()
        gui.orpheus_instance = None
        assert integration.make_tidal_library_provider(gui)() is None

    def test_matcher_provider_returns_none_without_tidal(self):
        gui = FakeGui()
        gui.orpheus_instance = None
        assert integration.make_matcher_provider(gui)() is None


# ---------------------------------------------------------------------------
# Account status providers
#
# These decide what the Accounts tab says, and the distinctions matter: "no
# TIDAL module" must never be presented as a failed login, and "no Spotify
# client id" must never be presented as "not signed in" -- the sign-in button
# cannot fix either.
# ---------------------------------------------------------------------------

from hires.models import AccountState, AuthRequiredError  # noqa: E402


class FakeTidalModule:
    def __init__(self, session=None, raises=None):
        self.session = session
        self.calls = []
        self._raises = raises

    def _ensure_credentials(self, force=False):
        self.calls.append(force)
        if self._raises:
            raise self._raises


class FakeSession:
    """The `.session` attribute TidalLibrary duck types against."""

    def __init__(self, user_id=None):
        self._user_id = user_id

    def authenticated_session(self):
        if self._user_id is None:
            return None
        return type("S", (), {"user_id": self._user_id})()


class FakeOrpheus:
    def __init__(self, module=None, module_list=("tidal",), load_raises=None):
        self.module_list = list(module_list)
        self.loaded_modules = {"tidal": module} if module is not None else {}
        self._load_raises = load_raises
        self.load_calls = []

    def load_module(self, name):
        self.load_calls.append(name)
        if self._load_raises:
            raise self._load_raises
        return self.loaded_modules.get(name)


class TestTidalStatusProvider:
    def test_no_orpheus_instance_is_unavailable_not_signed_out(self):
        gui = FakeGui()
        gui.orpheus_instance = None
        status = integration.make_tidal_status_provider(gui)()

        assert status.state is AccountState.UNAVAILABLE
        assert status.service == "TIDAL"

    def test_module_not_in_the_list_is_unavailable(self):
        gui = FakeGui()
        gui.orpheus_instance = FakeOrpheus(module=None, module_list=())
        assert integration.make_tidal_status_provider(gui)().state is AccountState.UNAVAILABLE

    def test_loaded_but_logged_out_is_signed_out(self):
        """The module is there, nobody is logged in -- that is the button's job."""
        gui = FakeGui()
        gui.orpheus_instance = FakeOrpheus(module=FakeTidalModule(FakeSession(user_id=None)))
        status = integration.make_tidal_status_provider(gui)()

        assert status.state is AccountState.SIGNED_OUT
        assert "guest" in status.detail.lower()

    def test_logged_in_reports_the_user(self):
        gui = FakeGui()
        gui.orpheus_instance = FakeOrpheus(module=FakeTidalModule(FakeSession(user_id=4242)))
        status = integration.make_tidal_status_provider(gui)()

        assert status.state is AccountState.SIGNED_IN
        assert "4242" in status.account

    def test_the_module_is_loaded_on_demand(self):
        """The tabs are built before modules load; a snapshot would be wrong."""
        module = FakeTidalModule(FakeSession(user_id=1))
        orpheus = FakeOrpheus(module=None)
        orpheus.loaded_modules = {}
        orpheus.load_module = lambda name: module

        gui = FakeGui()
        gui.orpheus_instance = orpheus
        assert integration.make_tidal_status_provider(gui)().state is AccountState.SIGNED_IN

    def test_a_failing_module_load_is_unavailable_not_a_crash(self):
        gui = FakeGui()
        gui.orpheus_instance = FakeOrpheus(module=None, load_raises=RuntimeError("boom"))
        assert integration.make_tidal_status_provider(gui)().state is AccountState.UNAVAILABLE


class TestTidalSignInCallable:
    def test_it_forces_the_credential_prompt(self):
        """Without force=True a GUI session stays in guest mode by design."""
        module = FakeTidalModule(FakeSession(user_id=None))
        gui = FakeGui()
        gui.orpheus_instance = FakeOrpheus(module=module)

        integration.make_tidal_sign_in(gui)()
        assert module.calls == [True]

    def test_it_resolves_the_module_at_call_time(self):
        """Built before TIDAL loads, it must still work once TIDAL is there."""
        gui = FakeGui()
        gui.orpheus_instance = None
        sign_in = integration.make_tidal_sign_in(gui)

        module = FakeTidalModule(FakeSession(user_id=None))
        gui.orpheus_instance = FakeOrpheus(module=module)

        sign_in()
        assert module.calls == [True]

    def test_without_the_module_it_raises_something_readable(self):
        gui = FakeGui()
        gui.orpheus_instance = None
        with pytest.raises(Exception) as excinfo:
            integration.make_tidal_sign_in(gui)()
        assert "TIDAL" in str(excinfo.value)

    def test_an_old_module_without_the_entry_point_is_reported(self):
        class Old:
            session = None

        gui = FakeGui()
        gui.orpheus_instance = FakeOrpheus(module=Old())
        with pytest.raises(Exception) as excinfo:
            integration.make_tidal_sign_in(gui)()
        assert "cannot be signed in" in str(excinfo.value)


class TestSpotifyStatusProvider:
    def test_without_credentials_it_is_setup_not_signed_out(self):
        """The sign-in button cannot succeed without a client id -- say why."""
        gui = FakeGui()
        gui.current_settings["credentials"]["Spotify"] = {}
        status = integration.make_spotify_status_provider(gui)()

        assert status.state is AccountState.NEEDS_SETUP
        assert "Settings" in status.hint

    def test_with_credentials_but_no_token_it_is_signed_out(self):
        gui = FakeGui()
        status = integration.make_spotify_status_provider(gui)()

        assert status.state is AccountState.SIGNED_OUT
        assert status.service == "Spotify"

    def test_status_is_reread_so_entering_credentials_takes_effect(self):
        gui = FakeGui()
        gui.current_settings["credentials"]["Spotify"] = {}
        provider = integration.make_spotify_status_provider(gui)
        assert provider().state is AccountState.NEEDS_SETUP

        gui.current_settings["credentials"]["Spotify"] = {
            "client_id": "cid",
            "client_secret": "secret",
        }
        assert provider().state is AccountState.SIGNED_OUT


# ---------------------------------------------------------------------------
# Sharing the download slot with the stock batch engine
#
# gui.py's final_ui_update clears download_process_active and pops the next
# batch URL *before* scheduling it through app.after(pause_ms, ...). During
# that pause both of the flags the runtime used to watch say "idle" -- so it
# claimed the slot, and the delayed batch start was refused and its URL lost.
# ---------------------------------------------------------------------------

class TestBatchSlotIsNotStolen:
    def test_stays_off_the_slot_during_the_inter_batch_pause(self, queue):
        gui = FakeGui(output_path="/out")
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="https://tidal.com/browse/track/1", title="Ours"))

        # Mid-batch pause: flag cleared, last URL already popped, restart pending.
        gui.download_process_active = False
        gui.file_download_queue = []
        gui.current_batch_output_path = "/out"

        runtime._pump()

        assert gui.calls == [], "claimed the slot the batch engine still owned"
        assert queue.list()[0].status == QueueStatus.PENDING.value
        assert queue.list()[0].attempts == 0, "burned an attempt while waiting"

    def test_takes_the_slot_once_the_batch_is_really_done(self, queue):
        gui = FakeGui(output_path="/out")
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="https://tidal.com/browse/track/1", title="Ours"))

        gui.current_batch_output_path = None  # batch finished
        runtime._pump()

        assert len(gui.calls) == 1


# ---------------------------------------------------------------------------
# Starting up with a restored queue
# ---------------------------------------------------------------------------

class TestWaitsForTheLibrary:
    def test_does_not_dispatch_before_orpheus_exists(self, queue):
        """Otherwise every item pops a modal error box and burns its retries."""
        gui = FakeGui(output_path="/out")
        gui.orpheus_instance = None
        runtime = make_runtime(gui, queue)
        queue.add_many(
            [QueueItem(url=f"https://tidal.com/browse/track/{i}") for i in range(3)]
        )

        for _ in range(5):
            runtime._pump()

        assert gui.calls == []
        assert [i.status for i in queue.list()] == [QueueStatus.PENDING.value] * 3
        assert [i.attempts for i in queue.list()] == [0, 0, 0]

    def test_dispatches_as_soon_as_the_library_is_up(self, queue):
        gui = FakeGui(output_path="/out")
        gui.orpheus_instance = None
        runtime = make_runtime(gui, queue)
        queue.add(QueueItem(url="https://tidal.com/browse/track/1"))

        runtime._pump()
        assert gui.calls == []

        gui.orpheus_instance = object()
        runtime._pump()

        assert len(gui.calls) == 1
