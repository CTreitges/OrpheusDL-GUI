"""Account status for SoundCloud and YouTube, plus what each service needs.

TIDAL and Spotify have real sign-in flows (see :mod:`hires.integration`).
SoundCloud and YouTube do not: SoundCloud runs on an OAuth token copied from
the browser, YouTube on an exported ``cookies.txt``. Their cards therefore
report whether that credential is present and usable, and the button opens
the settings page where it is entered.

Secrets never leave this module. The SoundCloud token is only ever handed to
the SoundCloud module's own API client; the YouTube cookie file is read for
names and expiry dates, its values are skipped on the spot. Nothing returned
from here -- detail, account, hint -- can contain either.
"""

from __future__ import annotations

import datetime
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .models import AccountState, AccountStatus

SOUNDCLOUD_MODULE_NAME = "soundcloud"
YOUTUBE_DEFAULT_COOKIES = "./config/youtube-cookies.txt"

#: Cookies that only exist for a signed-in Google/YouTube session.
YOUTUBE_LOGIN_COOKIES = frozenset(
    {"LOGIN_INFO", "SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID", "__Secure-3PAPISID"}
)

#: A cookie export older than this gets a hint to export a fresh one.
YOUTUBE_STALE_DAYS = 30

#: What each service needs, shown on its card under the status.
SERVICE_INFO: Dict[str, str] = {
    "TIDAL": (
        "Signs in through TIDAL's own device page in your browser. Needed for Hi-Res and "
        "lossless FLAC; without it TIDAL only browses as a guest."
    ),
    "Spotify": (
        "Reads your playlists through your own Spotify developer app (Client ID and Secret "
        "under Settings > Spotify). Public playlist links work without signing in."
    ),
    "SoundCloud": (
        "Uses the OAuth token of your browser session, entered under Settings > SoundCloud. "
        "With Go+ you get full-length tracks in AAC 256 kbit/s; without Go+ you get the free "
        "streams (often 128 kbit/s) and only 30-second previews of Go+ tracks."
    ),
    "YouTube": (
        "Uses a cookies.txt exported from a browser where you are signed in to YouTube "
        "(Settings > YouTube). Public videos work without it; age-restricted and members-only "
        "videos need it. YouTube audio is lossy (Opus up to about 160 kbit/s)."
    ),
}

#: Services whose card button opens the settings instead of a sign-in flow.
SETTINGS_ONLY_SERVICES = frozenset({"SoundCloud", "YouTube"})


def _credentials(gui: Any, service: str) -> dict:
    settings = getattr(gui, "current_settings", None)
    if not isinstance(settings, dict):
        return {}
    creds = (settings.get("credentials") or {}).get(service) or {}
    return creds if isinstance(creds, dict) else {}


def loaded_module(gui: Any, name: str) -> Any:
    """A loaded OrpheusDL module interface, loading it on demand; never raises."""
    orpheus = getattr(gui, "orpheus_instance", None)
    if orpheus is None:
        return None
    loaded = getattr(orpheus, "loaded_modules", None)
    module = loaded.get(name) if isinstance(loaded, dict) else None
    if module is not None:
        return module
    known = getattr(orpheus, "module_list", None) or ()
    loader = getattr(orpheus, "load_module", None)
    try:
        available = name in known
    except TypeError:
        available = False
    if not available or not callable(loader):
        return None
    try:
        return loader(name)
    except Exception:
        return None


def module_installed(gui: Any, name: str) -> Optional[bool]:
    """True/False when the GUI knows its modules, ``None`` while it is starting."""
    orpheus = getattr(gui, "orpheus_instance", None)
    if orpheus is None:
        return None
    known = getattr(orpheus, "module_list", None)
    if known is None:
        return None
    try:
        return name in known
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

