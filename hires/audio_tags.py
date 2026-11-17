"""Carry tags and cover art from one audio file to another.

ffmpeg converts the audio; this module moves the metadata, because ffmpeg's
WAV muxer can only write a RIFF INFO chunk (no cover, few fields) and DJ
software reads the ID3 chunk that Mp3tag, Serato and Traktor write. One
neutral form sits in the middle: ``{field: [values]}`` plus a list of
:class:`Cover`, read from FLAC/Ogg (Vorbis comments), MP4/M4A (atoms) or
ID3 (MP3, WAV, AIFF), and written as Vorbis comments (FLAC) or ID3 (WAV, AIFF).

Context7: mutagen (quodlibet/mutagen) -- FLAC.add_picture/Picture, AIFF/WAVE
``add_tags`` with ``mutagen.id3.ID3`` tags.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Dict, List, Tuple

#: The fields that survive a conversion. Names follow Vorbis comments.
FIELDS = (
    "title", "artist", "album", "albumartist", "date", "genre", "tracknumber",
    "discnumber", "isrc", "bpm", "initialkey", "comment", "composer", "label",
    "copyright",
)

_VORBIS_ALIASES = {"organization": "label", "publisher": "label", "key": "initialkey",
                   "description": "comment", "album artist": "albumartist"}

_ID3_FRAMES = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
    "date": "TDRC", "genre": "TCON", "tracknumber": "TRCK", "discnumber": "TPOS",
    "isrc": "TSRC", "bpm": "TBPM", "initialkey": "TKEY", "composer": "TCOM",
    "label": "TPUB", "copyright": "TCOP",
}

_MP4_ATOMS = {
    "\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album", "aART": "albumartist",
    "\xa9day": "date", "\xa9gen": "genre", "\xa9wrt": "composer", "\xa9cmt": "comment",
    "cprt": "copyright",
}

_FRONT_COVER = 3


@dataclass
class Cover:
    data: bytes
    mime: str = "image/jpeg"
    kind: int = _FRONT_COVER  # ID3/FLAC picture type
    description: str = ""


Tags = Dict[str, List[str]]


def read_tags(path: str) -> Tuple[Tags, List[Cover]]:
    """Tags and covers of ``path`` in neutral form; empty when unreadable."""
    import mutagen

    try:
        audio = mutagen.File(path)
    except Exception:
        return {}, []
    if audio is None:
        return {}, []

    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4

    if isinstance(audio, MP4):
        return _from_mp4(audio)
    tags = getattr(audio, "tags", None)
    if isinstance(tags, ID3):
        return _from_id3(tags)
    if isinstance(audio, FLAC):
        values = _from_vorbis(audio.tags)
        covers = [_cover_from_picture(p) for p in audio.pictures]
        return values, covers
    if tags is not None and hasattr(tags, "keys"):  # OggVorbis, OggOpus
        values = _from_vorbis(tags)
        covers = []
        for raw in tags.get("metadata_block_picture", []) or []:
            cover = _cover_from_ogg(raw)
            if cover is not None:
                covers.append(cover)
        return values, covers
    return {}, []


def write_tags(path: str, kind: str, tags: Tags, covers: List[Cover]) -> None:
    """Write ``tags`` and ``covers`` into ``path``, a fresh ``kind`` file.

    ``kind`` is ``"flac"``, ``"wav"`` or ``"aiff"`` -- given explicitly because
    the file is still a temporary ``.part`` file at this point. Raises on a
    write error; the caller then keeps the original.
    """
    if kind == "flac":
        from mutagen.flac import FLAC

        audio = FLAC(path)
        if audio.tags is None:
            audio.add_tags()
        for name, values in tags.items():
            audio.tags[name] = list(values)
        for cover in covers:
            audio.add_picture(_picture_from_cover(cover))
        audio.save()
        return

    if kind in ("wav", "aiff"):
        if kind == "wav":
            from mutagen.wave import WAVE as Container
        else:
            from mutagen.aiff import AIFF as Container
        audio = Container(path)
        if audio.tags is None:
            audio.add_tags()
        _fill_id3(audio.tags, tags, covers)
        audio.save()
        return

    raise ValueError(f"No tag writer for {kind!r}")


# -- readers ----------------------------------------------------------------

def _from_vorbis(comments) -> Tags:
    out: Tags = {}
    if not comments:
        return out
    for key, values in comments.items():
        name = _VORBIS_ALIASES.get(key.lower(), key.lower())
        if name in FIELDS and values:
            out.setdefault(name, []).extend(str(v) for v in values)
    return out


def _from_id3(id3) -> Tuple[Tags, List[Cover]]:
    out: Tags = {}
    for name, frame_id in _ID3_FRAMES.items():
        frame = id3.get(frame_id)
        if frame is not None and getattr(frame, "text", None):
            out[name] = [str(t) for t in frame.text]
    comments = [str(t) for f in id3.getall("COMM") for t in f.text]
    if comments:
        out["comment"] = comments
    covers = [
        Cover(data=f.data, mime=f.mime or "image/jpeg", kind=int(f.type), description=f.desc or "")
        for f in id3.getall("APIC")
    ]
    return out, covers


def _from_mp4(audio) -> Tuple[Tags, List[Cover]]:
    from mutagen.mp4 import MP4Cover

    out: Tags = {}
    tags = audio.tags or {}
    for atom, name in _MP4_ATOMS.items():
        if atom in tags:
            out[name] = [str(v) for v in tags[atom]]
    for atom, name in (("trkn", "tracknumber"), ("disk", "discnumber")):
        if atom in tags and tags[atom]:
            number, total = (tuple(tags[atom][0]) + (0, 0))[:2]
            out[name] = [f"{number}/{total}" if total else str(number)]
    if "tmpo" in tags and tags["tmpo"]:
        out["bpm"] = [str(tags["tmpo"][0])]
    for key in tags.keys():
        if key.startswith("----:") and key.rsplit(":", 1)[-1].lower() in ("isrc", "label", "initialkey"):
            name = key.rsplit(":", 1)[-1].lower()
            values = [v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v) for v in tags[key]]
            out[name] = values
    covers = []
    for cover in tags.get("covr", []) or []:
        mime = "image/png" if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
        covers.append(Cover(data=bytes(cover), mime=mime))
    return out, covers


def _cover_from_picture(picture) -> Cover:
    return Cover(data=picture.data, mime=picture.mime or "image/jpeg",
                 kind=int(picture.type), description=picture.desc or "")


def _cover_from_ogg(raw: str):
    from mutagen.flac import Picture

    try:
        return _cover_from_picture(Picture(base64.b64decode(raw)))
    except Exception:
        return None


# -- writers ----------------------------------------------------------------

def _picture_from_cover(cover: Cover):
    from mutagen.flac import Picture

    picture = Picture()
    picture.data = cover.data
    picture.mime = cover.mime
    picture.type = cover.kind
    picture.desc = cover.description
    return picture


def _fill_id3(id3, tags: Tags, covers: List[Cover]) -> None:
    from mutagen import id3 as frames

    for name, values in tags.items():
        if name == "comment":
            id3.add(frames.COMM(encoding=3, lang="eng", desc="", text=list(values)))
            continue
        frame_id = _ID3_FRAMES.get(name)
        if frame_id:
            id3.add(getattr(frames, frame_id)(encoding=3, text=list(values)))
    for cover in covers:
        id3.add(frames.APIC(encoding=3, mime=cover.mime, type=cover.kind,
                            desc=cover.description, data=cover.data))
