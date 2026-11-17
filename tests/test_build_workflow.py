"""Guards on what the release build puts into the app.

The SoundCloud module comes from our fork at a pinned commit until upstream
has the fix (Go+ previews saved as tracks, stale client id; see HIRES.md).
Switching back to an unpinned upstream clone must be a deliberate change, not
an accident -- these tests make it one.
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, ".github", "workflows", "build-release.yml")


def _workflow():
    with open(WORKFLOW, encoding="utf-8") as handle:
        return handle.read()


def test_soundcloud_module_comes_from_the_pinned_fork():
    text = _workflow()
    assert "SOUNDCLOUD_REPO: https://github.com/CTreitges/orpheusdl-soundcloud" in text
    assert re.search(r"SOUNDCLOUD_REF: [0-9a-f]{40}\n", text), "fork must be pinned to a full commit"
    assert 'git -C modules/soundcloud checkout --quiet "$SOUNDCLOUD_REF"' in text


def test_build_fails_if_the_soundcloud_fix_is_missing():
    assert "grep -q \"if i.get('snipped'):\" modules/soundcloud/interface.py" in _workflow()


def test_upstream_soundcloud_is_not_cloned_as_well():
    assert "bascurtiz/orpheusdl-soundcloud" not in _workflow()
