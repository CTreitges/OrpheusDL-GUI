"""Every setting and every hi-res control has a help text with a recommendation.

The setting keys are read out of gui.py with ``ast`` -- the defaults table
the settings tabs are built from -- so a setting added to gui.py without a
help text fails here instead of shipping unexplained. The second half builds
the real tabs through ``setup_tabs`` and checks the texts are actually
attached to the controls, not just written down.
"""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hires.help_texts import HELP, help_message, short_hint  # noqa: E402

GUI = os.path.join(ROOT, "gui.py")

#: The services this run covers (goal: Spotify, TIDAL, YouTube, SoundCloud).
SERVICES = ("SoundCloud", "Spotify", "TIDAL", "YouTube")

#: Settings whose recommendation must be visible next to the field (A9).
VISIBLE = (
    "general.concurrent_downloads",
    "general.throttle_batch_size",
    "general.throttle_pause_seconds",
    "general.quality",
    "credentials.Spotify.download_pause_seconds",
    "credentials.YouTube.download_pause_seconds",
    "credentials.TIDAL.throttle",
)


@pytest.fixture(scope="module")
def tree():
    with open(GUI, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _dict_assigned_to(tree, name, required_key):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            if name in targets and required_key in keys:
                return node.value
    raise AssertionError(f"{name} with key {required_key!r} not found in gui.py")


def _sub(node, key):
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    raise KeyError(key)


def gui_setting_keys(tree):
    """The keys the Global tab and the four service tabs render."""
    defaults = _dict_assigned_to(tree, "DEFAULT_SETTINGS", "credentials")
    keys = set()
    for section_key, section in zip(_sub(defaults, "globals").keys, _sub(defaults, "globals").values):
        for field in section.keys:
            keys.add(f"{section_key.value}.{field.value}")
    # conversion_flags is not one widget but four sliders, each with its own key.
    keys.discard("advanced.conversion_flags")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value.startswith("advanced.conversion_flags.")
            and any(isinstance(t, ast.Name) and t.id.endswith(("_full_key", "_setting_key")) for t in node.targets)
        ):
            keys.add(node.value.value)
    credentials = _sub(defaults, "credentials")
    for service in SERVICES:
        for field in _sub(credentials, service).keys:
            keys.add(f"credentials.{service}.{field.value}")
    return keys


def gui_tooltip_texts(tree):
    table = _dict_assigned_to(tree, "tooltip_texts", "general.output_path")
    return {
        k.value: v.value
        for k, v in zip(table.keys, table.values)
        if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
    }


class TestHelpCoverageOfSettings:
    def test_help_coverage_every_setting_has_an_entry(self, tree):
        keys = gui_setting_keys(tree)
        assert len(keys) > 70, "the key extraction found suspiciously few settings"
        missing = sorted(k for k in keys if k not in HELP)
        assert missing == [], f"settings without help text: {missing}"

    def test_help_coverage_every_entry_explains_and_recommends(self, tree):
        base = gui_tooltip_texts(tree)
        without_text = sorted(
            k for k in gui_setting_keys(tree) if not (HELP[k].text or base.get(k))
        )
        without_recommendation = sorted(k for k, e in HELP.items() if not e.recommendation.strip())
        assert without_text == [], f"no explanation anywhere: {without_text}"
        assert without_recommendation == [], f"no recommendation: {without_recommendation}"

    def test_help_coverage_no_stale_entries(self, tree):
        """A help text for a setting that no longer exists is dead weight."""
        keys = gui_setting_keys(tree)
        stale = sorted(k for k in HELP if not k.startswith("hires.") and k not in keys)
        assert stale == [], f"help for settings gui.py does not have: {stale}"

    def test_help_coverage_visible_recommendations(self):
        for key in VISIBLE:
            assert short_hint(key), f"{key} has no visible recommendation"

    def test_help_coverage_message_combines_base_text_and_recommendation(self):
        message = help_message("general.concurrent_downloads", "Number of tracks at once.")
        assert message.startswith("Number of tracks at once.")
        assert "Recommended:" in message

    def test_help_coverage_gui_attaches_the_markers(self, tree):
        """The settings code must actually call the "?" for each kind of row."""
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("_hires_help", "_hires_help_pack", "_hires_hint")
        ]
        literal = {
            arg.value
            for call in calls
            for arg in call.args[1:2]
            if isinstance(arg, ast.Constant)
        }
        names = {arg.id for call in calls for arg in call.args[1:2] if isinstance(arg, ast.Name)}
        fstrings = [arg for call in calls for arg in call.args[1:2] if isinstance(arg, ast.JoinedStr)]

        assert "full_key" in names, "the Global loop attaches no help"
        assert {"aac_full_key", "flac_full_key", "mp3_setting_key", "opus_full_key"} <= names
        assert fstrings, "the generic credential rows attach no help"
        for key in (
            "credentials.Spotify.username",
            "credentials.Spotify.download_pause_seconds",
            "credentials.Spotify.client_id",
            "credentials.Spotify.client_secret",
            "credentials.Spotify.cookies_path",
            "credentials.TIDAL.enable_mobile",
            "credentials.TIDAL.prefer_ac4",
            "credentials.TIDAL.fix_mqa",
            "credentials.TIDAL.throttle",
            "credentials.YouTube.download_pause_seconds",
            "credentials.YouTube.download_mode",
        ):
            assert key in literal, f"{key} is never attached in gui.py"


# ---------------------------------------------------------------------------
# Hi-res tabs: built for real, then searched for attached help
# ---------------------------------------------------------------------------

def _hires_keys():
    return {k for k in HELP if k.startswith("hires.")}


class TestHelpCoverageOfHiresTabs:
    def test_help_coverage_hires_controls(self, tmp_path):
        customtkinter = pytest.importorskip("customtkinter")
        tkinter = pytest.importorskip("tkinter")
        if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
            pytest.skip("no X display")

        from hires.help_widget import help_keys_in
        from hires.integration import setup_tabs
        from hires.models import QueueItem
        from hires.queue_store import QueueStore
        from test_integration import FakeGui

        # One queued playlist, so the group header (and its buttons) exists.
        path = str(tmp_path / "queue.json")
        QueueStore(path).add(QueueItem(url="u1", title="T", group_id="g", group_label="Set"))

        try:
            root = customtkinter.CTk()
        except tkinter.TclError as exc:  # pragma: no cover
            pytest.skip(f"cannot open a window: {exc}")
        try:
            gui = FakeGui(output_path=str(tmp_path))
            gui.app = root
            gui.application_path = str(tmp_path)
            view = customtkinter.CTkTabview(master=root)
            view.pack(fill="both", expand=True)
            result = setup_tabs(gui, view, queue_path=path)
            assert result is not None
            root.update()

            attached = help_keys_in(root)
            missing = sorted(_hires_keys() - attached)
            assert missing == [], f"hi-res controls without help: {missing}"
        finally:
            root.destroy()
