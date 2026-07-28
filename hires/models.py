"""Shared data model for the hi-res suite.

This module is the contract every other ``hires`` module implements against.
It has no dependency on tkinter, requests, or OrpheusDL itself so it stays
trivially importable from tests.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

class QueueStatus(str, Enum):
    """Lifecycle of a single queue entry."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        return self in (QueueStatus.DONE, QueueStatus.FAILED, QueueStatus.CANCELLED)


class MediaKind(str, Enum):
    TRACK = "track"
    ALBUM = "album"
    PLAYLIST = "playlist"
    ARTIST = "artist"


@dataclass
class QueueItem:
    """One unit of work for the download queue.

    ``url`` is what actually gets handed to OrpheusDL; everything else is
    display metadata or bookkeeping.
    """

    url: str
    title: str = ""
    subtitle: str = ""
    platform: str = ""
    media_kind: str = MediaKind.TRACK.value
    output_path: Optional[str] = None
    quality: Optional[str] = None
    status: str = QueueStatus.PENDING.value
    attempts: int = 0
    max_attempts: int = 3
    error: Optional[str] = None
    group_id: Optional[str] = None
    group_label: str = ""
    source: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    added_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueItem":
        """Tolerant loader: unknown keys are dropped, missing keys defaulted.

        Queue files are written by older/newer versions of the app, so this
        must never raise on a field mismatch.
        """
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in (data or {}).items() if k in known}
        if not clean.get("url"):
            raise ValueError("QueueItem requires a url")
        return cls(**clean)

    # -- convenience --------------------------------------------------------
    @property
    def is_finished(self) -> bool:
        return QueueStatus(self.status).is_finished

    @property
    def can_retry(self) -> bool:
        return self.status == QueueStatus.FAILED.value and self.attempts < self.max_attempts

    @property
    def display_name(self) -> str:
        if self.title and self.subtitle:
            return f"{self.subtitle} - {self.title}"
        return self.title or self.url


@dataclass
class QueueStats:
    total: int = 0
    pending: int = 0
    active: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0

    @property
    def remaining(self) -> int:
        return self.pending + self.active


# ---------------------------------------------------------------------------
# Source-agnostic music entities
# ---------------------------------------------------------------------------

@dataclass
class TrackRef:
    """A track as seen on *some* service. Used for both Spotify and TIDAL."""

    id: str
    title: str = ""
    artists: List[str] = field(default_factory=list)
    album: str = ""
    duration_sec: Optional[int] = None
    isrc: Optional[str] = None
    explicit: bool = False
    url: str = ""
    image_url: str = ""
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    platform: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def artist_string(self) -> str:
        return ", ".join(a for a in self.artists if a)

    @property
    def display_name(self) -> str:
        return f"{self.artist_string} - {self.title}".strip(" -")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrackRef":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class PlaylistRef:
    """A playlist as seen on some service."""

    id: str
    name: str = ""
    description: str = ""
    owner: str = ""
    track_count: int = 0
    duration_sec: Optional[int] = None
    image_url: str = ""
    url: str = ""
    platform: str = ""
    kind: str = "user"  # user | favorite | editorial | liked
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlaylistRef":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

class MatchMethod(str, Enum):
    ISRC = "isrc"
    FUZZY = "fuzzy"
    MANUAL = "manual"


class MatchDecision(str, Enum):
    """What the converter intends to do with a match."""

    AUTO_ACCEPT = "auto_accept"
    NEEDS_REVIEW = "needs_review"
    NO_MATCH = "no_match"
    REJECTED = "rejected"  # user said no during review


@dataclass
class MatchCandidate:
    track: TrackRef
    score: float
    method: str = MatchMethod.FUZZY.value
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "track": self.track.to_dict(),
            "score": self.score,
            "method": self.method,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MatchCandidate":
        return cls(
            track=TrackRef.from_dict(data.get("track") or {}),
            score=float(data.get("score") or 0.0),
            method=data.get("method") or MatchMethod.FUZZY.value,
            reasons=list(data.get("reasons") or []),
        )


