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
        assert make_accounts(tidal_status_provider=signed_in()).summary() == "Signed in: TIDAL"
        assert make_accounts().summary() == "Not signed in to any service."

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
        """A consent page built from an empty client id can only fail."""
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

        assert ctrl.sign_in("Spotify", lambda: None, errors.append) is None
        assert opened == [], "no browser for a sign-in that cannot succeed"
        assert errors == ["Enter it under Settings > Spotify."]

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
