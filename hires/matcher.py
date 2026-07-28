"""Spotify -> TIDAL track matching.

Strategy is "ISRC first, fuzzy second, human third":

* an ISRC hit is treated as exact -- it is the same recording by definition
* everything else is scored (title / artists / duration) and only auto-accepted
  when the score is clearly good
* anything in between lands in ``NEEDS_REVIEW`` so the user decides

The single most important job of this module is *not* finding matches, it is
refusing wrong ones. A live take, a remix or a karaoke version has the same
title, the same artist and a similar duration as the studio original, so the
plain similarity score cannot tell them apart. :func:`extract_version_tags`
pulls those markers out of the title and :func:`score_candidate` penalises a
mismatch hard enough to drop the candidate out of the auto-accept range.

Everything above :class:`TrackMatcher` is pure and side-effect free. The class
itself only talks to an injected ``search_provider`` (duck typed - the real one
is :class:`hires.tidal_library.TidalLibrary`), so tests can drive it with a
fake and nothing here ever touches the network.

Known limitation: two different live recordings of the same song ("Live at
Wembley" vs "Live in Paris") only differ in the words around the version
marker. Those words are kept in the normalised title, so such a pair scores
into review territory instead of being auto-accepted.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .models import (
    MatchCandidate,
    MatchDecision,
    MatchMethod,
    MatchResult,
    TrackRef,
)

ProgressCallback = Callable[[int, int, Optional[MatchResult]], None]

# -- scoring weights (must add up to 1.0) -----------------------------------
W_TITLE = 0.45
W_ARTIST = 0.35
W_DURATION = 0.20

#: Returned when two values cannot be compared at all (missing data).
NEUTRAL_SCORE = 0.5

#: Duration differences up to this are considered identical (encoder padding).
DURATION_TOLERANCE = 3
#: From this difference on a duration carries no credit at all.
DURATION_LIMIT = 15

#: A different *recording* (live/remix/...) - must push a candidate out of the
#: auto-accept range even when title, artist and duration all agree.
STRONG_TAG_PENALTY = 0.40
#: A different *master* of the same recording (remaster, mono/stereo, ...).
#: Usually a perfectly fine substitute, so only a nudge.
WEAK_TAG_PENALTY = 0.08
#: Both sides carry the same marker - mild confirmation.
TAG_MATCH_BONUS = 0.05

#: How many results to ask the provider for per search variant.
SEARCH_LIMIT = 10


# ---------------------------------------------------------------------------
# Text folding
# ---------------------------------------------------------------------------

def _char_map() -> Dict[int, str]:
    """Typographic characters -> their ASCII equivalent."""
    mapping: Dict[int, str] = {}
    for char in "‘’‚‛′´`ʼ":
        mapping[ord(char)] = "'"
    for char in "“”„‟″«»":
        mapping[ord(char)] = '"'
    for char in "‐‑‒–—―−­":
        mapping[ord(char)] = "-"
    for char in "    ":
        mapping[ord(char)] = " "
    return mapping


_CHAR_MAP = _char_map()

_BRACKET_RE = re.compile(r"[\(\[\{]([^)\]\}]*)[\)\]\}]")
_DASH_SPLIT_RE = re.compile(r"\s+-\s+")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# "Song feat. X" -- only applied to the base title, version segments are
# already split off at that point so nothing else gets eaten.
_FEAT_INLINE_RE = re.compile(r"\s*\b(?:featuring|feat|ft)\b\.?\s.*$")
# "(feat. X)" / "(with X)" as a whole bracket segment.
_FEAT_SEGMENT_RE = re.compile(r"^\s*\b(?:featuring|feat|ft|with|w)\b\.?\s")
_FEAT_NAMES_RE = re.compile(r"\b(?:featuring|feat|ft)\b\.?\s+([^)\]\}]+)")
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|/|\+|\band\b|\bx\b|\bvs\b\.?|\bwith\b)\s*")

#: Words that carry no identity, only packaging.
_NOISE_WORD_RE = re.compile(
    r"\b(?:official|music|lyrics?|audio|video|visualizer|hd|hq|4k|"
    r"explicit|clean|bonus|track|single|album|original|deluxe|"
    r"anniversary|edition|taylors)\b"
)


def _fold(value: Any) -> str:
    """Lowercase, strip accents and unify typographic punctuation."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.translate(_CHAR_MAP).lower().strip()


