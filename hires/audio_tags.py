"""Carry tags and cover art from one audio file to another -- all of them.

ffmpeg converts the audio; this module moves the metadata, because ffmpeg's
WAV muxer can only write a RIFF INFO chunk (no cover, few fields) and DJ
software reads the ID3 chunk that Mp3tag, Serato and Traktor write.

Nothing may get lost on the way, so there is no whitelist:

* **ID3 → ID3** (MP3/WAV/AIFF → WAV/AIFF): every frame is copied as it is,
  binary ones included (Serato cue points in GEOB, ratings in POPM, lyrics).
* **Everything else** goes through a neutral form ``{name: [values]}`` that
  keeps *every* text field: known ones map to their frame or atom, the rest
  travel under their own name (``TXXX:<NAME>`` in ID3, ``NAME=`` in FLAC).
  Pictures become APIC frames or FLAC pictures, lyrics USLT or ``LYRICS``.
* What the target cannot hold (binary ID3 frames in FLAC, odd MP4 atoms) is
  reported by :func:`write_tags`; the caller then keeps the original.

Context7: mutagen (quodlibet/mutagen) -- FLAC.add_picture/Picture, AIFF/WAVE
``add_tags`` with ``mutagen.id3.ID3`` tags.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Neutral name (Vorbis comment style) <-> ID3 text frame.
_ID3_TEXT = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
    "date": "TDRC", "genre": "TCON", "isrc": "TSRC", "bpm": "TBPM", "initialkey": "TKEY",
    "composer": "TCOM", "label": "TPUB", "copyright": "TCOP", "grouping": "TIT1",
    "subtitle": "TIT3", "conductor": "TPE3", "remixer": "TPE4", "lyricist": "TEXT",
    "encodedby": "TENC", "encoder": "TSSE", "media": "TMED", "language": "TLAN",
    "mood": "TMOO", "originaldate": "TDOR", "artistsort": "TSOP", "albumsort": "TSOA",
    "titlesort": "TSOT", "albumartistsort": "TSO2", "compilation": "TCMP",
}
_ID3_NAME = {frame: name for name, frame in _ID3_TEXT.items()}

_VORBIS_ALIASES = {"organization": "label", "publisher": "label", "key": "initialkey",
                   "description": "comment", "album artist": "albumartist",
                   "unsyncedlyrics": "lyrics", "totaltracks": "tracktotal", "totaldiscs": "disctotal"}

_MP4_ATOMS = {
    "\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album", "aART": "albumartist",
    "\xa9day": "date", "\xa9gen": "genre", "\xa9wrt": "composer", "\xa9cmt": "comment",
    "cprt": "copyright", "\xa9lyr": "lyrics", "\xa9grp": "grouping", "\xa9too": "encoder",
    "soar": "artistsort", "soal": "albumsort", "sonm": "titlesort", "soaa": "albumartistsort",
}

_FRONT_COVER = 3


@dataclass
class Cover:
    data: bytes
    mime: str = "image/jpeg"
    kind: int = _FRONT_COVER  # ID3/FLAC picture type
    description: str = ""


Tags = Dict[str, List[str]]


@dataclass
class TagSet:
    fields: Tags = field(default_factory=dict)
    covers: List[Cover] = field(default_factory=list)
    #: The source's own ID3 tag, copied frame by frame into an ID3 target.
    id3: Any = None
    #: Source items the neutral form cannot hold (binary frames, odd atoms).
    unportable: List[str] = field(default_factory=list)


def read_tags(path: str) -> TagSet:
    """Everything ``path`` carries; an empty set when it has no tags."""
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4

    audio = mutagen.File(path)  # raises on a broken file: the caller keeps the original
    if audio is None:
        return TagSet()
    tags = getattr(audio, "tags", None)
    if isinstance(audio, MP4):
        return _from_mp4(tags or {})
    if isinstance(tags, ID3):
        return _from_id3(tags)
    if isinstance(audio, FLAC):
        result = TagSet(fields=_from_vorbis(tags))
        result.covers = [_cover_from_picture(p) for p in audio.pictures]
        return result
    if tags is not None and hasattr(tags, "keys"):  # OggVorbis, OggOpus
        result = TagSet(fields=_from_vorbis(tags))
        for raw in tags.get("metadata_block_picture", []) or []:
            cover = _cover_from_ogg(raw)
            if cover is not None:
                result.covers.append(cover)
        return result
    return TagSet()


def write_tags(path: str, kind: str, tags: TagSet) -> List[str]:
    """Write ``tags`` into ``path`` (a fresh ``kind`` file); returns what did not fit.

    ``kind`` is ``"flac"``, ``"wav"`` or ``"aiff"`` -- given explicitly because
    the file is still a temporary ``.part`` file. Raises on a write error.
    """
    if kind == "flac":
        from mutagen.flac import FLAC

        audio = FLAC(path)
        if audio.tags is None:
            audio.add_tags()
        for name, values in tags.fields.items():
            audio.tags[name] = list(values)
        for cover in tags.covers:
            audio.add_picture(_picture_from_cover(cover))
        audio.save()
        return list(tags.unportable)

    if kind in ("wav", "aiff"):
        if kind == "wav":
            from mutagen.wave import WAVE as Container
        else:
            from mutagen.aiff import AIFF as Container
        audio = Container(path)
        if audio.tags is None:
            audio.add_tags()
        if tags.id3 is not None:
            for frame in tags.id3.values():  # 1:1, binary frames included
                audio.tags.add(copy.deepcopy(frame))
            audio.save()
            return []
        _fill_id3(audio.tags, tags.fields, tags.covers)
        audio.save()
        return list(tags.unportable)

    raise ValueError(f"No tag writer for {kind!r}")


# -- readers ----------------------------------------------------------------

def _from_vorbis(comments) -> Tags:
    out: Tags = {}
    if not comments:
        return out
    for key, values in comments.items():
        name = key.lower()
        if name == "metadata_block_picture":
            continue
        name = _VORBIS_ALIASES.get(name, name)
        if values:
            out.setdefault(name, []).extend(str(v) for v in values)
    return out


def _from_id3(id3) -> TagSet:
    result = TagSet(id3=id3)
    out = result.fields
    for frame in id3.values():
        frame_id = frame.FrameID
        if frame_id == "TXXX":
            out.setdefault((frame.desc or "txxx").lower(), []).extend(str(t) for t in frame.text)
        elif frame_id == "TRCK" or frame_id == "TPOS":
            number, _, total = str(frame.text[0]).partition("/")
            base = "track" if frame_id == "TRCK" else "disc"
            out[f"{base}number"] = [number]
            if total:
                out[f"{base}total"] = [total]
        elif frame_id.startswith("T"):
            name = _ID3_NAME.get(frame_id, frame_id.lower())
            out.setdefault(name, []).extend(str(t) for t in frame.text)
        elif frame_id == "COMM":
            out.setdefault("comment", []).extend(str(t) for t in frame.text)
        elif frame_id == "USLT":
            out.setdefault("lyrics", []).append(str(frame.text))
        elif frame_id == "APIC":
            result.covers.append(Cover(data=frame.data, mime=frame.mime or "image/jpeg",
                                       kind=int(frame.type), description=frame.desc or ""))
        elif frame_id == "WXXX":
            out.setdefault((frame.desc or "url").lower(), []).append(str(frame.url))
        elif frame_id.startswith("W"):
            out.setdefault(frame_id.lower(), []).append(str(frame.url))
        else:  # GEOB, POPM, PRIV, UFID, SYLT, ...: binary, no text form
            result.unportable.append(frame.HashKey)
    return result


def _from_mp4(tags) -> TagSet:
    from mutagen.mp4 import MP4Cover, MP4FreeForm

    result = TagSet()
    out = result.fields
    for atom, values in tags.items():
        if atom in _MP4_ATOMS:
            out[_MP4_ATOMS[atom]] = [str(v) for v in values]
        elif atom in ("trkn", "disk"):
            number, total = (tuple(values[0]) + (0, 0))[:2] if values else (0, 0)
            base = "track" if atom == "trkn" else "disc"
            out[f"{base}number"] = [str(number)]
            if total:
                out[f"{base}total"] = [str(total)]
        elif atom == "tmpo":
            out["bpm"] = [str(v) for v in values]
        elif atom == "covr":
            for cover in values:
                mime = "image/png" if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
                result.covers.append(Cover(data=bytes(cover), mime=mime))
        elif atom.startswith("----:"):
            name = atom.rsplit(":", 1)[-1].lower()
            texts = []
            for value in values:
                if isinstance(value, (bytes, MP4FreeForm)):
                    try:
                        texts.append(bytes(value).decode("utf-8"))
                    except UnicodeDecodeError:
                        result.unportable.append(atom)
                        texts = []
                        break
                else:
                    texts.append(str(value))
            if texts:
                out[_VORBIS_ALIASES.get(name, name)] = texts
        elif all(isinstance(v, (str, int, bool)) for v in values):
            out[atom.replace("\xa9", "").lower()] = [str(int(v)) if isinstance(v, bool) else str(v) for v in values]
        else:
            result.unportable.append(atom)
    return result


def _cover_from_picture(picture) -> Cover:
    return Cover(data=picture.data, mime=picture.mime or "image/jpeg",
                 kind=int(picture.type), description=picture.desc or "")


def _cover_from_ogg(raw: str) -> Optional[Cover]:
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

    for base, frame_id in (("track", "TRCK"), ("disc", "TPOS")):
        number = (tags.get(f"{base}number") or [""])[0]
        total = (tags.get(f"{base}total") or [""])[0]
        if number or total:
            id3.add(getattr(frames, frame_id)(encoding=3, text=[f"{number}/{total}" if total else number]))
    skip = {"tracknumber", "tracktotal", "discnumber", "disctotal"}
    for name, values in tags.items():
        if name in skip:
            continue
        if name == "comment":
            id3.add(frames.COMM(encoding=3, lang="eng", desc="", text=list(values)))
        elif name == "lyrics":
            id3.add(frames.USLT(encoding=3, lang="eng", desc="", text="\n".join(values)))
        elif name in _ID3_TEXT:
            id3.add(getattr(frames, _ID3_TEXT[name])(encoding=3, text=list(values)))
        else:
            id3.add(frames.TXXX(encoding=3, desc=name.upper(), text=list(values)))
    for cover in covers:
        id3.add(frames.APIC(encoding=3, mime=cover.mime, type=cover.kind,
                            desc=cover.description, data=cover.data))
