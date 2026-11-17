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


SCRIPT = os.path.join(REPO, "installer", "windows", "Update OrpheusDL.cmd")


def test_release_builds_bake_the_tag_as_version():
    """D1: the app learns its version from the tag; a bad one fails the build."""
    text = _workflow()
    assert 'if [[ "$GITHUB_REF" == refs/tags/* ]]; then version="$GITHUB_REF_NAME"; fi' in text
    assert "> app/hires/_build_version.py" in text
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+-hires\.[0-9]+$" in text


def test_build_version_is_bundled():
    with open(os.path.join(REPO, "gui.spec"), encoding="utf-8") as handle:
        assert "'hires._build_version'" in handle.read()


def test_windows_zip_contains_the_update_script():
    assert 'cp "installer/windows/Update OrpheusDL.cmd" dist/' in _workflow()


def test_second_test_version_is_never_built_for_a_tag():
    """Review PR #3: on a tag the release job attaches artifacts/**/* -- a
    second ZIP with the same name and another baked version must not exist."""
    text = _workflow()
    guard = "inputs.second_version != '' && !startsWith(github.ref, 'refs/tags/')"
    for step in ("- name: Build second version", "- name: Upload second version"):
        block = text[text.index(step):]
        assert guard in block.split("\n      - name:")[0], step


def test_update_script_failure_text_claims_nothing():
    """Review PR #3: exit 1 can mean 'EXE updated, modules not' -- the console
    must not say that nothing was replaced; the message window tells."""
    with open(SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    assert "nothing was replaced" not in text.lower()
    assert "The message window said what happened" in text


def test_update_script_only_calls_the_new_exe():
    """One code path: the script hands everything to the EXE (--install-into)."""
    with open(SCRIPT, "rb") as handle:
        raw = handle.read()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "cmd needs CRLF"
    text = raw.decode("ascii")
    assert 'start "" /wait "%~dp0OrpheusDL_GUI.exe" --install-into "%TARGET%" %*' in text
    assert 'set "TARGET=%~dp0.."' in text
    for word in ("copy ", "move ", "del ", "rmdir", "xcopy", "robocopy"):
        assert word not in text.lower(), f"the script must not touch files itself ({word.strip()})"
