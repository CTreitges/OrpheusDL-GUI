"""Target format: the setting, gui.py's hook and a queue item's real finish.

The end-to-end class drives the real :class:`HiresRuntime` through a queue
item: a download thread writes a real FLAC (made by ffmpeg) and then calls
:func:`hires.download_finish.after_download` exactly where gui.py's
``run_download_in_thread`` calls it. The runtime then marks the item done the
way it does in the app. The gui.py side of that call is pinned by the AST
tests below, so neither half can drift away from the other.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

GUI_PY = os.path.join(REPO, "gui.py")

from hires import audio_convert as ac  # noqa: E402
from hires import download_finish  # noqa: E402
from hires.integration import HiresRuntime  # noqa: E402
from hires.models import QueueItem, QueueStatus  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)


def _gui_source():
    with open(GUI_PY, encoding="utf-8") as handle:
        return handle.read()


def _function(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


# -- the setting ----------------------------------------------------------------

class TestSetting:
    def test_target_format_defaults_to_off_and_replace(self):
        from tests.test_default_settings import _default_setting

        assert _default_setting("globals", "codecs", "target_format") == "off"
        assert _default_setting("globals", "codecs", "target_keep_original") is False

    def test_target_format_old_settings_without_the_keys_stay_off(self):
        """W8: a settings.json from before this version changes nothing."""
        old = {"globals": {"codecs": {"proprietary_codecs": False}}}
        assert download_finish.target_settings(old) == ("off", False)
        assert download_finish.target_settings({}) == ("off", False)
        assert download_finish.target_settings(None) == ("off", False)

    def test_target_format_unknown_value_falls_back_to_off(self):
        settings = {"globals": {"codecs": {"target_format": "mp3", "target_keep_original": 1}}}
        assert download_finish.target_settings(settings) == ("off", True)

    def test_target_format_settings_row_is_a_dropdown_with_the_folder_button(self):
        source = _gui_source()
        branch = re.search(
            r'elif section_key == "codecs" and field == "target_format":(.*?)elif section_key',
            source, re.S,
        )
        assert branch, "Settings row for codecs.target_format not found"
        body = branch.group(1)
        assert '["off", "wav", "flac", "aiff"]' in body
        assert "CTkComboBox" in body and 'state="readonly"' in body
        assert "command=_hires_open_convert_folder" in body
        assert "Convert folder" in body

    def test_target_format_keep_original_has_its_own_label(self):
        assert '"Keep Original" if full_key == "codecs.target_keep_original"' in _gui_source()


# -- gui.py's hook -------------------------------------------------------------

class TestGuiHook:
    def test_target_format_download_thread_calls_the_hook_before_finishing(self):
        tree = ast.parse(_gui_source())
        thread = _function(tree, "run_download_in_thread")
        calls = [n for n in ast.walk(thread) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "_hires_after_download"]
        assert len(calls) == 1, "run_download_in_thread must call _hires_after_download once"
        call = calls[0]
        assert [ast.unparse(a) for a in call.args] == ["output_path", "start_time.timestamp()"]
        # Only for downloads that neither failed nor were stopped.
        guard = next(n for n in ast.walk(thread) if isinstance(n, ast.If) and call in ast.walk(n))
        assert ast.unparse(guard.test) == "not is_cancelled and (not download_exception_occurred)"

    def test_target_format_hook_reaches_download_finish(self):
        tree = ast.parse(_gui_source())
        hook = ast.unparse(_function(tree, "_hires_after_download"))
        assert "from hires.download_finish import after_download" in hook
        assert "after_download(sys.modules[__name__], output_path, started_at)" in hook

    def test_target_format_hook_runs_before_the_gui_goes_idle(self):
        """Conversion must finish before final_ui_update clears the busy flag."""
        source = _gui_source()
        start = source.index("def run_download_in_thread(")
        hook_at = source.index("_hires_after_download(output_path", start)
        idle_at = source.index("def final_ui_update(", start)
        assert hook_at < idle_at


# -- a queue item, end to end ----------------------------------------------------

class _App:
    def after(self, _delay, _fn=None):
        return "after#1"

    def after_cancel(self, _token):
        pass


class _Gui:
    """gui.py as the runtime and download_finish see it."""

    def __init__(self, output_path, target="off", keep=False):
        self.app = _App()
        self.orpheus_instance = object()
        self.download_process_active = False
        self.file_download_queue = []
        self.current_batch_output_path = None
        self.stop_event = threading.Event()
        self._active_download_thread = None
        self.current_settings = {"globals": {
            "general": {"output_path": output_path},
            "codecs": {"target_format": target, "target_keep_original": keep},
        }}
        self.hires_runtime = None
        self.names = ["Artist - Tone.flac"]

    def _get_ffmpeg_path(self):
        return FFMPEG

    def _start_single_download(self, url, path, search_result_data=None):
        started = time.time()
        self.download_process_active = True

        def download():
            for name in self.names:
                subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                                "-i", "sine=frequency=440:sample_rate=96000:duration=1",
                                "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                                os.path.join(path, name)], check=True)
            # The line gui.py's run_download_in_thread runs at this point.
            download_finish.after_download(self, path, started)
            self.download_process_active = False

        thread = threading.Thread(target=download)
        self._active_download_thread = thread
        thread.start()
        return None


def _run_one_item(tmp_path, target, keep=False):
    out = tmp_path / "Downloads"
    out.mkdir()
    gui = _Gui(str(out), target=target, keep=keep)
    store = QueueStore(str(tmp_path / "queue.json"))
    runtime = HiresRuntime(gui, store, logger=lambda _m: None)
    gui.hires_runtime = {"runtime": runtime, "queue": store, "tabs": None}
    item = store.add(QueueItem(url="https://tidal.com/browse/track/1", title="Tone", output_path=str(out)))
    runtime._pump()  # claims and dispatches
    gui._active_download_thread.join(timeout=60)
    runtime._active_since = 0.0  # past the dispatch grace period
    runtime._pump()  # sees the GUI idle and finishes the item
    return gui, store.get(item.id), out


@needs_ffmpeg
class TestQueueItemFinish:
    def test_target_format_queue_item_lands_as_aiff(self, tmp_path):
        _gui, item, out = _run_one_item(tmp_path, "aiff")
        assert item.status == QueueStatus.DONE.value
        assert sorted(os.listdir(out)) == ["Artist - Tone.aiff"]
        info = ac.probe(str(out / "Artist - Tone.aiff"))
        assert (info.codec, info.sample_rate, info.bits) == ("pcm", 96000, 24)
        assert item.actual_quality == "FLAC 24-bit/96 kHz → AIFF"

    def test_target_format_keep_original_keeps_both(self, tmp_path):
        _gui, item, out = _run_one_item(tmp_path, "wav", keep=True)
        assert item.status == QueueStatus.DONE.value
        assert sorted(os.listdir(out)) == ["Artist - Tone.flac", "Artist - Tone.wav"]

    def test_target_format_off_leaves_the_download_alone(self, tmp_path):
        _gui, item, out = _run_one_item(tmp_path, "off")
        assert sorted(os.listdir(out)) == ["Artist - Tone.flac"]
        assert item.actual_quality == "FLAC 24-bit/96 kHz"

    def test_target_format_library_files_from_before_are_not_touched(self, tmp_path):
        out = tmp_path / "Downloads"
        out.mkdir()
        older = out / "old.flac"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=duration=0.3", str(older)], check=True)
        if not hasattr(os.stat(older), "st_birthtime") and sys.platform != "win32":
            os.utime(older, (time.time() - 3600, time.time() - 3600))
        time.sleep(download_finish.START_SLACK_SEC + 0.1)
        quality = download_finish.process_download(str(out), time.time(), "wav", ffmpeg=FFMPEG)
        assert quality == ""
        assert sorted(os.listdir(out)) == ["old.flac"]

    def test_target_format_rewrites_the_playlist_of_the_download(self, tmp_path):
        out = tmp_path / "Downloads"
        (out / "Set").mkdir(parents=True)
        started = time.time()
        track = out / "Set" / "Tone.flac"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=duration=0.3", str(track)], check=True)
        playlist = out / "Set" / "Set.m3u8"
        playlist.write_text("#EXTM3U\n" + str(track) + "\n", encoding="utf-8")
        logs = []
        download_finish.process_download(str(out), started, "aiff", ffmpeg=FFMPEG, log=logs.append)
        assert playlist.read_text(encoding="utf-8") == "#EXTM3U\n" + str(out / "Set" / "Tone.aiff") + "\n"
        assert any("1 converted to AIFF" in line for line in logs)

    def test_target_format_lossy_download_logs_the_warning(self, tmp_path):
        out = tmp_path / "Downloads"
        out.mkdir()
        started = time.time()
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=duration=0.5", "-c:a", "aac", "-b:a", "256k", str(out / "sc.m4a")],
                       check=True)
        logs = []
        quality = download_finish.process_download(str(out), started, "wav", ffmpeg=FFMPEG, log=logs.append)
        assert quality.startswith("AAC ") and quality.endswith("→ WAV")
        assert any(ac.LOSSY_WARNING in line for line in logs)

    def test_target_format_after_download_never_raises(self, tmp_path):
        gui = _Gui(str(tmp_path))
        gui.current_settings = "broken"
        download_finish.after_download(gui, None, time.time())  # must not raise


# -- Convert folder… dialog ------------------------------------------------------

def _tk_root():
    """A real window; the Windows venv's Tcl sometimes needs a second try."""
    customtkinter = pytest.importorskip("customtkinter")
    for attempt in range(3):
        try:
            return customtkinter.CTk()
        except Exception as exc:  # no display, or Tcl not ready yet
            if attempt == 2:
                pytest.skip(f"no display: {exc}")
            time.sleep(0.2)