def soundcloud_plan(me: dict) -> Tuple[bool, str]:
    """``(has Go+, readable plan name)`` from SoundCloud's ``/me`` answer.

    Same reading as the SoundCloud module itself (subscriptions first, then
    the quota flags), so the card and the downloads agree. "Go" without the
    plus is the ad-free tier and does *not* unlock the 256 kbit/s streams.
    """
    me = me or {}
    product = ""
    subscriptions = me.get("consumer_subscriptions") or me.get("subscriptions") or []
    subscription = me.get("consumer_subscription")
    if subscriptions and isinstance(subscriptions, list) and isinstance(subscriptions[0], dict):
        product = str((subscriptions[0].get("product") or {}).get("id") or "premium")
    elif isinstance(subscription, dict):
        product = str((subscription.get("product") or {}).get("id") or "premium")
    quota = me.get("quota") or {}
    if not product and isinstance(quota, dict) and (quota.get("high_tier") or quota.get("top_tier")):
        product = "high_tier"
    if not product and me.get("plan") and str(me.get("plan")).lower() != "free":
        product = str(me.get("plan"))

    lowered = product.lower()
    if not lowered:
        return False, "Free"
    if "high" in lowered or "plus" in lowered or "go+" in lowered or "top" in lowered:
        return True, "Go+"
    if "mid" in lowered or lowered in ("go", "consumer-go"):
        return False, "Go (without Go+)"
    return False, product


def _module_me_fetcher(gui: Any) -> Optional[Callable[[str], dict]]:
    """``fetch(token) -> /me`` through the SoundCloud module's own API client.

    A fresh client per call, built from the *current* token: the module keeps
    the token it was loaded with, so a token changed in the settings would
    otherwise be checked as the old one until the next restart.
    """
    module = loaded_module(gui, SOUNDCLOUD_MODULE_NAME)
    session = getattr(module, "websession", None) if module is not None else None
    if session is None:
        return None
    api_class = type(session)
    error_class = getattr(module, "exception", Exception)

    def fetch(token: str) -> dict:
        return api_class(token, error_class).get_me()

    return fetch


def make_soundcloud_status_provider(
    gui: Any, me_fetcher: Optional[Callable[[str], dict]] = None
) -> Callable[[], AccountStatus]:
    """SoundCloud's state: module, token present, token accepted, Go+ or not."""

    def status(state, detail="", account="", hint=""):
        return AccountStatus(service="SoundCloud", state=state, detail=detail, account=account, hint=hint)

    def provider() -> AccountStatus:
        installed = module_installed(gui, SOUNDCLOUD_MODULE_NAME)
        if installed is False:
            return status(AccountState.UNAVAILABLE, "SoundCloud module not installed.")
        token = str(_credentials(gui, "SoundCloud").get("web_access_token") or "").strip()
        if not token:
            return status(
                AccountState.SIGNED_OUT,
                "No token set — free streams only (often 128 kbit/s), Go+ tracks as 30-second previews.",
                hint="Enter your OAuth token under Settings > SoundCloud.",
            )
        fetch = me_fetcher or _module_me_fetcher(gui)
        if fetch is None:
            return status(
                AccountState.SIGNED_OUT,
                "Token set, but the SoundCloud module has not loaded yet.",
                hint="This is checked again automatically.",
            )
        try:
            me = fetch(token) or {}
        except Exception as exc:
            # The exception text may quote the request; never show it. Only
            # decide from it whether the token itself was refused.
            text = str(exc).lower()
            if "401" in text or "unauthorized" in text:
                return status(
                    AccountState.SIGNED_OUT,
                    "SoundCloud rejected the token — it has probably expired.",
                    hint="Copy a fresh token from your browser into Settings > SoundCloud.",
                )
            return status(
                AccountState.SIGNED_OUT,
                f"Token set, but SoundCloud could not be checked ({type(exc).__name__}) — offline?",
                hint="This is checked again automatically.",
            )
        go_plus, plan = soundcloud_plan(me)
        username = str(me.get("username") or "").strip()
        if go_plus:
            detail = "Go+ active — full-length tracks in AAC 256 kbit/s."
        else:
            detail = f"Plan: {plan} — free streams only (often 128 kbit/s), Go+ tracks as previews."
        return status(AccountState.SIGNED_IN, detail, account=f"{username} ({plan})" if username else plan)

    return provider


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def resolve_cookie_path(gui: Any, raw: str) -> str:
    """Where a configured cookie path points, the way gui.py looks for it."""
    raw = (raw or "").strip() or YOUTUBE_DEFAULT_COOKIES
    if os.path.isabs(raw):
        return raw
    base = getattr(gui, "application_path", None) or os.getcwd()
    candidates = [os.path.join(base, raw)]
    if not raw.startswith(("./", ".\\")):
        candidates.append(os.path.join(base, "config", raw))
    for candidate in candidates:
        if os.path.exists(candidate):
            return os.path.normpath(candidate)
    return os.path.normpath(candidates[0])


