"""Contract between gui.py and hires/integration.py.

The queue runtime reads two gui.py globals by name. The tests for the runtime
use a FakeGui that sets them itself, so a rename in gui.py would pass every
other test and silently bring the old behaviour back (TS-ORPHEUS review,
finding 1). These read gui.py's source instead of importing it, like
tests/test_default_settings.py.
"""

import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI_PY = os.path.join(REPO, "gui.py")


def _tree(path=None):
    with open(path or GUI_PY, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _function(name, path=None):
    for node in _tree(path).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"gui.py has no top-level function {name}()")


def _declares_global(func, name):
    return any(isinstance(n, ast.Global) and name in n.names for n in ast.walk(func))


def _assigns(func, name):
    for node in ast.walk(func):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
    return False


def start_records_active_thread(path=None):
    func = _function("_start_single_download", path)
    return _declares_global(func, "_active_download_thread") and _assigns(func, "_active_download_thread")


def _module_assigns(name, path=None):
    return any(
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        for node in _tree(path).body
    )


def stop_counts_requests(path=None):
    # Without the module-level `= 0` the `+=` in stop_download raises NameError.
    func = _function("stop_download", path)
    return (
        _module_assigns("_download_stop_requests", path)
        and _declares_global(func, "_download_stop_requests")
        and _assigns(func, "_download_stop_requests")
    )


def test_start_single_download_sets_the_global_active_download_thread():
    # hires/integration.py: _gui_started_another_download and stop_active
    assert start_records_active_thread()


def test_stop_download_counts_stop_requests():
    # hires/integration.py: _was_cancelled, after gui.py's next start has
    # already cleared stop_event
    assert stop_counts_requests()


def test_the_contract_check_notices_a_rename(tmp_path):
    with open(GUI_PY, encoding="utf-8") as handle:
        source = handle.read()
    renamed = tmp_path / "gui.py"
    renamed.write_text(source.replace("_active_download_thread = download_thread", "_current_thread = download_thread"), encoding="utf-8")
    assert not start_records_active_thread(str(renamed))


def test_the_contract_check_notices_a_missing_counter(tmp_path):
    with open(GUI_PY, encoding="utf-8") as handle:
        source = handle.read()
    no_global = tmp_path / "gui.py"
    no_global.write_text(source.replace("\n_download_stop_requests = 0\n", "\n"), encoding="utf-8")
    assert not stop_counts_requests(str(no_global))
