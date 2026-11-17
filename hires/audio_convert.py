"""Convert downloaded audio to WAV, FLAC or AIFF without losing anything.

Three promises, each enforced here rather than hoped for:

* **Never upsample.** No ``-ar`` is ever passed, so ffmpeg keeps the source's
  sample rate, and the PCM width follows the source's bit depth (lossy
  sources, which have none, become 16 bit). A source that the target cannot
  hold losslessly -- floating point or 32 bit integer into FLAC -- is skipped.
* **Never replace the original without a good result.** ffmpeg writes to a
  ``.part`` file next to the target; that file is probed (sample rate, bit
  depth, length) and tagged before it is renamed into place, and only then is
  the original removed -- unless "Keep original" is on.
* **Say when it cannot get better.** A lossy source (AAC, MP3, Opus, Vorbis)
  is converted but flagged: a WAV made from 256 kbit/s AAC is still 256 kbit/s
  AAC, just bigger.

Spatial audio (Dolby Atmos, more than two channels) is left as it is: a WAV
would fold it down to stereo.

Context7: ffmpeg muxer options checked against the local binary
(``ffmpeg -h muxer=wav`` / ``muxer=aiff``, ffmpeg 9.0).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .audio_tags import read_tags, write_tags

PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")

#: Target format setting values. "off" keeps every download as it arrives.
TARGET_OFF = "off"
TARGET_FORMATS = ("wav", "flac", "aiff")
TARGET_CHOICES = (TARGET_OFF,) + TARGET_FORMATS

TARGET_EXTENSION = {"wav": ".wav", "flac": ".flac", "aiff": ".aiff"}
_SAME_FORMAT_EXTENSIONS = {"wav": {".wav"}, "flac": {".flac"}, "aiff": {".aif", ".aiff"}}

#: What may be converted at all. Video, AC-3/E-AC-3 and other spatial
#: containers stay out on purpose (see module docstring).
CONVERTIBLE_EXTENSIONS = frozenset(
    {".flac", ".m4a", ".mp3", ".opus", ".ogg", ".wav", ".aif", ".aiff", ".aac"}
)
LOSSY_CODECS = frozenset({"aac", "mp3", "opus", "vorbis"})
#: Everything not in here counts as lossy (AC-3, E-AC-3, unknown codecs).
LOSSLESS_CODECS = frozenset({"flac", "alac", "pcm"})

#: How far the converted length may drift from the source. Lossless decoding
#: is sample exact; lossy decoders add or trim encoder padding.
LOSSLESS_TOLERANCE_SEC = 0.05
LOSSY_TOLERANCE_SEC = 0.5

LOSSY_WARNING = "lossy source — converting does not make it sound better"


@dataclass
class AudioInfo:
    codec: str                    # flac, alac, pcm, aac, mp3, opus, vorbis
    sample_rate: int = 0
    bits: Optional[int] = None    # None for lossy codecs
    bitrate: int = 0              # bits per second
    duration: float = 0.0
    channels: int = 2
    is_float: bool = False

    @property
    def is_lossy(self) -> bool:
        return self.codec not in LOSSLESS_CODECS


@dataclass
class ConvertResult:
    source: str
    status: str                   # converted | skipped | failed | cancelled
    output: Optional[str] = None
    reason: str = ""
    lossy_source: bool = False
    removed_original: bool = False


@dataclass
class FolderSummary:
    found: int = 0
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    results: List[ConvertResult] = field(default_factory=list)


# -- probing -----------------------------------------------------------------

def probe(path: str) -> Optional[AudioInfo]:
    """Stream facts of ``path`` read with mutagen, or ``None`` if unreadable."""
    import mutagen

    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None or getattr(audio, "info", None) is None:
        return None
    info = audio.info
    kind = type(audio).__name__.lower()
    codec = {
        "flac": "flac", "wave": "pcm", "aiff": "pcm", "mp3": "mp3", "easymp3": "mp3",
        "oggopus": "opus", "oggvorbis": "vorbis", "aac": "aac",
    }.get(kind)
    if kind in ("mp4", "easymp4"):
        raw = str(getattr(info, "codec", "") or "").lower()
        codec = "alac" if raw.startswith("alac") else "aac" if raw.startswith("mp4a") else raw
    if not codec:
        return None
    bits = getattr(info, "bits_per_sample", None) or None
    if codec not in LOSSLESS_CODECS:
        bits = None
    is_float = False
    if codec == "pcm" and kind == "wave":
        # mutagen reports the width; the format tag tells float from integer.
        is_float = _wav_is_float(path)
    elif codec == "pcm" and kind == "aiff":
        float_bits = _aiff_float_bits(path)
        if float_bits:
            is_float, bits = True, float_bits
    return AudioInfo(
        codec=codec,
        sample_rate=int(getattr(info, "sample_rate", 0) or 0),
        bits=int(bits) if bits else None,
        bitrate=int(getattr(info, "bitrate", 0) or 0),
        duration=float(getattr(info, "length", 0.0) or 0.0),
        channels=int(getattr(info, "channels", 2) or 2),
        is_float=is_float,
    )


def _aiff_float_bits(path: str) -> Optional[int]:
    """32 or 64 for an AIFF-C with float samples (fl32/fl64), else ``None``."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
            if head[:4] != b"FORM" or head[8:12] != b"AIFC":
                return None
            while True:
                chunk = handle.read(8)
                if len(chunk) < 8:
                    return None
                size = int.from_bytes(chunk[4:8], "big")
                if chunk[:4] == b"COMM":
                    data = handle.read(min(size, 64))
                    kind = data[18:22].lower()
                    return {b"fl32": 32, b"fl64": 64}.get(kind)
                handle.seek(size + (size & 1), 1)
    except OSError:
        return None