def read_cookie_summary(path: str) -> dict:
    """Names and expiry of YouTube/Google cookies in a Netscape cookie file.

    Values are never kept: each line is split, the value column is dropped
    at once. Returns ``{"cookies", "login_cookies", "login_expiry"}`` where
    ``login_expiry`` is the latest expiry of a login cookie (0 = session).
    """
    cookies = 0
    login = 0
    expiry = 0.0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, name = parts[0].lower(), parts[5]
            if not (domain.endswith("youtube.com") or domain.endswith("google.com")):
                continue
            cookies += 1
            if name in YOUTUBE_LOGIN_COOKIES:
                login += 1
                try:
                    expiry = max(expiry, float(parts[4] or 0))
                except ValueError:
                    pass
    return {"cookies": cookies, "login_cookies": login, "login_expiry": expiry}


def _date(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp).strftime("%d %b %Y")


def make_youtube_status_provider(
    gui: Any, now: Callable[[], float] = time.time
) -> Callable[[], AccountStatus]:
    """YouTube's state from the cookie file: present, signed in, how old."""

    def status(state, detail="", account="", hint=""):
        return AccountStatus(service="YouTube", state=state, detail=detail, account=account, hint=hint)

    def provider() -> AccountStatus:
        if module_installed(gui, "youtube") is False:
            return status(AccountState.UNAVAILABLE, "YouTube module not installed.")
        raw = str(_credentials(gui, "YouTube").get("cookies_path") or "")
        path = resolve_cookie_path(gui, raw)
        shown = os.path.basename(path)
        if not os.path.isfile(path):
            return status(
                AccountState.SIGNED_OUT,
                f"No cookie file ({shown}) — public videos still work; age-restricted and members-only need it.",
                hint="Export cookies.txt from a signed-in browser and set it under Settings > YouTube.",
            )
        try:
            summary = read_cookie_summary(path)
            modified = os.path.getmtime(path)
        except OSError as exc:
            return status(AccountState.SIGNED_OUT, f"Cookie file {shown} cannot be read ({type(exc).__name__}).")

        age_days = max(0, int((now() - modified) // 86400))
        age = f"exported {_date(modified)} ({age_days} day{'s' if age_days != 1 else ''} ago)"
        if summary["login_cookies"] == 0:
            return status(
                AccountState.SIGNED_OUT,
                f"Cookie file {age}, but it holds no YouTube login.",
                hint="Export it again while signed in to youtube.com.",
            )
        expiry = summary["login_expiry"]
        if expiry and expiry < now():
            return status(
                AccountState.SIGNED_OUT,
                f"Cookie file {age}; its login expired on {_date(expiry)}.",
                hint="Export a fresh cookies.txt.",
            )
        detail = f"Cookie file {age} with a YouTube login. Not checked online."
        hint = "Export a fresh one if downloads start asking you to sign in." if age_days > YOUTUBE_STALE_DAYS else ""
        return status(AccountState.SIGNED_IN, detail, hint=hint)

    return provider


# ---------------------------------------------------------------------------
# Settings shortcut
# ---------------------------------------------------------------------------

def make_open_settings(gui: Any) -> Callable[[str], bool]:
    """Switch the main window to Settings > ``service``. False when impossible."""

    def open_settings(service: str) -> bool:
        tabview = getattr(gui, "tabview", None)
        settings_tabview = getattr(gui, "settings_tabview", None)
        try:
            if tabview is not None:
                tabview.set("Settings")
            if settings_tabview is not None:
                settings_tabview.set(service)
                handler = getattr(gui, "_handle_settings_tab_change", None)
                if callable(handler):
                    handler()
            return tabview is not None
        except Exception:
            return False

    return open_settings
