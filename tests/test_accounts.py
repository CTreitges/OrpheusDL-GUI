"""Tests for the Accounts tab's controller (tkinter-free).

The point of the tab is that signing in happens *before* the first download,
so most of what matters here is refusal behaviour: what the controller does
when it cannot sign in, must not sign in, or was told to sign in twice.
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hires.controllers import (  # noqa: E402
    TIDAL_SIGN_IN_HINT,
    AccountsController,
    SpotifyImportController,
    UiDispatcher,
)
from hires.models import AccountState, AccountStatus  # noqa: E402
from hires.queue_store import QueueStore  # noqa: E402
from test_controllers import (  # noqa: E402
    AuthAwareSource,
    FakeWebBackend,
    SyncDispatcher,
    wait,
)


@pytest.fixture
def queue(tmp_path):
    return QueueStore(str(tmp_path / "queue.json"))


def account(service, state, **kwargs):
    return AccountStatus(service=service, state=state, **kwargs)


def signed_out(service="TIDAL"):
    return lambda: account(service, AccountState.SIGNED_OUT)


def signed_in(service="TIDAL"):
    return lambda: account(service, AccountState.SIGNED_IN)


def make_accounts(**kwargs):
    kwargs.setdefault("tidal_status_provider", signed_out("TIDAL"))
    kwargs.setdefault("spotify_status_provider", signed_out("Spotify"))
    kwargs.setdefault("dispatch", SyncDispatcher())
    return AccountsController(**kwargs)


def raiser(exc):
    def boom():
        raise exc

    return boom


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestAccountStatus:
    def test_both_services_are_reported(self):
        assert [s.service for s in make_accounts().statuses()] == ["TIDAL", "Spotify"]

    def test_a_broken_provider_degrades_to_unavailable(self):
        """One exploding provider must not blank the whole tab."""
        ctrl = make_accounts(tidal_status_provider=raiser(RuntimeError("module exploded")))

        status = ctrl.status_for("TIDAL")
        assert status.state is AccountState.UNAVAILABLE
        assert "exploded" in status.detail
        assert ctrl.status_for("Spotify").state is AccountState.SIGNED_OUT

    def test_status_is_reread_every_call(self):
        """Nothing may be cached: the TIDAL module loads after the tabs exist."""
        states = [
            AccountState.UNAVAILABLE,
            AccountState.SIGNED_OUT,
            AccountState.SIGNED_IN,
        ]
        ctrl = make_accounts(tidal_status_provider=lambda: account("TIDAL", states.pop(0)))

        assert ctrl.status_for("TIDAL").state is AccountState.UNAVAILABLE
        assert ctrl.status_for("TIDAL").state is AccountState.SIGNED_OUT
        assert ctrl.status_for("TIDAL").state is AccountState.SIGNED_IN

    def test_summary_names_the_signed_in_services(self):
        ctrl = make_accounts(tidal_status_provider=signed_in())
        ctrl.statuses()  # summary reports what was read, it does not read itself
        assert ctrl.summary() == "Signed in: TIDAL"

        ctrl = make_accounts()
        ctrl.statuses()
        assert ctrl.summary() == "Not signed in to any service."

    def test_summary_does_not_hit_the_network(self):
        """It is painted on every repaint, so it must never call a provider."""
        calls = []

        def provider():
            calls.append(True)
            return account("TIDAL", AccountState.SIGNED_IN)

        ctrl = make_accounts(tidal_status_provider=provider)
        ctrl.summary()
        ctrl.summary()

        assert calls == [], "summary() called a status provider"


class TestAsyncStatusReads:
    """Reading a status does HTTP, so it may never run on the UI thread."""

    def test_refresh_reads_on_a_worker_and_reports_back(self):
        ctrl = make_accounts(tidal_status_provider=signed_in())
        got = []

        wait(ctrl.refresh(got.append))

        assert len(got) == 1
        assert [s.service for s in got[0]] == ["TIDAL", "Spotify"]
        assert ctrl.summary() == "Signed in: TIDAL"

    def test_known_statuses_are_placeholders_until_the_first_read(self):
        calls = []

        def provider():
            calls.append(True)
            return account("TIDAL", AccountState.SIGNED_IN)

        ctrl = make_accounts(tidal_status_provider=provider)
        before = ctrl.known_statuses()

        assert calls == [], "known_statuses() must not call a provider"
        assert [s.service for s in before] == ["TIDAL", "Spotify"]
        assert all(s.detail == ctrl.UNKNOWN_DETAIL for s in before)

        wait(ctrl.refresh(lambda _s: None))
        assert ctrl.known_statuses()[0].state is AccountState.SIGNED_IN

    def test_a_second_refresh_while_one_runs_is_dropped(self):
        """Two concurrent reads would just race to repaint the same thing."""
        release = threading.Event()

        def slow():
            release.wait(timeout=5)
            return account("TIDAL", AccountState.SIGNED_IN)

        ctrl = make_accounts(tidal_status_provider=slow)
        first = ctrl.refresh(lambda _s: None)
        second = ctrl.refresh(lambda _s: None)

        assert second is None
        assert ctrl.is_reading

        release.set()
        wait(first)
        assert not ctrl.is_reading, "the flag must clear so Refresh works again"

    def test_the_reading_flag_clears_after_a_failure(self):
        ctrl = make_accounts(tidal_status_provider=raiser(RuntimeError("down")))

        wait(ctrl.refresh(lambda _s: None))
        assert not ctrl.is_reading
        # A broken provider still yields a status, so this reports done, not error.
        assert ctrl.known_statuses()[0].state is AccountState.UNAVAILABLE

    def test_needs_setup_is_not_offered_a_sign_in_button(self):
        status = account("Spotify", AccountState.NEEDS_SETUP)
        assert not status.can_sign_in
        assert account("Spotify", AccountState.SIGNED_OUT).can_sign_in


# ---------------------------------------------------------------------------
# TIDAL
# ---------------------------------------------------------------------------

class TestTidalSignIn:
    def test_it_calls_the_module_and_confirms_via_status(self):
        calls = []
        state = {"in": False}

        def sign_in():
            calls.append(True)
            state["in"] = True

        ctrl = make_accounts(
            tidal_status_provider=lambda: account(
                "TIDAL",
                AccountState.SIGNED_IN if state["in"] else AccountState.SIGNED_OUT,
            ),
            tidal_sign_in=sign_in,
        )
        done, errors = [], []

        wait(ctrl.sign_in("TIDAL", lambda: done.append(True), errors.append))

        assert calls == [True]
        assert done == [True]
        assert errors == []

    def test_a_silent_no_op_is_reported_as_failure(self):
        """The module returns nothing on success *and* on failure.

        Trusting the bare call would paint a green "signed in" for a browser
        flow the user abandoned, so the status provider gets the last word.
        """
        ctrl = make_accounts(
            tidal_status_provider=lambda: account(
                "TIDAL", AccountState.SIGNED_OUT, detail="still a guest"
            ),
            tidal_sign_in=lambda: None,
        )
        done, errors = [], []

        wait(ctrl.sign_in("TIDAL", lambda: done.append(True), errors.append))

        assert done == []
        assert errors == ["still a guest"]

    def test_without_the_module_it_refuses_instead_of_crashing(self):
        ctrl = make_accounts(tidal_sign_in=None)
        errors = []

        assert ctrl.sign_in("TIDAL", lambda: None, errors.append) is None
        assert errors and "not available" in errors[0]

    def test_module_errors_reach_the_user(self):
        ctrl = make_accounts(tidal_sign_in=raiser(RuntimeError("device flow refused")))
        errors = []

        wait(ctrl.sign_in("TIDAL", lambda: None, errors.append))
        assert errors == ["device flow refused"]

    def test_the_hint_is_shown_while_the_browser_is_open(self):
        """The flow blocks with no output of its own; the UI must say why."""
        ctrl = make_accounts(tidal_status_provider=signed_in(), tidal_sign_in=lambda: None)
        messages = []

        wait(ctrl.sign_in("TIDAL", lambda: None, lambda _m: None, messages.append))
        assert messages == [TIDAL_SIGN_IN_HINT]

    def test_a_second_click_is_refused_while_a_flow_is_open(self):
        """The device flow has no timeout -- two open browsers would be a mess."""
        release = threading.Event()
        ctrl = make_accounts(
            tidal_status_provider=signed_in(),
            tidal_sign_in=lambda: release.wait(timeout=5),
        )
        errors = []

        first = ctrl.sign_in("TIDAL", lambda: None, errors.append)
        second = ctrl.sign_in("TIDAL", lambda: None, errors.append)

        assert second is None
        assert errors and "already in progress" in errors[0]

        release.set()
        wait(first)
        assert not ctrl.is_busy("TIDAL"), "the button must work again afterwards"

    def test_the_guard_is_released_after_a_failure(self):
        ctrl = make_accounts(tidal_sign_in=raiser(RuntimeError("no")))

        wait(ctrl.sign_in("TIDAL", lambda: None, lambda _m: None))
        assert not ctrl.is_busy("TIDAL"), "a failed sign-in must not lock the button"


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

class TestSpotifySignIn:
    def _spotify(self, queue, source, **kwargs):
        kwargs.setdefault("open_url", lambda _u: None)
        kwargs.setdefault("wait_for_code", lambda _u, _s: "CODE")
        return SpotifyImportController(
            lambda: source,
            lambda: None,
            queue,
            dispatch=SyncDispatcher(),
            **kwargs,
        )

    def test_it_drives_the_pkce_flow(self, queue):
        source = AuthAwareSource(web=FakeWebBackend(), authorized=False)
        ctrl = make_accounts(spotify_controller=self._spotify(queue, source))
        done, errors = [], []

        wait(ctrl.sign_in("Spotify", lambda: done.append(True), errors.append))

        assert errors == []
        assert done == [True]
        assert [code for code, _verifier in source.web.exchanged] == ["CODE"]

    def test_missing_credentials_refuse_before_opening_a_browser(self, queue):
        """A consent page built from an empty client id can only fail.

        The check itself reads settings, so it runs on the worker -- the click
        handler must return immediately either way.
        """
        opened = []
        source = AuthAwareSource(web=FakeWebBackend(client_id=""))
        ctrl = make_accounts(
            spotify_status_provider=lambda: account(
                "Spotify",
                AccountState.NEEDS_SETUP,
                detail="Client ID missing",
                hint="Enter it under Settings > Spotify.",
            ),
            spotify_controller=self._spotify(queue, source, open_url=opened.append),
        )
        errors = []

        wait(ctrl.sign_in("Spotify", lambda: None, errors.append))

        assert opened == [], "no browser for a sign-in that cannot succeed"
        assert errors == ["Enter it under Settings > Spotify."]
        assert not ctrl.is_busy("Spotify"), "a refused sign-in must not lock the button"

    def test_without_a_controller_it_refuses(self):
        ctrl = make_accounts(spotify_controller=None)
        errors = []

        assert ctrl.sign_in("Spotify", lambda: None, errors.append) is None
        assert errors == ["Spotify is not configured."]

    def test_the_guard_is_released_after_a_failure(self, queue):
        source = AuthAwareSource(web=FakeWebBackend())
        spotify = self._spotify(
            queue, source, wait_for_code=lambda _u, _s: (_ for _ in ()).throw(RuntimeError("denied"))
        )
        ctrl = make_accounts(spotify_controller=spotify)
        errors = []

        wait(ctrl.sign_in("Spotify", lambda: None, errors.append))

        assert errors == ["denied"]
        assert not ctrl.is_busy("Spotify")

    def test_tidal_cannot_be_signed_out_from_here(self):
        """TIDAL's sessions live in OrpheusDL's own store, not ours."""
        assert make_accounts().sign_out("TIDAL") is False

    def test_sign_out_without_a_backend_reports_failure(self):
        assert make_accounts(spotify_controller=None).sign_out("Spotify") is False


# ---------------------------------------------------------------------------
# Shared guards
# ---------------------------------------------------------------------------

class TestSignInGuards:
    def test_a_running_download_blocks_sign_in(self):
        """Re-authenticating would swap the session out from under the transfer."""
        ctrl = make_accounts(tidal_sign_in=lambda: None, busy_provider=lambda: True)
        errors = []

        assert ctrl.sign_in("TIDAL", lambda: None, errors.append) is None
        assert errors and "download is running" in errors[0]

    def test_an_unknown_service_is_reported_not_ignored(self):
        ctrl = make_accounts()
        errors = []

        assert ctrl.sign_in("Deezer", lambda: None, errors.append) is None
        assert errors == ["Unknown service: Deezer"]


class TestSpotifySignInIsFinishedWhenItSaysSo:
    """The thread sign_in hands back has to cover the whole flow.

    It used to be only the precheck: the PKCE flow ran on a second worker whose
    handle was dropped, so joining the returned thread proved nothing and the
    result was read before it existed. Green on fast runners, red on macOS.
    """

    def _controller(self, queue, entered, release):
        """A sign-in that parks inside the flow until `release` is set."""
        source = AuthAwareSource(web=FakeWebBackend(), authorized=False)

        def slow_code(_uri, _state):
            entered.set()
            release.wait(timeout=5)
            return "CODE"

        spotify = SpotifyImportController(
            lambda: source,
            lambda: None,
            queue,
            dispatch=SyncDispatcher(),
            open_url=lambda _u: None,
            wait_for_code=slow_code,
        )
        return make_accounts(spotify_controller=spotify), source

    def test_the_result_is_there_once_the_thread_ends(self, queue):
        entered, release = threading.Event(), threading.Event()
        ctrl, source = self._controller(queue, entered, release)
        done, errors = [], []

        thread = ctrl.sign_in("Spotify", lambda: done.append(True), errors.append)
        assert thread is not None
        assert entered.wait(timeout=5), "the flow never started"
        assert done == [], "cannot be finished while the code is still pending"

        # Released from the side, so joining the thread is what has to wait.
        # A thread that only covered the precheck returns here immediately.
        threading.Timer(0.3, release.set).start()
        wait(thread)

        assert errors == []
        assert done == [True], "the thread ended before the sign-in had finished"
        assert [code for code, _v in source.web.exchanged] == ["CODE"]

    def test_the_guard_is_free_once_the_thread_ends(self, queue):
        entered, release = threading.Event(), threading.Event()
        ctrl, _source = self._controller(queue, entered, release)

        thread = ctrl.sign_in("Spotify", lambda: None, lambda _m: None)
        assert entered.wait(timeout=5)
        assert ctrl.is_busy("Spotify"), "the button must stay locked while it runs"

        threading.Timer(0.3, release.set).start()
        wait(thread)

        assert not ctrl.is_busy("Spotify"), "the button was still locked afterwards"


# ---------------------------------------------------------------------------
# SoundCloud and YouTube
#
# Neither has a sign-in flow: SoundCloud runs on a token copied from the
# browser, YouTube on an exported cookies.txt. The cards report whether that
# credential is there and usable -- and must never echo it back.
# ---------------------------------------------------------------------------

import time as _time  # noqa: E402

from hires.account_providers import (  # noqa: E402
    SERVICE_INFO,
    make_soundcloud_status_provider,
    make_youtube_status_provider,
    soundcloud_plan,
)

#: Stand-ins for secrets. Real tokens and cookies never appear in this file.
FAKE_TOKEN = "fake-soundcloud-token-0000"
FAKE_COOKIE_VALUE = "fake-cookie-value-1111"


class ProviderGui:
    """The slice of gui.py the SoundCloud/YouTube providers read."""

    def __init__(self, tmp_path, token="", cookies_path="", modules=("soundcloud", "youtube")):
        self.application_path = str(tmp_path)
        self.current_settings = {
            "credentials": {
                "SoundCloud": {"web_access_token": token},
                "YouTube": {"cookies_path": cookies_path},
            }
        }

        class Orpheus:
            module_list = list(modules)
            loaded_modules = {}

        self.orpheus_instance = Orpheus()


def all_text(status):
    return " ".join([status.detail, status.account, status.hint])


def go_plus_me(username="dj-test"):
    return {
        "username": username,
        "consumer_subscriptions": [{"product": {"id": "consumer-high-tier"}}],
    }


class TestSoundCloudStatus:
    def test_soundcloud_without_token_is_signed_out_with_a_hint(self, tmp_path):
        status = make_soundcloud_status_provider(ProviderGui(tmp_path))()
        assert status.state is AccountState.SIGNED_OUT
        assert "128" in status.detail and "preview" in status.detail
        assert "Settings > SoundCloud" in status.hint

    def test_soundcloud_go_plus_is_named(self, tmp_path):
        provider = make_soundcloud_status_provider(
            ProviderGui(tmp_path, token=FAKE_TOKEN), me_fetcher=lambda _t: go_plus_me()
        )
        status = provider()
        assert status.state is AccountState.SIGNED_IN
        assert "Go+ active" in status.detail and "256" in status.detail
        assert status.account == "dj-test (Go+)"

    def test_soundcloud_free_account_explains_what_it_gets(self, tmp_path):
        provider = make_soundcloud_status_provider(
            ProviderGui(tmp_path, token=FAKE_TOKEN), me_fetcher=lambda _t: {"username": "dj"}
        )
        status = provider()
        assert status.state is AccountState.SIGNED_IN
        assert "Free" in status.detail and "previews" in status.detail

    def test_soundcloud_rejected_token_reads_as_expired_and_hides_the_error(self, tmp_path):
        def reject(token):
            # A real client error may quote the request -- token included.
            raise RuntimeError(f"401 for Authorization: OAuth {token}")

        provider = make_soundcloud_status_provider(
            ProviderGui(tmp_path, token=FAKE_TOKEN), me_fetcher=reject
        )
        status = provider()
        assert status.state is AccountState.SIGNED_OUT
        assert "expired" in status.detail
        assert FAKE_TOKEN not in all_text(status)

    def test_soundcloud_offline_is_not_reported_as_an_expired_token(self, tmp_path):
        """Review part 1, finding 3: a network error is not a dead token."""

        def offline(_token):
            raise ConnectionError("Max retries exceeded with url: /me")

        provider = make_soundcloud_status_provider(
            ProviderGui(tmp_path, token=FAKE_TOKEN), me_fetcher=offline
        )
        status = provider()
        assert "expired" not in status.detail
        assert "could not be checked" in status.detail and "ConnectionError" in status.detail

    def test_soundcloud_module_missing_is_unavailable(self, tmp_path):
        gui = ProviderGui(tmp_path, token=FAKE_TOKEN, modules=("tidal",))
        status = make_soundcloud_status_provider(gui)()
        assert status.state is AccountState.UNAVAILABLE

    def test_soundcloud_checks_the_current_token_through_the_module_client(self, tmp_path):
        """The module keeps the token it was loaded with; the card must not."""
        seen = []

        class FakeApi:
            def __init__(self, token, _error):
                seen.append(token)

            def get_me(self):
                return go_plus_me()

        class FakeModule:
            exception = RuntimeError
            websession = FakeApi("token-at-load-time", RuntimeError)

        gui = ProviderGui(tmp_path, token=FAKE_TOKEN)
        gui.orpheus_instance.loaded_modules = {"soundcloud": FakeModule()}
        seen.clear()

        status = make_soundcloud_status_provider(gui)()

        assert seen == [FAKE_TOKEN]
        assert status.is_signed_in

    def test_soundcloud_status_never_contains_the_token(self, tmp_path):
        gui = ProviderGui(tmp_path, token=FAKE_TOKEN)

        def explode(_token):
            raise ValueError(FAKE_TOKEN)

        answers = [
            lambda _t: go_plus_me(),
            lambda _t: {},
            lambda _t: {"username": "dj"},
            explode,
        ]
        for fetch in answers:
            status = make_soundcloud_status_provider(gui, me_fetcher=fetch)()
            assert FAKE_TOKEN not in all_text(status)

    @pytest.mark.parametrize(
        "me, go_plus",
        [
            ({"consumer_subscriptions": [{"product": {"id": "consumer-high-tier"}}]}, True),
            ({"consumer_subscription": {"product": {"id": "go-plus"}}}, True),
            ({"quota": {"high_tier": True}}, True),
            ({"consumer_subscriptions": [{"product": {"id": "consumer-mid-tier"}}]}, False),
            ({"plan": "Free"}, False),
            ({}, False),
        ],
    )
    def test_soundcloud_plan_reading(self, me, go_plus):
        assert soundcloud_plan(me)[0] is go_plus


def write_cookies(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        for line in lines:
            fh.write("\t".join(str(part) for part in line) + "\n")


def cookie(name, expiry, domain=".youtube.com"):
    return (domain, "TRUE", "/", "TRUE", int(expiry), name, FAKE_COOKIE_VALUE)


class TestYouTubeStatus:
    def test_youtube_missing_cookie_file_is_signed_out(self, tmp_path):
        status = make_youtube_status_provider(ProviderGui(tmp_path))()
        assert status.state is AccountState.SIGNED_OUT
        assert "public videos still work" in status.detail
        assert "Settings > YouTube" in status.hint

    def test_youtube_default_path_resolves_inside_the_app_folder(self, tmp_path):
        write_cookies(
            str(tmp_path / "config" / "youtube-cookies.txt"),
            [cookie("LOGIN_INFO", _time.time() + 86400 * 100)],
        )
        status = make_youtube_status_provider(ProviderGui(tmp_path))()
        assert status.state is AccountState.SIGNED_IN
        assert "Not checked online" in status.detail

    def test_youtube_cookie_file_without_login_is_signed_out(self, tmp_path):
        path = str(tmp_path / "c.txt")
        write_cookies(path, [cookie("PREF", _time.time() + 86400), cookie("VISITOR_INFO1_LIVE", 0)])
        status = make_youtube_status_provider(ProviderGui(tmp_path, cookies_path=path))()
        assert status.state is AccountState.SIGNED_OUT
        assert "no YouTube login" in status.detail

    def test_youtube_expired_login_is_signed_out(self, tmp_path):
        path = str(tmp_path / "c.txt")
        write_cookies(path, [cookie("SID", _time.time() - 86400, domain=".google.com")])
        status = make_youtube_status_provider(ProviderGui(tmp_path, cookies_path=path))()
        assert status.state is AccountState.SIGNED_OUT
        assert "expired" in status.detail

    def test_youtube_old_export_gets_a_reexport_hint(self, tmp_path):
        path = str(tmp_path / "c.txt")
        write_cookies(path, [cookie("__Secure-3PSID", _time.time() + 86400 * 365)])
        later = _time.time() + 86400 * 45
        status = make_youtube_status_provider(
            ProviderGui(tmp_path, cookies_path=path), now=lambda: later
        )()
        assert status.state is AccountState.SIGNED_IN
        assert "45 days ago" in status.detail
        assert "fresh" in status.hint

    def test_youtube_httponly_lines_are_read(self, tmp_path):
        path = str(tmp_path / "c.txt")
        write_cookies(path, [])
        line = ["#HttpOnly_.youtube.com", "TRUE", "/", "TRUE",
                str(int(_time.time() + 86400)), "LOGIN_INFO", FAKE_COOKIE_VALUE]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\t".join(line) + "\n")
        status = make_youtube_status_provider(ProviderGui(tmp_path, cookies_path=path))()
        assert status.is_signed_in

    def test_youtube_status_never_contains_a_cookie_value(self, tmp_path):
        path = str(tmp_path / "c.txt")
        for lines in (
            [cookie("LOGIN_INFO", _time.time() + 86400)],
            [cookie("PREF", 0)],
            [cookie("SID", _time.time() - 10)],
        ):
            write_cookies(path, lines)
            status = make_youtube_status_provider(ProviderGui(tmp_path, cookies_path=path))()
            assert FAKE_COOKIE_VALUE not in all_text(status)


class TestFourServiceCards:
    def test_soundcloud_and_youtube_are_listed_after_tidal_and_spotify(self):
        ctrl = make_accounts(
            soundcloud_status_provider=signed_out("SoundCloud"),
            youtube_status_provider=signed_out("YouTube"),
        )
        assert [s.service for s in ctrl.statuses()] == ["TIDAL", "Spotify", "SoundCloud", "YouTube"]

    def test_soundcloud_and_youtube_each_have_an_explanation(self):
        assert "Go+" in SERVICE_INFO["SoundCloud"] and "token" in SERVICE_INFO["SoundCloud"]
        assert "cookies.txt" in SERVICE_INFO["YouTube"]