def _wav_is_float(path: str) -> bool:
    """True for WAVE_FORMAT_IEEE_FLOAT (3), also inside WAVE_FORMAT_EXTENSIBLE."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    at = head.find(b"fmt ")
    if at < 0 or len(head) < at + 10:
        return False
    tag = int.from_bytes(head[at + 8:at + 10], "little")
    if tag == 0xFFFE and len(head) >= at + 34:  # extensible: sub-format GUID
        tag = int.from_bytes(head[at + 32:at + 34], "little")
    return tag == 3


def describe(info: Optional[AudioInfo]) -> str:
    """Short quality label for the queue: ``FLAC 24-bit/96 kHz``, ``AAC 256 kbit/s``."""
    if info is None:
        return ""
    name = {"pcm": "PCM", "alac": "ALAC"}.get(info.codec, info.codec.upper())
    if info.is_lossy:
        kbps = round(info.bitrate / 1000) if info.bitrate else 0
        return f"{name} {kbps} kbit/s" if kbps else name
    rate = f"{info.sample_rate / 1000:g} kHz" if info.sample_rate else ""
    bits = f"{info.bits}-bit" if info.bits else ""
    detail = "/".join(part for part in (bits, rate) if part)
    return f"{name} {detail}".strip()


def summarize(labels: Iterable[str]) -> str:
    """One label for several files: the common one, or each with its count."""
    counts: Dict[str, int] = {}
    for label in labels:
        if label:
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    if len(counts) == 1:
        return next(iter(counts))
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(f"{label} ×{n}" for label, n in ordered)


# -- one file -----------------------------------------------------------------

def output_path_for(source: str, target: str) -> str:
    return os.path.splitext(source)[0] + TARGET_EXTENSION[target]


def ffmpeg_arguments(info: AudioInfo, target: str) -> Optional[List[str]]:
    """Codec arguments that keep rate and depth, or ``None`` if not lossless."""
    bits = info.bits or 16  # lossy sources: 16 bit, never more
    if target in ("wav", "aiff"):
        endian = "le" if target == "wav" else "be"
        if info.is_float:
            return ["-c:a", f"pcm_f{64 if info.bits == 64 else 32}{endian}"]
        if bits not in (16, 24, 32):
            bits = 24 if bits > 16 else 16
        return ["-c:a", f"pcm_s{bits}{endian}"]
    if target == "flac":
        if info.is_float or bits > 24:
            return None  # FLAC cannot hold it without losing precision
        if bits <= 16:
            return ["-c:a", "flac", "-sample_fmt", "s16"]
        return ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    raise ValueError(f"Unknown target format {target!r}")


def convert_file(
    source: str,
    target: str,
    *,
    keep_original: bool = False,
    ffmpeg: str = "ffmpeg",
    cancel: Optional[threading.Event] = None,
) -> ConvertResult:
    """Convert ``source`` to ``target``; the original stays unless it worked."""
    if target not in TARGET_FORMATS:
        raise ValueError(f"Unknown target format {target!r}")
    ext = os.path.splitext(source)[1].lower()
    if ext in _SAME_FORMAT_EXTENSIONS[target]:
        return ConvertResult(source, "skipped", reason=f"already {target.upper()}")
    if ext not in CONVERTIBLE_EXTENSIONS:
        return ConvertResult(source, "skipped", reason="not a convertible audio file")

    info = probe(source)
    if info is None:
        return ConvertResult(source, "failed", reason="could not read the audio stream")
    if info.channels > 2:
        return ConvertResult(source, "skipped", reason="spatial audio is kept as it is")
    codec_args = ffmpeg_arguments(info, target)
    if codec_args is None:
        return ConvertResult(source, "skipped",
                             reason=f"{target.upper()} cannot hold this source without loss")

    output = output_path_for(source, target)
    if _target_exists(output, target):
        return ConvertResult(source, "skipped", output=output, reason="target file exists")
    with _claim(output) as claimed:
        if not claimed:
            return ConvertResult(source, "skipped", output=output,
                                 reason="already being converted (download or Convert folder)")
        return _convert_claimed(source, target, info, codec_args, output, keep_original, ffmpeg, cancel)


def _convert_claimed(source, target, info, codec_args, output, keep_original, ffmpeg, cancel):
    partial = f"{output}.{os.getpid()}-{threading.get_ident()}.part"
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", source,
               "-map", "0:a:0", "-map_metadata", "-1", "-vn", *codec_args, "-f", target, partial]
    outcome = _run(command, cancel)
    if outcome is not None:
        _remove_quietly(partial)
        status = "cancelled" if outcome == "cancelled" else "failed"
        return ConvertResult(source, status, reason=outcome, lossy_source=info.is_lossy)

    problem = _verify(partial, info, target)
    lost: List[str] = []
    if problem is None:
        try:
            lost = write_tags(partial, target, read_tags(source))
        except Exception as exc:
            problem = f"could not copy the tags: {exc}"
    if problem is not None:
        _remove_quietly(partial)
        return ConvertResult(source, "failed", reason=problem, lossy_source=info.is_lossy)

    if _target_exists(output, target):  # someone else wrote it meanwhile
        _remove_quietly(partial)
        return ConvertResult(source, "skipped", output=output, reason="target file exists")
    try:
        _rename_no_replace(partial, output)
    except OSError as exc:
        _remove_quietly(partial)
        return ConvertResult(source, "failed", reason=f"could not save: {exc}",
                             lossy_source=info.is_lossy)

    if lost:
        # The copy is good, but these tags had no place in it: never delete
        # the only file that still has them.
        names = ", ".join(sorted(set(lost))[:6])
        return ConvertResult(source, "converted", output=output, lossy_source=info.is_lossy,
                             reason=f"original kept: {names} cannot be stored in {target.upper()}")

    removed = False
    if not keep_original:
        try:
            os.remove(source)
            removed = True
        except OSError:
            pass  # the copy is good; a locked original just stays
    return ConvertResult(source, "converted", output=output,
                         reason=LOSSY_WARNING if info.is_lossy else "",
                         lossy_source=info.is_lossy, removed_original=removed)


def _verify(partial: str, source: AudioInfo, target: str) -> Optional[str]:
    """Why the fresh file is not a faithful copy, or ``None`` when it is."""
    result = probe(partial) if os.path.isfile(partial) else None
    if result is None:
        return "ffmpeg wrote no readable file"
    if source.sample_rate and result.sample_rate != source.sample_rate:
        return f"sample rate changed ({source.sample_rate} → {result.sample_rate})"
    if result.is_float != source.is_float:
        return "sample format changed (float ↔ integer)"
    if source.bits and result.bits != source.bits:
        return f"bit depth changed ({source.bits} → {result.bits})"
    tolerance = LOSSY_TOLERANCE_SEC if source.is_lossy else LOSSLESS_TOLERANCE_SEC
    if source.duration and abs(result.duration - source.duration) > tolerance:
        return f"length changed ({source.duration:.2f} s → {result.duration:.2f} s)"
    return None


def _run(command: List[str], cancel: Optional[threading.Event]) -> Optional[str]:
    """Run ffmpeg without a shell; ``None`` on success, else the reason."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, creationflags=flags)
    except OSError as exc:
        return f"ffmpeg not found ({exc.strerror or exc})"
    stderr_lines: List[bytes] = []
    reader = threading.Thread(target=lambda: stderr_lines.extend(process.stderr), daemon=True)
    reader.start()
    while process.poll() is None:
        if cancel is not None and cancel.is_set():
            process.kill()
            process.wait()
            return "cancelled"
        time.sleep(0.05)
    reader.join(timeout=2)
    if process.returncode != 0:
        text = b"".join(stderr_lines).decode("utf-8", "replace").strip().splitlines()
        return f"ffmpeg failed: {text[-1][:200]}" if text else "ffmpeg failed"
    return None


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


