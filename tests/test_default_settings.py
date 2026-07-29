"""Guards on the settings gui.py writes for a fresh install.

gui.py is a 22k-line module that needs tkinter, so these read its source
instead of importing it. That is enough for the question being asked here:
what does a user get before they have configured anything?
"""

import ast
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

GUI_PY = os.path.join(REPO, "gui.py")

#: Every literal assigned to a formatting key, across all default blocks.
_ASSIGNMENT = re.compile(
    r'["\'](?P<key>[a-z_]+)["\']\s*:\s*(?P<value>"[^"]*"|\'[^\']*\')'
)


def formatting_defaults(key):
    """Every default gui.py declares for ``key``. Never empty -- see the test."""
    with open(GUI_PY, encoding="utf-8") as handle:
        source = handle.read()
    return [
        ast.literal_eval(match.group("value"))
        for match in _ASSIGNMENT.finditer(source)
        if match.group("key") == key
    ]


class TestTrackFilenameDefault:
    def test_the_key_is_actually_found(self):
        """If gui.py renames it, the assertions below must not silently pass."""
        assert formatting_defaults("track_filename_format"), (
            "no track_filename_format default found in gui.py -- "
            "this test has stopped checking anything"
        )

    def test_a_fresh_install_does_not_number_by_album_position(self):
        """Regression: playlists came out numbered "03. …", "01. …", "03. …".

        {track_number} looks like the position in the download but is the
        track's position on its own album, and OrpheusDL uses the same format
        string for album and playlist downloads. Across a playlist drawn from
        many albums that duplicates and misorders every prefix.
        """
        for default in formatting_defaults("track_filename_format"):
            assert "{track_number}" not in default, (
                f"track_filename_format default {default!r} numbers by album "
                "position, which duplicates inside a playlist"
            )

    def test_every_default_block_agrees(self):
        """gui.py declares these in more than one place; they must not drift."""
        defaults = set(formatting_defaults("track_filename_format"))
        assert len(defaults) == 1, f"conflicting defaults across gui.py: {defaults}"

    def test_artist_and_name_survive(self):
        """Dropping the number must not have dropped what identifies the track."""
        for default in formatting_defaults("track_filename_format"):
            assert "{artist}" in default and "{name}" in default


# ---------------------------------------------------------------------------
# Migration
#
# Changing the default only helps a fresh install. Anyone who already ran the
# app carries the old numbered format in their settings.json and would keep
# getting "03. …" forever, so it is rewritten once on load.
#
# The migration function is pulled out of gui.py's source rather than imported,
# because importing gui.py starts building a window.
# ---------------------------------------------------------------------------

def load_migration():
    """Exec just the migration helper out of gui.py, with no tkinter involved."""
    with open(GUI_PY, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    wanted = {
        "LEGACY_NUMBERED_TRACK_FORMATS",
        "UNNUMBERED_TRACK_FORMAT",
        "migrate_track_filename_format",
    }
    picked = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id in wanted for t in node.targets
            )
        )
    ]
    namespace = {"frozenset": frozenset}
    exec(compile(ast.Module(body=picked, type_ignores=[]), GUI_PY, "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def gui_migration():
    ns = load_migration()
    assert "migrate_track_filename_format" in ns, "migration helper vanished from gui.py"
    return ns


def settings_with(track_format):
    return {"global": {"formatting": {"track_filename_format": track_format}}}


class TestTrackFormatMigration:
    def test_the_old_default_is_rewritten(self, gui_migration):
        migrate = gui_migration["migrate_track_filename_format"]
        data = settings_with("{track_number}. {artist} - {name}")

        assert migrate(data) is True
        assert data["global"]["formatting"]["track_filename_format"] == "{artist} - {name}"

    @pytest.mark.parametrize(
        "legacy",
        ["{track_number}. {name}", "{track_number} - {artist} - {name}"],
    )
    def test_other_shipped_numbered_formats_are_rewritten(self, gui_migration, legacy):
        data = settings_with(legacy)
        assert gui_migration["migrate_track_filename_format"](data) is True

    def test_a_format_the_user_wrote_is_left_alone(self, gui_migration):
        """Only the exact old defaults are migrated.

        Somebody who deliberately built their own numbered scheme must keep it
        -- silently rewriting it would be the same class of surprise this fix
        is meant to remove.
        """
        mine = "{track_number}) {name} [{artist}]"
        data = settings_with(mine)

        assert gui_migration["migrate_track_filename_format"](data) is False
        assert data["global"]["formatting"]["track_filename_format"] == mine

    def test_an_already_fixed_config_is_not_rewritten(self, gui_migration):
        """No pointless write, and no log line, on every single startup."""
        data = settings_with("{artist} - {name}")
        assert gui_migration["migrate_track_filename_format"](data) is False

    @pytest.mark.parametrize(
        "junk",
        [None, {}, {"global": {}}, {"global": {"formatting": None}}, "not a dict", []],
    )
    def test_malformed_settings_do_not_raise(self, gui_migration, junk):
        """This runs during startup; it must never be the thing that breaks it."""
        assert gui_migration["migrate_track_filename_format"](junk) is False

    def test_every_shipped_default_is_covered_by_the_migration(self, gui_migration):
        """Guards the pair: if a numbered default is ever shipped again, the
        migration has to know about it, or users would be stuck with it."""
        legacy = gui_migration["LEGACY_NUMBERED_TRACK_FORMATS"]
        assert "{track_number}. {artist} - {name}" in legacy, (
            "the format that actually shipped is missing from the migration"
        )
        for default in formatting_defaults("track_filename_format"):
            assert default not in legacy, (
                f"{default!r} is both the current default and treated as legacy"
            )
