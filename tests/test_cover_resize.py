"""Cover resizing in gui.py's cancellable download_file survives transparent covers.

Some SoundCloud covers are PNGs with an alpha channel or a palette. Saving them
as JPEG raised ``OSError: cannot write mode RGBA as JPEG`` and the whole track
counted as failed (audio on disk, but no tags and no target format).

gui.py cannot be imported in a test (it builds a window), so the real
``patch_download_file_for_cancellation`` is executed from the syntax tree. It
patches a stand-in ``utils.utils`` whose session serves a local file instead of
the network; every saved cover is opened again with Pillow.
"""

from __future__ import annotations

import ast
import os
import sys
import types

import pytest

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("tqdm")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI_PY = os.path.join(REPO, "gui.py")

SIZE = 64


class DownloadCancelledError(Exception):
    pass


class _LocalResponse:
    def __init__(self, path):
        self.headers = {}
        self._path = path

    def iter_content(self, chunk_size=1024):
        with open(self._path, "rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk


class _LocalSession:
    """Stands in for utils.r_session: the "url" is a local file path."""

    def get(self, url, stream=True, headers=None, verify=True):
        return _LocalResponse(url)


@pytest.fixture
def download_file(monkeypatch):
    with open(GUI_PY, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    patch = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "patch_download_file_for_cancellation")

    utils_module = types.ModuleType("utils.utils")
    utils_module.download_file = lambda *a, **k: None
    utils_module.r_session = _LocalSession()
    utils_module.silentremove = os.remove
    package = types.ModuleType("utils")
    package.utils = utils_module
    monkeypatch.setitem(sys.modules, "utils", package)
    monkeypatch.setitem(sys.modules, "utils.utils", utils_module)
    # Keep the real Downloader untouched: its patch fails to import and is skipped.
    monkeypatch.setitem(sys.modules, "orpheus.music_downloader", None)

    namespace = {"_download_cancelled": False, "DownloadCancelledError": DownloadCancelledError}
    exec(compile(ast.Module(body=[patch], type_ignores=[]), GUI_PY, "exec"), namespace)
    namespace["patch_download_file_for_cancellation"]()
    assert utils_module.download_file.__name__ == "cancellable_download_file"
    return utils_module.download_file


def _half_transparent_rgba():
    """Left half fully transparent red, right half opaque blue."""
    im = Image.new("RGBA", (200, 200), (0, 0, 255, 255))
    im.paste((255, 0, 0, 0), (0, 0, 100, 200))
    return im


def _run(download_file, tmp_path, source_image, target_format, name="cover"):
    source = tmp_path / f"{name}-source.png"
    source_image.save(source)
    target = tmp_path / f"{name}.{target_format}"
    settings = {"should_resize": True, "resolution": SIZE, "format": target_format, "compression": "low"}
    assert download_file(str(source), str(target), artwork_settings=settings) == str(target)
    return Image.open(target)


def _is_white(pixel):
    return all(channel >= 240 for channel in pixel[:3])


def test_rgba_png_cover_is_saved_as_rgb_jpeg_on_white(download_file, tmp_path):
    with _run(download_file, tmp_path, _half_transparent_rgba(), "jpg") as im:
        assert (im.format, im.mode, im.size) == ("JPEG", "RGB", (SIZE, SIZE))
        assert _is_white(im.getpixel((5, SIZE // 2)))          # transparent area: white, not black
        r, g, b = im.getpixel((SIZE - 5, SIZE // 2))
        assert b > 200 and r < 60 and g < 60                     # opaque area keeps its colour


def test_palette_png_with_transparency_is_saved_as_rgb_jpeg(download_file, tmp_path):
    im = Image.new("P", (200, 200), 0)
    im.putpalette([0, 0, 0, 0, 128, 0] + [0] * (254 * 3))
    im.paste(1, (100, 0, 200, 200))
    im.info["transparency"] = 0                                  # index 0 (black) is transparent
    with _run(download_file, tmp_path, im, "jpeg") as out:
        assert (out.format, out.mode, out.size) == ("JPEG", "RGB", (SIZE, SIZE))
        assert _is_white(out.getpixel((5, SIZE // 2)))
        r, g, b = out.getpixel((SIZE - 5, SIZE // 2))
        assert g > 100 and r < 60 and b < 60


def test_grayscale_with_alpha_is_saved_as_rgb_jpeg(download_file, tmp_path):
    im = Image.new("LA", (200, 200), (40, 255))
    im.paste((200, 0), (0, 0, 100, 200))
    with _run(download_file, tmp_path, im, "jpg") as out:
        assert (out.format, out.mode, out.size) == ("JPEG", "RGB", (SIZE, SIZE))
        assert _is_white(out.getpixel((5, SIZE // 2)))
        assert max(out.getpixel((SIZE - 5, SIZE // 2))) < 80


def test_rgb_jpeg_cover_is_processed_as_before(download_file, tmp_path):
    source_image = Image.new("RGB", (200, 200), (10, 120, 200))
    source_image.paste((250, 200, 0), (0, 0, 100, 200))
    with _run(download_file, tmp_path, source_image, "jpg") as out:
        assert (out.format, out.mode, out.size) == ("JPEG", "RGB", (SIZE, SIZE))
        result = out.tobytes()
    # Same pixels as the plain resize + save the code always did for RGB covers.
    reference = tmp_path / "reference.jpg"
    source_image.resize((SIZE, SIZE), Image.Resampling.BICUBIC).save(reference, "jpeg", quality=90)
    with Image.open(reference) as ref:
        assert result == ref.tobytes()


def test_rgba_png_cover_stays_rgba_when_target_is_png(download_file, tmp_path):
    with _run(download_file, tmp_path, _half_transparent_rgba(), "png") as out:
        assert (out.format, out.mode, out.size) == ("PNG", "RGBA", (SIZE, SIZE))
        assert out.getpixel((5, SIZE // 2))[3] == 0              # transparency kept
        assert out.getpixel((SIZE - 5, SIZE // 2)) == (0, 0, 255, 255)