@dataclass
class MatchResult:
    """Outcome of matching one source track against the target service."""

    source: TrackRef
    best: Optional[MatchCandidate] = None
    alternatives: List[MatchCandidate] = field(default_factory=list)
    decision: str = MatchDecision.NO_MATCH.value
    note: str = ""

    @property
    def is_usable(self) -> bool:
        """True when this result would produce a download."""
        return self.best is not None and self.decision in (
            MatchDecision.AUTO_ACCEPT.value,
            MatchDecision.NEEDS_REVIEW.value,
        )

    def accept(self, candidate: Optional[MatchCandidate] = None) -> None:
        """Confirm a match during review (optionally picking an alternative)."""
        if candidate is not None:
            self.best = candidate
        if self.best is None:
            self.decision = MatchDecision.NO_MATCH.value
        else:
            self.decision = MatchDecision.AUTO_ACCEPT.value

    def reject(self, note: str = "") -> None:
        self.decision = MatchDecision.REJECTED.value
        if note:
            self.note = note

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "best": self.best.to_dict() if self.best else None,
            "alternatives": [c.to_dict() for c in self.alternatives],
            "decision": self.decision,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MatchResult":
        best = data.get("best")
        return cls(
            source=TrackRef.from_dict(data.get("source") or {}),
            best=MatchCandidate.from_dict(best) if best else None,
            alternatives=[MatchCandidate.from_dict(c) for c in (data.get("alternatives") or [])],
            decision=data.get("decision") or MatchDecision.NO_MATCH.value,
            note=data.get("note") or "",
        )


@dataclass
class ConversionReport:
    """Full result of converting one Spotify playlist to TIDAL."""

    playlist: PlaylistRef
    results: List[MatchResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def by_decision(self, decision: MatchDecision) -> List[MatchResult]:
        return [r for r in self.results if r.decision == decision.value]

    @property
    def auto_accepted(self) -> List[MatchResult]:
        return self.by_decision(MatchDecision.AUTO_ACCEPT)

    @property
    def needs_review(self) -> List[MatchResult]:
        return self.by_decision(MatchDecision.NEEDS_REVIEW)

    @property
    def unmatched(self) -> List[MatchResult]:
        return self.by_decision(MatchDecision.NO_MATCH)

    @property
    def counts(self) -> Dict[str, int]:
        out = {d.value: 0 for d in MatchDecision}
        for r in self.results:
            out[r.decision] = out.get(r.decision, 0) + 1
        out["total"] = len(self.results)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "playlist": self.playlist.to_dict(),
            "results": [r.to_dict() for r in self.results],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "counts": self.counts,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversionReport":
        return cls(
            playlist=PlaylistRef.from_dict(data.get("playlist") or {}),
            results=[MatchResult.from_dict(r) for r in (data.get("results") or [])],
            started_at=float(data.get("started_at") or time.time()),
            finished_at=data.get("finished_at"),
        )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class AccountState(str, Enum):
    """How far a service is from being usable.

    Four states rather than a bool, because "you have not logged in" and "you
    cannot log in until you enter a client id" need completely different words
    on screen -- and ``UNAVAILABLE`` (module not installed at all) must not be
    presented as a failed login.
    """

    #: Logged in and ready.
    SIGNED_IN = "signed_in"
    #: Everything is in place, the user simply has not signed in yet.
    SIGNED_OUT = "signed_out"
    #: Signing in is impossible until something else is configured first.
    NEEDS_SETUP = "needs_setup"
    #: The service is not installed / not loadable at all.
    UNAVAILABLE = "unavailable"


@dataclass
class AccountStatus:
    """One service's sign-in state, ready to render.

    Deliberately a snapshot with no live handles: it is produced on demand by a
    provider and thrown away after painting, so it can never go stale in place.
    """

    service: str
    state: AccountState
    #: One line under the service name explaining the state.
    detail: str = ""
    #: Account name, when the service tells us one.
    account: str = ""
    #: What to do first when ``state`` is ``NEEDS_SETUP``.
    hint: str = ""

    @property
    def is_signed_in(self) -> bool:
        return self.state is AccountState.SIGNED_IN

    @property
    def can_sign_in(self) -> bool:
        """Whether offering a sign-in button makes sense at all."""
        return self.state in (AccountState.SIGNED_OUT, AccountState.SIGNED_IN)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service,
            "state": self.state.value,
            "detail": self.detail,
            "account": self.account,
            "hint": self.hint,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HiresError(Exception):
    """Base class for all errors raised by the hi-res suite."""


class AuthRequiredError(HiresError):
    """Raised when a service needs a login the user has not performed yet."""


class SourceUnavailableError(HiresError):
    """Raised when a playlist/track cannot be fetched from a service."""