#: Targets being written right now, by the download hook or "Convert folder…"
#: (both run in this process). One writer per target, so two converters can
#: never share a file or race to the rename.
_CLAIMS: Set[str] = set()
_CLAIMS_LOCK = threading.Lock()


@contextmanager
def _claim(output: str):
    key = os.path.normcase(os.path.abspath(output))
    with _CLAIMS_LOCK:
        claimed = key not in _CLAIMS
        if claimed:
            _CLAIMS.add(key)
    try:
        yield claimed
    finally:
        if claimed:
            with _CLAIMS_LOCK:
                _CLAIMS.discard(key)


def _target_exists(output: str, target: str) -> bool:
    """True when the target, or its other spelling (.aif for .aiff), exists."""
    stem = os.path.splitext(output)[0]
    return any(os.path.exists(stem + ext) for ext in _SAME_FORMAT_EXTENSIONS[target])


def _rename_no_replace(partial: str, output: str) -> None:
    """Move ``partial`` to ``output`` without ever replacing an existing file.

    ``os.rename`` refuses an existing target on Windows; elsewhere a hard link
    does (falling back to a checked rename on filesystems without links).
    """
    if sys.platform == "win32":
        os.rename(partial, output)
        return
    try:
        os.link(partial, output)
    except FileExistsError:
        raise
    except OSError:
        if os.path.exists(output):
            raise FileExistsError(output)
        os.rename(partial, output)
        return
    os.remove(partial)