def _clean(text: str) -> str:
    """Drop punctuation and collapse whitespace on already folded text.

    ``\\w`` is deliberately used instead of ``a-z`` so Cyrillic/CJK titles keep
    their characters instead of collapsing to an empty string.
    """
    text = text.replace("&", " and ").replace("'", "")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"[\s_]+", " ", text).strip()


def _split_version_segments(text: str) -> Tuple[str, List[str]]:
    """Split folded text into ``(base, segments)``.

    Segments are the bracketed groups plus everything behind a " - " suffix,
    i.e. exactly the places where both Spotify and TIDAL park version markers.
    """
    segments: List[str] = []

    def _capture(match: "re.Match[str]") -> str:
        segments.append(match.group(1).strip())
        return " "

    base = _BRACKET_RE.sub(_capture, text)
    parts = _DASH_SPLIT_RE.split(base)
    segments.extend(p.strip() for p in parts[1:])
    return parts[0], [s for s in segments if s]


# ---------------------------------------------------------------------------
# Version markers
# ---------------------------------------------------------------------------

def _compile_patterns(pairs: Sequence[Tuple[str, str]]) -> Tuple[Tuple[str, "re.Pattern[str]"], ...]:
    return tuple((tag, re.compile(pattern)) for tag, pattern in pairs)


#: Marker -> pattern. Order is irrelevant, generics are filtered afterwards.
_VERSION_PATTERNS = _compile_patterns((
    ("radio edit", r"\bradio\s+(?:edit|mix|version)\b"),
    ("sped up", r"\bsped\s*-?\s*up\b|\bspeed\s+up\b"),
    ("slowed", r"\bslowed\b"),
    ("remaster", r"\bre-?master(?:ed|ing)?\b"),
    ("remix", r"\bre-?mix(?:e[sd])?\b"),
    ("live", r"\blive\b"),
    ("acoustic", r"\bacoustic\b|\bunplugged\b"),
    ("instrumental", r"\binstrumental\b"),
    ("karaoke", r"\bkaraoke\b"),
    ("demo", r"\bdemo\b"),
    ("cover", r"\bcover\b|\btribute\b"),
    ("reprise", r"\breprise\b"),
    ("extended", r"\bextended\b"),
    ("mono", r"\bmono\b"),
    ("stereo", r"\bstereo\b"),
    # Generic leftovers - only kept when nothing more specific was found, so
    # "(Acoustic Version)" and "(Acoustic)" do not look like different takes.
    ("edit", r"\bedit\b"),
    ("mix", r"\bmix\b"),
    ("version", r"\bversion\b|\bver\b\."),
))

_GENERIC_TAGS = frozenset({"edit", "mix", "version"})

#: A mismatch here means a different recording, not a different master.
_STRONG_TAGS = frozenset({
    "live", "remix", "acoustic", "instrumental", "karaoke", "demo", "cover",
    "reprise", "radio edit", "extended", "sped up", "slowed",
})


def _segment_tags(segment: str) -> Set[str]:
    return {tag for tag, pattern in _VERSION_PATTERNS if pattern.search(segment)}


def _drop_generics(tags: Set[str]) -> Set[str]:
    return tags - _GENERIC_TAGS if tags - _GENERIC_TAGS else tags


def _segment_residue(segment: str) -> str:
    """What is left of a version segment once the markers are removed.

    ``"live at wembley"`` -> ``"at wembley"``: the marker becomes a tag, the
    rest stays part of the title so two *different* live takes still differ.
    """
    if _FEAT_SEGMENT_RE.match(segment):
        return ""
    text = segment
    for _, pattern in _VERSION_PATTERNS:
        text = pattern.sub(" ", text)
    text = _YEAR_RE.sub(" ", text)
    text = _NOISE_WORD_RE.sub(" ", text)
    return _clean(text)


def extract_version_tags(title: str) -> Set[str]:
    """Version markers found in the bracket/suffix parts of ``title``.

    Only those parts are inspected on purpose: "Live and Let Die" is not a live
    recording, "Let Die (Live)" is.

    Remaster years are recognised but not returned - a 2011 remaster and a 2009
    remaster are still both remasters.
    """
    _, segments = _split_version_segments(_fold(title))
    tags: Set[str] = set()
    for segment in segments:
        tags |= _segment_tags(segment)
    return _drop_generics(tags)


