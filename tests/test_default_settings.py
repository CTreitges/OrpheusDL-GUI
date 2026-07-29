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