# -- many files --------------------------------------------------------------

def scan(root: str) -> Tuple[List[str], List[str]]:
    """``(convertible audio files, .m3u/.m3u8 playlists)`` under ``root``, one walk."""
    audio: List[str] = []
    playlists: List[str] = []
    if not root or not os.path.isdir(root):
        return audio, playlists
    for folder, _dirs, files in os.walk(root):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in CONVERTIBLE_EXTENSIONS:
                audio.append(os.path.join(folder, name))
            elif ext in PLAYLIST_EXTENSIONS:
                playlists.append(os.path.join(folder, name))
    return sorted(audio), sorted(playlists)


def find_audio(root: str, *, since: Optional[float] = None) -> List[str]:
    """Convertible audio files under ``root``, sorted; ``since`` keeps new ones.

    "New" means created at or after ``since``. That alone is not proof (Linux
    keeps no creation time, copies get a fresh one), so the download hook also
    drops every path that existed when the download started.
    """
    found = scan(root)[0]
    if since is None:
        return found
    return [path for path in found if created_at(path) >= since]


def created_at(path: str) -> float:
    """When ``path`` came into being (birth time where the OS keeps one)."""
    try:
        st = os.stat(path)
    except OSError:
        return 0.0
    birth = getattr(st, "st_birthtime", None)
    if birth:
        return float(birth)
    if sys.platform == "win32":
        return float(st.st_ctime)  # creation time on Windows
    return float(st.st_mtime)  # Linux keeps no birth time in stat


