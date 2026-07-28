"""Tests for hires.quality."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.quality import (  # noqa: E402
    HIRES,
    QUALITY_ORDER,
    TIDAL_STREAM_QUALITY,
    build_search_result_data,
    effective_tier_for_platform,
    is_at_least,
    is_lossless,
    label_for,
    normalize_tier,
)


class TestNormalizeTier:
    def test_known_tiers_pass_through(self):
        for tier in QUALITY_ORDER:
            assert normalize_tier(tier) == tier

    def test_case_and_whitespace_insensitive(self):
        assert normalize_tier("  HiFi ") == "hifi"
        assert normalize_tier("LOSSLESS") == "lossless"

    def test_aliases(self):
        assert normalize_tier("Hi-Res") == "hifi"
        assert normalize_tier("hires") == "hifi"
        assert normalize_tier("master") == "hifi"
        assert normalize_tier("FLAC") == "lossless"
        assert normalize_tier("Dolby Atmos") == "atmos"

    def test_unknown_falls_back_to_default(self):
        assert normalize_tier("nonsense") == HIRES
        assert normalize_tier(None) == HIRES
        assert normalize_tier("", default="high") == "high"


class TestComparisons:
    def test_is_at_least(self):
        assert is_at_least("hifi", "lossless")
        assert is_at_least("lossless", "lossless")
        assert not is_at_least("high", "lossless")
        assert is_at_least("atmos", "minimum")

    def test_is_lossless(self):
        assert is_lossless("hifi")
        assert is_lossless("lossless")
        assert is_lossless("atmos")
        assert not is_lossless("high")
        assert not is_lossless("low")

    def test_label_for(self):
        assert "24 bit" in label_for("hifi")
        assert label_for("lossless").startswith("Lossless")


class TestTidalMapping:
    def test_hifi_is_the_hi_res_stream(self):
        assert TIDAL_STREAM_QUALITY["hifi"] == "HI_RES_LOSSLESS"

    def test_every_tier_maps(self):
        for tier in QUALITY_ORDER:
            assert tier in TIDAL_STREAM_QUALITY


class TestBuildSearchResultData:
    def test_none_when_nothing_to_override(self):
        assert build_search_result_data(quality=None) is None

    def test_sets_override_key_gui_reads(self):
        data = build_search_result_data(quality="hifi", media_type="track")
        assert data["extra_kwargs"]["download_quality_override"] == "hifi"
        assert data["type"] == "track"

    def test_normalizes_the_tier(self):
        data = build_search_result_data(quality="Hi-Res")
        assert data["extra_kwargs"]["download_quality_override"] == "hifi"

    def test_does_not_mutate_the_base_dict(self):
        base = {"type": "album", "extra_kwargs": {"existing": 1}}
        data = build_search_result_data(quality="hifi", base=base)
        assert base["extra_kwargs"] == {"existing": 1}
        assert data["extra_kwargs"]["existing"] == 1
        assert data["extra_kwargs"]["download_quality_override"] == "hifi"

    def test_base_type_wins_over_media_type(self):
        data = build_search_result_data(quality="hifi", media_type="track", base={"type": "album"})
        assert data["type"] == "album"

    def test_base_without_quality_still_returns_data(self):
        data = build_search_result_data(quality=None, base={"type": "track"})
        assert data is not None
        assert "download_quality_override" not in (data.get("extra_kwargs") or {})


class TestPlatformFallback:
    def test_spotify_cannot_do_lossless(self):
        # Librespot serves Ogg Vorbis only; gui.py downgrades these tiers too.
        assert effective_tier_for_platform("hifi", "Spotify") == "high"
        assert effective_tier_for_platform("lossless", "spotify") == "high"
        assert effective_tier_for_platform("atmos", "SPOTIFY") == "high"

    def test_spotify_keeps_lossy_tiers(self):
        assert effective_tier_for_platform("high", "Spotify") == "high"
        assert effective_tier_for_platform("low", "Spotify") == "low"

    def test_tidal_keeps_hi_res(self):
        assert effective_tier_for_platform("hifi", "TIDAL") == "hifi"
        assert effective_tier_for_platform("atmos", "TIDAL") == "atmos"

    def test_unknown_platform_untouched(self):
        assert effective_tier_for_platform("hifi", "") == "hifi"
