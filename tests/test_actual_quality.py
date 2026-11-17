"""The quality that really arrived, per finished queue entry.

From the download thread's report (:meth:`HiresRuntime.note_download_result`)
to the store, the queue file and the line in the real Queue tab.
"""

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.controllers import QueueController  # noqa: E402
from hires.quality import label_for  # noqa: E402
from hires.integration import HiresRuntime  # noqa: E402
from hires.models import QueueItem, QueueStatus  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402
from test_integration import FakeGui, write_audio  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return QueueStore(str(tmp_path / "queue.json"))


def _download_finishes(runtime, gui, folder, quality, *, reporter=None):
    """The download thread wrote a file and reported what it got."""
    write_audio(folder)
    runtime.note_download_result(reporter or gui._active_download_thread, quality)
    gui.download_process_active = False
    runtime._active_since = 0.0
    runtime._pump()


class TestStore:
    def test_actual_quality_is_saved_with_the_entry(self, store, tmp_path):
        item = store.add(QueueItem(url="u1", title="One"))
        store.claim_next()
        store.mark_done(item.id, actual_quality="FLAC 24-bit/96 kHz")
        reloaded = QueueStore(str(tmp_path / "queue.json")).get(item.id)
        assert reloaded.actual_quality == "FLAC 24-bit/96 kHz"

    def test_actual_quality_old_queue_file_loads_without_it(self, tmp_path):
        path = tmp_path / "queue.json"
        old = {"version": 1, "paused": False, "items": [
            {"url": "u1", "title": "Old", "status": "done", "id": "abc"}]}
        path.write_text(json.dumps(old), encoding="utf-8")
        item = QueueStore(str(path)).get("abc")
        assert item is not None and item.actual_quality == ""

    def test_actual_quality_is_cleared_when_the_entry_runs_again(self, store):
        item = store.add(QueueItem(url="u1", title="One"))
        store.claim_next()
        store.mark_done(item.id, actual_quality="AAC 256 kbit/s")
        store.mark_failed(item.id, "re-run")
        store.retry(item.id)
        assert store.claim_next().actual_quality == ""


class TestRuntime:
    def test_actual_quality_reaches_the_finished_item(self, store, tmp_path):
        gui = FakeGui(output_path=str(tmp_path / "out"))
        runtime = HiresRuntime(gui, store, logger=lambda _m: None)
        item = store.add(QueueItem(url="u1", title="One"))
        runtime._pump()
        _download_finishes(runtime, gui, str(tmp_path / "out"), "FLAC 16-bit/44.1 kHz")
        done = store.get(item.id)
        assert done.status == QueueStatus.DONE.value
        assert done.actual_quality == "FLAC 16-bit/44.1 kHz"

    def test_actual_quality_of_another_download_is_not_borrowed(self, store, tmp_path):
        """A download the user started by hand reports on its own thread."""
        gui = FakeGui(output_path=str(tmp_path / "out"))
        runtime = HiresRuntime(gui, store, logger=lambda _m: None)
        item = store.add(QueueItem(url="u1", title="One"))
        runtime._pump()
        _download_finishes(runtime, gui, str(tmp_path / "out"), "MP3 320 kbit/s",
                           reporter=threading.Thread())
        assert store.get(item.id).actual_quality == ""

    def test_actual_quality_reports_are_bounded(self, store):
        runtime = HiresRuntime(FakeGui(), store, logger=lambda _m: None)
        for n in range(50):
            runtime.note_download_result(object(), f"label {n}")
        assert len(runtime._reports) == 8


class TestRow:
    def test_actual_quality_replaces_the_requested_tier_once_done(self, store):
        item = store.add(QueueItem(url="u1", title="One", quality="hifi"))
        controller = QueueController(store)
        assert controller.rows()[0].quality == label_for("hifi")
        store.claim_next()
        store.mark_done(item.id, actual_quality="FLAC 24-bit/96 kHz")
        assert controller.rows()[0].quality == "FLAC 24-bit/96 kHz"

    def test_actual_quality_missing_keeps_the_requested_tier(self, store):
        item = store.add(QueueItem(url="u1", title="One", quality="hifi"))
        store.claim_next()
        store.mark_done(item.id)
        assert QueueController(store).rows()[0].quality == label_for("hifi")


class TestQueueTabShowsIt:
    def test_actual_quality_on_the_line_through_setup_tabs(self, tmp_path):
        customtkinter = pytest.importorskip("customtkinter")
        if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
            pytest.skip("no X display")
        from test_gui_panel import pump_until
        from hires.integration import setup_tabs

        root = None
        for attempt in range(3):
            try:
                root = customtkinter.CTk()
                break
            except Exception as exc:
                if attempt == 2:
                    pytest.skip(f"no display: {exc}")
        try:
            gui = FakeGui(output_path=str(tmp_path / "out"))
            gui.app = root
            gui.application_path = str(tmp_path)
            view = customtkinter.CTkTabview(master=root)
            view.pack(fill="both", expand=True)
            result = setup_tabs(gui, view, queue_path=str(tmp_path / "queue.json"))
            store, runtime, tab = result["queue"], result["runtime"], result["tabs"]["queue"]
            store.add(QueueItem(url="u1", title="One", quality="hifi"))
            runtime._pump()
            _download_finishes(runtime, gui, str(tmp_path / "out"), "FLAC 24-bit/96 kHz → AIFF")

            def shown():
                return [r.quality.cget("text") for r in tab._rows if r.quality.winfo_ismapped()]

            assert pump_until(root, lambda: shown() == ["FLAC 24-bit/96 kHz → AIFF"])
        finally:
            root.destroy()