def convert_paths(
    paths: Iterable[str],
    target: str,
    *,
    keep_original: bool = False,
    ffmpeg: str = "ffmpeg",
    cancel: Optional[threading.Event] = None,
    progress: Optional[Callable[[int, int, ConvertResult], None]] = None,
) -> FolderSummary:
    """Convert ``paths`` one after another; ``progress(done, total, result)``.

    Where several files share a name (``x.flac`` and ``x.m4a``), the lossless
    one goes first, so the target is made from the best source and the others
    are skipped as "target file exists".
    """
    paths = sorted(paths, key=_source_rank)
    summary = FolderSummary(found=len(paths))
    for index, path in enumerate(paths, start=1):
        if cancel is not None and cancel.is_set():
            summary.cancelled = True
            break
        result = convert_file(path, target, keep_original=keep_original, ffmpeg=ffmpeg,
                              cancel=cancel)
        summary.results.append(result)
        if result.status == "converted":
            summary.converted += 1
        elif result.status == "skipped":
            summary.skipped += 1
        elif result.status == "failed":
            summary.failed += 1
        else:
            summary.cancelled = True
        if progress is not None:
            progress(index, len(paths), result)
        if summary.cancelled:
            break
    return summary


#: Lossless containers first when two files share a stem.
_RANK = {".flac": 0, ".wav": 0, ".aif": 0, ".aiff": 0, ".m4a": 1}


def _source_rank(path: str) -> Tuple[str, int, str]:
    stem, ext = os.path.splitext(path)
    return os.path.normcase(stem), _RANK.get(ext.lower(), 2), ext.lower()


def convert_folder(folder: str, target: str, **kwargs) -> FolderSummary:
    """Convert every convertible file under ``folder`` and fix its playlists."""
    audio, playlists = scan(folder)
    summary = convert_paths(audio, target, **kwargs)
    rewrite_playlists(playlists, renamed_by(summary))
    return summary


def renamed_by(summary: FolderSummary) -> Dict[str, str]:
    """Old path -> new path for every original that was replaced."""
    return {r.source: r.output for r in summary.results
            if r.status == "converted" and r.removed_original and r.output}


# -- playlists -----------------------------------------------------------------

def rewrite_playlists(playlists: Iterable[str], renamed: Dict[str, str]) -> int:
    """Point ``.m3u``/``.m3u8`` entries at the converted files.

    ``renamed`` maps old absolute path -> new absolute path. Only lines that
    resolve to a renamed file change, and they keep their form (relative stays
    relative): just the extension is swapped. Returns the files rewritten.
    """
    lookup = {os.path.normcase(os.path.abspath(old)): new for old, new in renamed.items()}
    changed_files = 0
    if not lookup:
        return 0
    for playlist in playlists:
        try:
            with open(playlist, "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        # .m3u from other tools is often cp1252/latin-1; latin-1 maps every byte,
        # so writing back in it leaves untouched lines byte for byte as they were.
        try:
            encoding, text = "utf-8", raw.decode("utf-8")
        except UnicodeDecodeError:
            encoding, text = "latin-1", raw.decode("latin-1")
        lines = text.splitlines(keepends=True)
        base = os.path.dirname(playlist)
        changed = False
        for index, line in enumerate(lines):
            entry = line.rstrip("\r\n")
            if not entry or entry.startswith("#"):
                continue
            resolved = entry if os.path.isabs(entry) else os.path.join(base, entry)
            new = lookup.get(os.path.normcase(os.path.abspath(resolved)))
            if new is None:
                continue
            ending = line[len(entry):]
            lines[index] = os.path.splitext(entry)[0] + os.path.splitext(new)[1] + ending
            changed = True
        if changed:
            temp = playlist + ".part"
            try:
                with open(temp, "w", encoding=encoding, newline="") as handle:
                    handle.writelines(lines)
                os.replace(temp, playlist)
                changed_files += 1
            except OSError:
                _remove_quietly(temp)
    return changed_files