def extract_featured_artists(title: str) -> List[str]:
    """Guest artists hidden in a title ("Song (feat. X & Y)" -> [x, y]).

    Spotify lists features in ``artists[]``, TIDAL frequently only puts them in
    the title, so both sides have to be brought to the same shape before they
    can be compared.
    """
    out: List[str] = []
    seen = set()
    for match in _FEAT_NAMES_RE.finditer(_fold(title)):
        for part in _ARTIST_SPLIT_RE.split(match.group(1)):
            name = _clean(part)
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_title(value: Any) -> str:
    """Comparable form of a track title.

    Lowercased, accent folded, ``&`` spelled out, punctuation dropped,
    featured artists and version markers removed (the markers are picked up by
    :func:`extract_version_tags` instead).
    """
    base, segments = _split_version_segments(_fold(value))
    parts = [_clean(_FEAT_INLINE_RE.sub("", base))]
    parts.extend(_segment_residue(segment) for segment in segments)
    return _clean(" ".join(p for p in parts if p))


def normalize_artist(value: Any) -> str:
    """Comparable form of an artist name ("The Beatles" -> "beatles")."""
    name = _clean(_fold(value))
    if name.startswith("the "):
        name = name[4:]
    return name


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def _tokens(value: Any) -> Set[str]:
    return set(_clean(_fold(value)).split())


