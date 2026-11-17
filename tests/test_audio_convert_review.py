"""Review part 3: no tag, no precision and no file may be lost by a conversion.

Each test reproduces one finding of W/review-part3.md with real files.
"""

import os
import shutil
import subprocess
import sys
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("mutagen")
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")

from hires import audio_convert as ac  # noqa: E402
from hires import download_finish  # noqa: E402
from test_audio_convert import PNG, ffprobe, make_tone  # noqa: E402


def _id3_frames(path, kind):
    if kind == "wav":
        from mutagen.wave import WAVE as Container
    else:
        from mutagen.aiff import AIFF as Container
    return Container(path).tags


# -- finding 1: every tag survives, or the original stays ----------------------------

class TestTagsSurvive:
    def test_audio_convert_flac_extras_reach_the_wav(self, tmp_path):
        from mutagen.flac import FLAC

        source = make_tone(str(tmp_path / "t.flac"), rate=44100, bits=16, seconds=0.5)
        audio = FLAC(source)
        audio.update({"title": "Tone", "tracknumber": "3", "tracktotal": "12",
                      "lyrics": "la la la", "replaygain_track_gain": "-7.5 dB",
                      "serato_markers_v2": "YXBwbGljYXRpb24=", "musicbrainz_trackid": "abc-123",
                      "upc": "0123456789012"})
        audio.save()
        result = ac.convert_file(source, "wav", ffmpeg=FFMPEG)
        assert result.status == "converted", result.reason
        tags = _id3_frames(result.output, "wav")
        assert str(tags["TIT2"]) == "Tone" and str(tags["TRCK"]) == "3/12"
        assert tags.getall("USLT")[0].text == "la la la"
        txxx = {f.desc.upper(): f.text[0] for f in tags.getall("TXXX")}
        assert txxx["REPLAYGAIN_TRACK_GAIN"] == "-7.5 dB"
        assert txxx["SERATO_MARKERS_V2"] == "YXBwbGljYXRpb24="
        assert txxx["MUSICBRAINZ_TRACKID"] == "abc-123" and txxx["UPC"] == "0123456789012"
        assert result.removed_original

    def test_audio_convert_id3_frames_are_copied_one_to_one(self, tmp_path):
        from mutagen.id3 import APIC, GEOB, POPM, TIT2, TXXX, USLT
        from mutagen.mp3 import MP3

        source = make_tone(str(tmp_path / "t.mp3"), rate=44100, seconds=0.5, codec="libmp3lame")
        audio = MP3(source)
        if audio.tags is None:
            audio.add_tags()
        for frame in (TIT2(encoding=3, text=["Tone"]),
                      GEOB(encoding=0, mime="application/octet-stream", filename="",
                           desc="Serato Markers2", data=b"\x01\x01cues"),
                      USLT(encoding=3, lang="eng", desc="", text="la la"),
                      POPM(email="no@email", rating=196, count=3),
                      TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=["-6.1 dB"]),
                      APIC(encoding=3, mime="image/png", type=3, desc="", data=PNG)):
            audio.tags.add(frame)
        audio.save()
        before = {key: repr(frame) for key, frame in MP3(source).tags.items()}
        result = ac.convert_file(source, "aiff", ffmpeg=FFMPEG)
        assert result.status == "converted", result.reason
        after = {key: repr(frame) for key, frame in _id3_frames(result.output, "aiff").items()}
        assert after == before
        assert result.removed_original

    def test_audio_convert_tags_flac_cannot_hold_keep_the_original(self, tmp_path):
        from mutagen.id3 import GEOB, TIT2
        from mutagen.wave import WAVE

        source = make_tone(str(tmp_path / "t.wav"), rate=44100, bits=16, seconds=0.5)
        wav = WAVE(source)
        wav.add_tags()
        wav.tags.add(TIT2(encoding=3, text=["Tone"]))
        wav.tags.add(GEOB(encoding=0, mime="application/octet-stream", filename="",
                          desc="Serato Markers2", data=b"\x01\x01cues"))
        wav.save()
        result = ac.convert_file(source, "flac", ffmpeg=FFMPEG)
        assert result.status == "converted"
        assert os.path.exists(source) and not result.removed_original
        assert "original kept" in result.reason and "GEOB" in result.reason

    def test_audio_convert_m4a_freeform_and_lyrics_reach_the_aiff(self, tmp_path):
        from mutagen.mp4 import MP4, MP4FreeForm

        source = make_tone(str(tmp_path / "t.m4a"), rate=44100, seconds=0.5)
        audio = MP4(source)
        audio["\xa9nam"] = ["Tone"]
        audio["\xa9lyr"] = ["la la"]
        audio["trkn"] = [(2, 9)]
        audio["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(b"DEX123")]
        audio["----:com.apple.iTunes:MOOD"] = [MP4FreeForm(b"Dark")]
        audio.save()
        result = ac.convert_file(source, "aiff", ffmpeg=FFMPEG)
        tags = _id3_frames(result.output, "aiff")
        assert str(tags["TSRC"]) == "DEX123" and str(tags["TRCK"]) == "2/9"
        assert tags.getall("USLT")[0].text == "la la"
        assert "Dark" in str(tags.get("TMOO") or {f.desc: f for f in tags.getall("TXXX")}.get("MOOD"))
        assert result.removed_original


# -- finding 2: float stays float, at its own width ------------------------------------

class TestFloatSources:
    def test_audio_convert_float_aiff_stays_float_in_wav(self, tmp_path):
        source = str(tmp_path / "hot.aiff")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=0.5", "-af", "volume=8dB",
                        "-c:a", "pcm_f32be", source], check=True)
        assert ac.probe(source).is_float
        result = ac.convert_file(source, "wav", ffmpeg=FFMPEG)
        assert result.status == "converted", result.reason
        assert ffprobe(result.output)["codec"] == "pcm_f32le"

    def test_audio_convert_64_bit_float_keeps_its_width(self, tmp_path):
        source = make_tone(str(tmp_path / "f64.wav"), rate=48000, seconds=0.5, codec="pcm_f64le")
        result = ac.convert_file(source, "aiff", ffmpeg=FFMPEG)
        assert result.status == "converted", result.reason
        assert ffprobe(result.output)["codec"] == "pcm_f64be"


