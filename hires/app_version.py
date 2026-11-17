"""Which release this app is, and whether another release is newer.

The release build writes the tag it builds (``v2.0.3-hires.N``) into
``hires/_build_version.py`` before PyInstaller runs (see
``.github/workflows/build-release.yml``). A checkout or a build without a tag
has no such file: that is a development build, and it never checks for or
installs updates (a version it cannot compare would offer every release).

Releases are ordered by the four numbers of the tag, compared as integers --
``hires.10`` is newer than ``hires.9``, which a string comparison gets wrong.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

#: ``v2.0.3-hires.12`` -> (2, 0, 3, 12). The leading ``v`` is optional.
_TAG = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})-hires\.(\d{1,6})$")

VersionKey = Tuple[int, int, int, int]


def parse_tag(tag: object) -> Optional[VersionKey]:
    """The comparable key of a release tag, or ``None`` if it is not one of ours."""
    if not isinstance(tag, str):
        return None
    match = _TAG.match(tag.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def current_version() -> Optional[str]:
    """The tag this build was made from, or ``None`` for a development build."""
    try:
        from ._build_version import VERSION  # written by the release build only
    except Exception:
        return None
    return VERSION if parse_tag(VERSION) is not None else None


def is_newer(candidate: object, current: object) -> bool:
    """True only if both are our tags and ``candidate`` is strictly newer.

    Anything unparsable on either side means "no": a development build, a
    foreign tag or a broken API answer must never lead to an install.
    """
    new_key = parse_tag(candidate)
    old_key = parse_tag(current)
    if new_key is None or old_key is None:
        return False
    return new_key > old_key


def short_label(tag: object) -> str:
    """``hires.12`` for messages; the full text for anything else."""
    key = parse_tag(tag)
    if key is None:
        return str(tag or "development build")
    return f"hires.{key[3]}"
