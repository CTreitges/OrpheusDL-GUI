"""Hi-res quality handling.

The GUI stores a global download quality in ``globals.general.quality``, but a
single download can override it through
``search_result_data['extra_kwargs']['download_quality_override']`` (see
``run_download_in_thread`` in gui.py). The Spotify->TIDAL converter uses that
override so a converted playlist always pulls the best available stream,
regardless of what the global setting happens to be.

Quality tiers, as OrpheusDL names them, mapped to what TIDAL actually serves
(see modules/tidal/interface.py):

    minimum / low -> LOW               (96 kbit/s AAC)
    medium / high -> HIGH              (320 kbit/s AAC)
    lossless      -> LOSSLESS          (44.1 kHz / 16 bit FLAC)
    hifi          -> HI_RES_LOSSLESS   (up to 192 kHz / 24 bit FLAC)
    atmos         -> HI_RES_LOSSLESS   (Dolby Atmos, needs spatial codecs)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Tier that yields the highest available stereo quality on TIDAL.
HIRES = "hifi"

#: Ordered worst -> best. Used to compare tiers.
QUALITY_ORDER = ["minimum", "low", "medium", "high", "lossless", "hifi", "atmos"]

#: Tiers that are lossless or better.
LOSSLESS_TIERS = frozenset({"lossless", "hifi", "atmos"})

#: What each tier resolves to on TIDAL's side.
TIDAL_STREAM_QUALITY = {
    "minimum": "LOW",
    "low": "LOW",
    "medium": "HIGH",
    "high": "HIGH",
    "lossless": "LOSSLESS",
    "hifi": "HI_RES_LOSSLESS",
    "atmos": "HI_RES_LOSSLESS",
}

#: Human-readable labels for the GUI.
QUALITY_LABELS = {
    "minimum": "Minimum (96 kbit/s AAC)",
    "low": "Low (96 kbit/s AAC)",
    "medium": "Medium (320 kbit/s AAC)",
    "high": "High (320 kbit/s AAC)",
    "lossless": "Lossless (16 bit / 44.1 kHz FLAC)",
    "hifi": "Hi-Res (24 bit / up to 192 kHz FLAC)",
    "atmos": "Dolby Atmos",
}


def normalize_tier(value: Optional[str], default: str = HIRES) -> str:
    """Coerce arbitrary user input to a known tier name."""
    tier = (value or "").strip().lower()
    if tier in QUALITY_ORDER:
        return tier
    # Tolerate the labels the GUI shows in its dropdowns.
    aliases = {
        "hi-res": "hifi",
        "hires": "hifi",
        "hi res": "hifi",
        "master": "hifi",
        "max": "hifi",
        "flac": "lossless",
        "cd": "lossless",
        "dolby atmos": "atmos",
    }
    return aliases.get(tier, default)


def is_at_least(tier: Optional[str], minimum: str) -> bool:
    """True when ``tier`` is at least as good as ``minimum``."""
    try:
        return QUALITY_ORDER.index(normalize_tier(tier)) >= QUALITY_ORDER.index(
            normalize_tier(minimum)
        )
    except ValueError:
        return False


def is_lossless(tier: Optional[str]) -> bool:
    return normalize_tier(tier) in LOSSLESS_TIERS


def label_for(tier: Optional[str]) -> str:
    return QUALITY_LABELS.get(normalize_tier(tier), normalize_tier(tier))


def build_search_result_data(
    quality: Optional[str] = None,
    media_type: str = "track",
    base: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Build the ``search_result_data`` dict that pins a download's quality.

    Returns ``None`` when there is nothing to override, so callers can pass the
    result straight through to ``_start_single_download`` without a branch.
    """
    tier = normalize_tier(quality, default="")
    if not tier and not base:
        return None

    data: Dict[str, Any] = dict(base or {})
    if media_type:
        data.setdefault("type", media_type)
    if tier:
        extra = dict(data.get("extra_kwargs") or {})
        extra["download_quality_override"] = tier
        data["extra_kwargs"] = extra
    return data


def effective_tier_for_platform(tier: Optional[str], platform: str) -> str:
    """Downgrade a tier the platform cannot actually deliver.

    Mirrors the fallbacks gui.py applies at download time, so the queue can show
    the user the tier they will really get instead of an optimistic one.
    Spotify streams through Librespot (Ogg Vorbis only), so lossless tiers are
    meaningless there.
    """
    tier = normalize_tier(tier)
    if (platform or "").strip().lower() == "spotify" and tier in LOSSLESS_TIERS:
        return "high"
    return tier
