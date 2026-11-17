"""A download started by hand while "Convert folder…" runs gets a hint, not a start.

The queue already waits for the folder job (hires/integration.py); a download
from the Download or Search tab did not and could write into the folder being
converted. gui.py cannot be imported in a test, so its helper is executed from
the syntax tree and its two callers are checked to ask it before anything else.
"""

from __future__ import annotations

import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from hires import convert_job  # noqa: E402

GUI_PY = os.path.join(REPO, "gui.py")


@pytest.fixture(scope="module")
def tree():
    with open(GUI_PY, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _function(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


@pytest.fixture
def folder_job():
    convert_job._ACTIVE.set()
    yield
    convert_job._ACTIVE.clear()


def test_folder_job_manual_hint_only_while_the_job_runs(folder_job):
    assert "Convert folder" in convert_job.manual_download_hint()
    convert_job._ACTIVE.clear()
    assert convert_job.manual_download_hint() == ""


def test_folder_job_manual_gui_helper_shows_the_hint(tree, folder_job):
    shown = []
    namespace = {"show_centered_messagebox": lambda title, text, dialog_type="info": shown.append(text)}
    exec(compile(ast.Module(body=[_function(tree, "_hires_folder_job_blocks_download")], type_ignores=[]),
                 GUI_PY, "exec"), namespace)
    assert namespace["_hires_folder_job_blocks_download"]() is True
    assert shown == [convert_job.MANUAL_DOWNLOAD_HINT]
    convert_job._ACTIVE.clear()
    assert namespace["_hires_folder_job_blocks_download"]() is False
    assert len(shown) == 1


@pytest.mark.parametrize("name", ["start_download_thread", "download_selected"])
def test_folder_job_manual_entry_points_ask_first(tree, name):
    """The check is the first statement after the globals -- before anything starts."""
    body = [n for n in _function(tree, name).body if not isinstance(n, (ast.Global, ast.Expr))]
    first = body[0]
    assert isinstance(first, ast.If), f"{name} does not start with the folder-job check"
    call = first.test
    assert isinstance(call, ast.Call) and call.func.id == "_hires_folder_job_blocks_download"
    assert isinstance(first.body[0], ast.Return)