@needs_ffmpeg
class TestConvertFolderDialog:
    def test_target_format_folder_dialog_converts_and_reports(self, tmp_path):
        for name in ("a.flac", "sub/b.flac"):
            os.makedirs(os.path.dirname(str(tmp_path / name)), exist_ok=True)
            subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "sine=duration=0.3", str(tmp_path / name)], check=True)
        root = _tk_root()
        try:
            from hires.convert_panel import ConvertFolderDialog

            dialog = ConvertFolderDialog(root, ffmpeg=FFMPEG, target="wav", folder=str(tmp_path))
            asked = []
            assert dialog.start(confirm=lambda *a: asked.append(a[1]) or True)
            assert asked == ["Replace originals?"]
            assert dialog.cancel_button.cget("text") == "Cancel"
            deadline = time.time() + 60
            while dialog.job.running and time.time() < deadline:
                root.update()
                time.sleep(0.02)
            for _ in range(20):
                root.update()
                time.sleep(0.02)
            assert sorted(os.path.relpath(p, tmp_path) for p in ac.find_audio(str(tmp_path))) == [
                "a.wav", os.path.join("sub", "b.wav")]
            assert dialog.status.cget("text").startswith("2 of 2 files converted to WAV.")
            assert dialog.cancel_button.cget("text") == "Close"
        finally:
            root.destroy()

    def test_target_format_folder_dialog_declined_confirmation_changes_nothing(self, tmp_path):
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=duration=0.3", str(tmp_path / "a.flac")], check=True)
        root = _tk_root()
        try:
            from hires.convert_panel import ConvertFolderDialog

            dialog = ConvertFolderDialog(root, ffmpeg=FFMPEG, target="aiff", folder=str(tmp_path))
            assert dialog.start(confirm=lambda *a: False) is False
            assert not dialog.job.running
            assert os.listdir(tmp_path) == ["a.flac"]
        finally:
            root.destroy()