# -- finding 3: two converters never share a file, an existing file is never replaced -----

class TestNoOverwrite:
    def test_audio_convert_a_file_in_progress_is_not_converted_twice(self, tmp_path):
        source = make_tone(str(tmp_path / "t.flac"), seconds=0.5)
        with ac._claim(ac.output_path_for(source, "wav")) as claimed:
            assert claimed
            result = ac.convert_file(source, "wav", ffmpeg=FFMPEG)
        assert result.status == "skipped" and "already being converted" in result.reason
        assert os.path.exists(source)

    def test_audio_convert_target_appearing_meanwhile_is_not_replaced(self, tmp_path, monkeypatch):
        source = make_tone(str(tmp_path / "t.flac"), seconds=0.5)
        target = ac.output_path_for(source, "wav")
        real_verify = ac._verify

        def verify_while_someone_writes(*args):
            with open(target, "wb") as handle:
                handle.write(b"someone else's file")
            return real_verify(*args)

        monkeypatch.setattr(ac, "_verify", verify_while_someone_writes)
        result = ac.convert_file(source, "wav", ffmpeg=FFMPEG)
        assert result.status != "converted"
        assert open(target, "rb").read() == b"someone else's file"
        assert os.path.exists(source)
        assert [n for n in os.listdir(tmp_path) if n.endswith(".part")] == []


# -- finding 4: only paths that did not exist when the download started ------------------

class TestOnlyNewPaths:
    def test_audio_convert_files_present_at_the_start_are_never_touched(self, tmp_path, monkeypatch):
        folder = tmp_path / "Library"
        folder.mkdir()
        old = make_tone(str(folder / "old.flac"), seconds=0.3)
        existing = download_finish.snapshot(str(folder))
        new = make_tone(str(folder / "new.flac"), seconds=0.3)
        # Linux has no birth time; a copy or re-tag makes an old file look new.
        monkeypatch.setattr(ac, "created_at", lambda _p: 9e18)
        download_finish.process_download(str(folder), 0.0, "aiff", ffmpeg=FFMPEG, existing=existing)
        assert os.path.exists(old) and not os.path.exists(ac.output_path_for(old, "aiff"))
        assert os.path.exists(ac.output_path_for(new, "aiff"))


# -- lows ---------------------------------------------------------------------------------

class TestLows:
    def test_audio_convert_best_source_of_a_stem_wins(self, tmp_path):
        make_tone(str(tmp_path / "x.m4a"), rate=44100, seconds=0.5)
        make_tone(str(tmp_path / "x.flac"), rate=96000, bits=24, seconds=0.5)
        summary = ac.convert_folder(str(tmp_path), "wav", ffmpeg=FFMPEG)
        assert ffprobe(str(tmp_path / "x.wav"))["rate"] == 96000
        assert os.path.exists(tmp_path / "x.m4a")  # skipped, not lost
        assert summary.converted == 1

    def test_audio_convert_aif_counts_as_existing_aiff(self, tmp_path):
        source = make_tone(str(tmp_path / "x.flac"), seconds=0.3)
        (tmp_path / "x.aif").write_bytes(b"mine")
        result = ac.convert_file(source, "aiff", ffmpeg=FFMPEG)
        assert result.status == "skipped" and result.reason == "target file exists"

    def test_audio_convert_only_known_lossless_codecs_count_as_lossless(self):
        assert ac.AudioInfo("ac-3").is_lossy and ac.AudioInfo("ec-3").is_lossy
        assert not ac.AudioInfo("alac", bits=16).is_lossy
