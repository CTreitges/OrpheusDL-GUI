"""Ask GitHub whether a newer release of this app exists.

One channel only: the latest release (no drafts, no pre-releases) of
``CTreitges/OrpheusDL-GUI``. GitHub's ``releases/latest`` endpoint already
leaves pre-releases out; :func:`parse_release` checks again.

What comes back is only trusted as far as it can be checked:

* the tag must be one of ours (``v2.0.3-hires.N``) and newer than this build;
* the download must sit under this repository's release URL, over HTTPS;
* the asset must carry a SHA-256 ``digest`` -- without it there is nothing to
  verify the download against, so the release is offered as a page to open,
  never as an automatic install.

Network trouble of any kind (offline, 403 rate limit, 404, garbage) ends the
check quietly with a log line; the app keeps running as before.

For tests a local server can stand in for GitHub (environment variable
``ORPHEUSDL_UPDATE_TEST_SERVER``), but only on the loopback interface, and then
only downloads below that same server are accepted -- the switch cannot point
the app at another machine.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .app_version import is_newer, parse_tag

REPO = "CTreitges/OrpheusDL-GUI"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
ASSET_PREFIX = f"https://github.com/{REPO}/releases/download/"
WINDOWS_ASSET = "OrpheusDL-GUI-windows.zip"

#: Hosts a download may be redirected to. GitHub hands release assets out from
#: one of its storage hosts after a redirect from github.com.
GITHUB_HOSTS = frozenset(
    {
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)

TEST_SERVER_ENV = "ORPHEUSDL_UPDATE_TEST_SERVER"
_LOOPBACK = frozenset({"127.0.0.1", "localhost"})

#: Check on start at most this often.
CHECK_INTERVAL_SEC = 24 * 60 * 60

USER_AGENT = "OrpheusDL-GUI-updater"
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
MAX_REDIRECTS = 5


class UpdateCheckError(Exception):
    """The release information could not be read. The message is safe to log."""


@dataclass(frozen=True)
class Channel:
    """Where releases come from and which hosts a download may touch."""

    api_url: str
    asset_prefix: str
    page_url: str
    hosts: frozenset
    https_only: bool = True
    is_test: bool = False


GITHUB = Channel(API_URL, ASSET_PREFIX, RELEASES_PAGE, GITHUB_HOSTS)


def channel_from_env(env: Optional[Mapping[str, str]] = None) -> Channel:
    """The GitHub channel, or a loopback test server if one is configured.

    Anything in the variable that is not ``http://127.0.0.1:<port>`` or
    ``http://localhost:<port>`` is ignored.
    """
    env = os.environ if env is None else env
    raw = (env.get(TEST_SERVER_ENV) or "").strip().rstrip("/")
    if not raw:
        return GITHUB
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK or parts.path or parts.query:
        return GITHUB
    if parts.username or parts.password:
        return GITHUB
    base = f"http://{parts.netloc}"
    return Channel(
        api_url=f"{base}/repos/{REPO}/releases/latest",
        asset_prefix=f"{base}/{REPO}/releases/download/",
        page_url=f"{base}/{REPO}/releases",
        hosts=frozenset({parts.hostname}),
        https_only=False,
        is_test=True,
    )


def url_allowed(url: str, channel: Channel) -> bool:
    """May the app open ``url`` on this channel?"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if channel.https_only and parts.scheme != "https":
        return False
    if parts.scheme not in ("https", "http"):
        return False
    if parts.username or parts.password:
        return False
    return (parts.hostname or "") in channel.hosts


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to hosts of the channel, and only a few."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, channel: Channel):
        super().__init__()
        self._channel = channel

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if not url_allowed(target, self._channel):
            host = urllib.parse.urlsplit(target).hostname or "?"
            raise urllib.error.HTTPError(
                req.full_url, code, f"redirect to a host that is not allowed ({host})", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_opener(channel: Channel) -> urllib.request.OpenerDirector:
    """An opener that refuses redirects off the channel's hosts."""
    return urllib.request.build_opener(_GuardedRedirects(channel))


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    name: str
    notes: str
    page_url: str
    asset_name: str = ""
    asset_url: str = ""
    size: int = 0
    sha256: str = ""

    @property
    def installable(self) -> bool:
        """True when the app may download and install this release itself."""
        return bool(self.asset_url and self.sha256 and self.size > 0)


def parse_release(
    data: Any, channel: Channel = GITHUB, asset_name: str = WINDOWS_ASSET
) -> Optional[ReleaseInfo]:
    """Turn an API answer into a :class:`ReleaseInfo`, or ``None``.

    ``None`` means "not a release we would offer": not a dict, a draft or
    pre-release, or a tag that is not ours. A release whose asset fails a
    check is still returned, just without the asset, so it can be offered as a
    page to open.
    """
    if not isinstance(data, dict):
        return None
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    if parse_tag(tag) is None:
        return None
    page = data.get("html_url")
    if not (isinstance(page, str) and page.startswith(channel.page_url + "/")):
        page = channel.page_url
    info = dict(
        tag=tag,
        name=str(data.get("name") or tag),
        notes=str(data.get("body") or "")[:4000],
        page_url=page,
    )
    for asset in data.get("assets") or ():
        if not isinstance(asset, dict) or asset.get("name") != asset_name:
            continue
        url = asset.get("browser_download_url")
        digest = _DIGEST.match(str(asset.get("digest") or "").lower())
        size = asset.get("size")
        if (
            isinstance(url, str)
            and url == f"{channel.asset_prefix}{tag}/{asset_name}"
            and url_allowed(url, channel)
            and digest
            and isinstance(size, int)
            and size > 0
        ):
            info.update(asset_name=asset_name, asset_url=url, size=size, sha256=digest.group(1))
        break
    return ReleaseInfo(**info)


def fetch_latest(
    channel: Channel = GITHUB,
    *,
    opener: Optional[urllib.request.OpenerDirector] = None,
    timeout: float = 10.0,
) -> Any:
    """The JSON of ``releases/latest``. Raises :class:`UpdateCheckError`."""
    opener = opener or make_opener(channel)
    request = urllib.request.Request(
        channel.api_url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        raise UpdateCheckError(f"GitHub answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateCheckError(f"no connection ({type(reason).__name__})") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateCheckError("unreadable answer from GitHub") from None


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check. ``status`` is one of the constants below."""

    status: str
    release: Optional[ReleaseInfo] = None
    message: str = ""


DEV_BUILD = "dev_build"
UP_TO_DATE = "up_to_date"
AVAILABLE = "available"
FAILED = "failed"


def check(
    current: Optional[str],
    *,
    channel: Optional[Channel] = None,
    fetch: Optional[Callable[[Channel], Any]] = None,
) -> CheckResult:
    """Compare this build against the latest release. Never raises."""
    if parse_tag(current) is None:
        return CheckResult(DEV_BUILD, message="Development build: no update check.")
    channel = channel or channel_from_env()
    fetch = fetch or (lambda ch: fetch_latest(ch))
    try:
        data = fetch(channel)
    except UpdateCheckError as exc:
        return CheckResult(FAILED, message=f"Update check failed: {exc}.")
    except Exception as exc:  # never let a check take the app down
        return CheckResult(FAILED, message=f"Update check failed ({type(exc).__name__}).")
    release = parse_release(data, channel)
    if release is None:
        return CheckResult(FAILED, message="Update check: no usable release found.")
    if not is_newer(release.tag, current):
        return CheckResult(UP_TO_DATE, release, message=f"Up to date ({current}).")
    return CheckResult(AVAILABLE, release, message=f"Version {release.tag} is available.")


# -- once a day ---------------------------------------------------------------

def load_state(path: str) -> dict:
    """The updater's small state file; ``{}`` when absent or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: str, state: dict) -> None:
    """Write the state atomically. Failures are ignored: it is only a cache."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def check_due(state: Mapping[str, Any], now: Optional[float] = None) -> bool:
    """Has a day passed since the last automatic check?"""
    now = time.time() if now is None else now
    last = state.get("last_check")
    if not isinstance(last, (int, float)) or last > now:
        return True  # never checked, or the clock went backwards
    return now - last >= CHECK_INTERVAL_SEC