def token_set_ratio(a: Any, b: Any) -> float:
    """Order-insensitive similarity of two strings in ``0..1``.

    Combines a character level ratio over the sorted token sets (catches typos
    and spelling variants) with the Jaccard index of the token sets (catches
    extra or missing words). Two empty strings score ``0.0`` - no text is no
    evidence of similarity.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    jaccard = len(ta & tb) / float(len(ta | tb))
    return round(0.6 * seq + 0.4 * jaccard, 6)


def _name_list(value: Any) -> List[str]:
    """Tolerant view on an artist list.

    Payloads are not always the ``List[str]`` the contract asks for: a single
    name sometimes arrives as a bare string, and a broken response can put
    anything at all in there. Nothing usable simply means "no artists" - this
    must not raise, because one malformed track may not abort a playlist.
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _artist_keys(names: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for name in _name_list(names):
        key = normalize_artist(name)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def artists_overlap(a_list: Any, b_list: Any) -> float:
    """How much two artist lists overlap, ``0..1``.

    Asymmetric on purpose: one side listing extra guests (Spotify) while the
    other only names the main artist (TIDAL) still scores high, whereas a
    completely different artist scores near zero. Returns the neutral
    :data:`NEUTRAL_SCORE` when one side has no artists at all.
    """
    a, b = _artist_keys(a_list), _artist_keys(b_list)
    if not a or not b:
        return NEUTRAL_SCORE
    cov_a = sum(max(token_set_ratio(x, y) for y in b) for x in a) / len(a)
    cov_b = sum(max(token_set_ratio(y, x) for x in a) for y in b) / len(b)
    return round(0.6 * max(cov_a, cov_b) + 0.4 * min(cov_a, cov_b), 6)


def _positive_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def duration_score(
    a_sec: Any,
    b_sec: Any,
    tolerance: int = DURATION_TOLERANCE,
) -> float:
    """1.0 for (near) identical runtimes, fading to 0.0 at 15 seconds apart.

    A missing duration on either side yields the neutral
    :data:`NEUTRAL_SCORE` - unknown must not look like "wrong".
    """
    a, b = _positive_int(a_sec), _positive_int(b_sec)
    if a is None or b is None:
        return NEUTRAL_SCORE
    tol = max(0, _positive_int(tolerance) or 0)
    delta = abs(a - b)
    if delta <= tol:
        return 1.0
    if delta >= DURATION_LIMIT or tol >= DURATION_LIMIT:
        return 0.0
    return round(1.0 - (delta - tol) / float(DURATION_LIMIT - tol), 6)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _track_tags(track: Any) -> Set[str]:
    """Version markers of a track, title *and* TIDAL's separate version field."""
    tags = extract_version_tags(getattr(track, "title", ""))
    extra = getattr(track, "extra", None)
    version = extra.get("version") if isinstance(extra, dict) else None
    if isinstance(version, str) and version.strip():
        tags |= _segment_tags(_fold(version))
    return _drop_generics(tags)


def _effective_artists(track: Any) -> List[str]:
    artists = _name_list(getattr(track, "artists", None))
    return artists + extract_featured_artists(getattr(track, "title", ""))


def _format_tags(tags: Set[str]) -> str:
    return ", ".join(sorted(tags)) if tags else "none"


def _title_reason(value: float) -> str:
    if value >= 0.9:
        return "titles match"
    if value >= 0.7:
        return f"titles similar ({value:.2f})"
    return f"titles differ ({value:.2f})"


def _artist_reason(value: float, a: List[str], b: List[str]) -> str:
    if not a or not b:
        return "artist missing on one side"
    if value >= 0.9:
        return "same artist"
    if value >= 0.6:
        return f"artist partially matches ({value:.2f})"
    return f"different artist ({value:.2f})"


def _duration_reason(a_sec: Any, b_sec: Any) -> str:
    a, b = _positive_int(a_sec), _positive_int(b_sec)
    if a is None or b is None:
        return "duration unknown"
    delta = abs(a - b)
    if delta <= DURATION_TOLERANCE:
        return f"duration matches ({delta}s apart)"
    return f"duration off by {delta}s"


def score_candidate(source: TrackRef, candidate: TrackRef) -> Tuple[float, List[str]]:
    """Score ``candidate`` against ``source`` and explain the result.

    Returns ``(score, reasons)`` with the score clamped to ``0..1``. The
    reasons are short English phrases shown verbatim in the review dialog.
    """
    title = token_set_ratio(
        normalize_title(getattr(source, "title", "")),
        normalize_title(getattr(candidate, "title", "")),
    )
    source_artists = _effective_artists(source)
    candidate_artists = _effective_artists(candidate)
    artist = artists_overlap(source_artists, candidate_artists)
    source_duration = getattr(source, "duration_sec", None)
    candidate_duration = getattr(candidate, "duration_sec", None)
    duration = duration_score(source_duration, candidate_duration)

    score = W_TITLE * title + W_ARTIST * artist + W_DURATION * duration
    reasons = [
        _title_reason(title),
        _artist_reason(artist, _artist_keys(source_artists), _artist_keys(candidate_artists)),
        _duration_reason(source_duration, candidate_duration),
    ]

    source_tags = _track_tags(source)
    candidate_tags = _track_tags(candidate)
    if source_tags != candidate_tags:
        summary = f"source [{_format_tags(source_tags)}] vs candidate [{_format_tags(candidate_tags)}]"
        if (source_tags ^ candidate_tags) & _STRONG_TAGS:
            score -= STRONG_TAG_PENALTY
            reasons.append(f"different version: {summary}")
        else:
            score -= WEAK_TAG_PENALTY
            reasons.append(f"minor version difference: {summary}")
    elif source_tags:
        score += TAG_MATCH_BONUS
        reasons.append(f"same version [{_format_tags(source_tags)}]")

    return round(max(0.0, min(1.0, score)), 6), reasons


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

class TrackMatcher:
    """Finds the TIDAL counterpart of a Spotify track.

    ``search_provider`` is any object with ``find_by_isrc(isrc)`` and
    ``search_tracks(query, limit)`` returning :class:`TrackRef` lists - i.e.
    the surface of :class:`hires.tidal_library.TidalLibrary`, duck typed so
    this module stays free of imports it cannot test.

    Neither :meth:`match` nor :meth:`match_many` raises: a provider blowing up
    turns into a ``NO_MATCH`` result carrying the error in ``note``, because
    one broken track must not abort a 200 track playlist.
    """

    def __init__(
        self,
        search_provider: Any,
        auto_accept_threshold: float = 0.88,
        review_threshold: float = 0.62,
        max_alternatives: int = 4,
    ):
        self.search_provider = search_provider
        self.auto_accept_threshold = float(auto_accept_threshold)
        self.review_threshold = float(review_threshold)
        self.max_alternatives = max(0, int(max_alternatives))

    # -- public -------------------------------------------------------------
    def match(self, source: TrackRef) -> MatchResult:
        """Best TIDAL track for ``source``, plus what to do with it."""
        notes: List[str] = []

        isrc = (getattr(source, "isrc", None) or "").strip()
        if isrc:
            hits, error = self._by_isrc(isrc)
            if error:
                notes.append(error)
            if hits:
                return self._isrc_result(source, hits)

        candidates, error = self._collect(source)
        if error:
            notes.append(error)
        if not candidates:
            notes.append("no TIDAL search result")
            return MatchResult(
                source=source,
                decision=MatchDecision.NO_MATCH.value,
                note="; ".join(notes),
            )

        candidates.sort(key=lambda c: c.score, reverse=True)
        best = candidates[0]
        alternatives = candidates[1:1 + self.max_alternatives]

        if best.score >= self.auto_accept_threshold:
            decision = MatchDecision.AUTO_ACCEPT.value
        elif best.score >= self.review_threshold:
            decision = MatchDecision.NEEDS_REVIEW.value
        else:
            decision = MatchDecision.NO_MATCH.value
            notes.append(
                f"best candidate scored {best.score:.2f}, "
                f"below the review threshold of {self.review_threshold:.2f}"
            )

        return MatchResult(
            source=source,
            best=best,
            alternatives=alternatives,
            decision=decision,
            note="; ".join(notes),
        )

    def match_many(
        self,
        sources: Any,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> List[MatchResult]:
        """Match a whole playlist. ``progress_callback(done, total, result)``."""
        tracks = list(sources or [])
        total = len(tracks)
        results: List[MatchResult] = []
        for index, source in enumerate(tracks, start=1):
            result = self.match(source)
            results.append(result)
            if progress_callback is not None:
                try:
                    progress_callback(index, total, result)
                except Exception:
                    # A broken progress display must never stop a conversion.
                    pass
        return results

    # -- ISRC ---------------------------------------------------------------
    def _by_isrc(self, isrc: str) -> Tuple[List[TrackRef], str]:
        finder = getattr(self.search_provider, "find_by_isrc", None)
        if not callable(finder):
            return [], ""
        try:
            hits = finder(isrc)
        except Exception as exc:
            return [], f"ISRC lookup failed: {exc}"
        return [t for t in list(hits or []) if _track_id(t)], ""

    def _isrc_result(self, source: TrackRef, hits: List[TrackRef]) -> MatchResult:
        """An ISRC can point at several releases - rank them, keep the rest."""
        ranked = sorted(hits, key=lambda t: score_candidate(source, t)[0], reverse=True)
        reasons = ["exact ISRC match"]
        if len(ranked) > 1:
            reasons.append(f"best of {len(ranked)} releases sharing this ISRC")
        best = MatchCandidate(
            track=ranked[0],
            score=1.0,
            method=MatchMethod.ISRC.value,
            reasons=reasons,
        )
        alternatives = [
            MatchCandidate(
                track=track,
                score=1.0,
                method=MatchMethod.ISRC.value,
                reasons=["exact ISRC match", "other release with the same ISRC"],
            )
            for track in ranked[1:1 + self.max_alternatives]
        ]
        return MatchResult(
            source=source,
            best=best,
            alternatives=alternatives,
            decision=MatchDecision.AUTO_ACCEPT.value,
            note="",
        )

    # -- fuzzy --------------------------------------------------------------
    def _queries(self, source: TrackRef) -> List[str]:
        """Search variants, most specific first."""
        title = (getattr(source, "title", None) or "").strip()
        base = normalize_title(title)
        artist = next(iter(_name_list(getattr(source, "artists", None))), "").strip()

        # ``base`` is empty for titles made of punctuation only ("!!!", "+"):
        # fall back to the raw title so those still get searched instead of
        # being reported as "no result" without ever asking the provider.
        variants = (
            (f"{artist} {title}", f"{artist} {base}", title, base)
            if base
            else (f"{artist} {title}", title)
        )

        out: List[str] = []
        seen = set()
        for query in variants:
            query = query.strip()
            key = query.lower()
            if not query or key in seen:
                continue
            seen.add(key)
            out.append(query)
        return out

    def _collect(self, source: TrackRef) -> Tuple[List[MatchCandidate], str]:
        scored: Dict[str, MatchCandidate] = {}
        errors: List[str] = []

        for query in self._queries(source):
            try:
                results = self.search_provider.search_tracks(query, limit=SEARCH_LIMIT)
            except Exception as exc:
                errors.append(f"TIDAL search for {query!r} failed: {exc}")
                continue

            for track in list(results or []):
                track_id = _track_id(track)
                if not track_id or track_id in scored:
                    continue
                score, reasons = score_candidate(source, track)
                scored[track_id] = MatchCandidate(
                    track=track,
                    score=score,
                    method=MatchMethod.FUZZY.value,
                    reasons=reasons,
                )

            if any(c.score >= self.auto_accept_threshold for c in scored.values()):
                # Good enough - the remaining variants would only cost API calls.
                break

        return list(scored.values()), "; ".join(errors)


def _track_id(track: Any) -> str:
    return str(getattr(track, "id", "") or "").strip()
