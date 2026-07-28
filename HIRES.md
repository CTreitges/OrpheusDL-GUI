# Hi-Res Suite

Four additions to OrpheusDL-GUI, built on top of upstream `v2.0.3`:

1. **Accounts** — sign in to TIDAL and Spotify before you queue anything.
2. **Download queue** — a persistent, reorderable queue that survives restarts.
3. **TIDAL playlist browser** — pick your own TIDAL playlists inside the app.
4. **Spotify → TIDAL** — convert a Spotify playlist and download it in hi-res.

Everything lives in the `hires/` package. The only change to `gui.py` is four
lines that add the tabs, wrapped in a `try/except` — if anything here breaks,
the stock GUI still starts, just without the extra tabs.

---

## Accounts

Both services can be signed into implicitly — TIDAL when a download first needs
credentials, Spotify when you first click **My playlists**. That works, but it
means your *first download* is what suddenly opens a browser window. The
**Accounts** tab moves that step to the front.

Each service shows a state and, where it makes sense, a button:

| State | Means | Button |
|-------|-------|--------|
| **Signed in** | Ready. Shows the account where the service tells us one. | *Sign in again* (expired session, wrong account) |
| **Not signed in** | Everything is in place, you just have not signed in. | *Sign in* |
| **Setup needed** | Signing in cannot work yet — Spotify without a Client ID. | disabled, with what to do |
| **Unavailable** | The service is not installed at all. | disabled |

"Setup needed" and "Unavailable" are deliberately not shown as failed logins:
no button can fix either, so the tab says what actually has to happen instead.

### This tab gates nothing

Signing in stays optional. TIDAL browses as a guest on purpose, and public
Spotify playlist links need no login at all — so the other tabs keep working
untouched whether you use this one or not.

### TIDAL

Uses the TIDAL module's own TV/browser flow, the same one a download triggers.
Your browser opens at `link.tidal.com`; the app waits until you finish there.

Two consequences worth knowing:

- **There is no timeout and no cancel.** The module polls until you either
  finish or close the app. Abandoning the browser tab leaves a harmless daemon
  thread polling until you quit — it never blocks shutdown, but the button
  stays on *Waiting…* until then.
- **The module reports no result.** After the call returns, the tab re-reads
  the login state rather than assuming success, so an abandoned flow shows as
  failed instead of a green tick.

Sessions are stored by OrpheusDL, not by this suite — so signing *out* of TIDAL
belongs in the stock GUI, and this tab does not offer it.

### Spotify

Runs the same PKCE flow described under *Spotify → TIDAL* below, just started
explicitly. It needs the Client ID and Secret from *Settings → Spotify*; without
them the tab reports "Setup needed" rather than sending you to a consent page
that can only be rejected.

The Accounts tab and the Spotify tab share one source object, so signing in
here immediately counts over there.

### While a download runs

Sign-in is refused with a message. Re-authenticating replaces the session the
running transfer is using.

---

## Queue

A new **Queue** tab. Items are added from the TIDAL and Spotify tabs, then
handed to OrpheusDL one at a time.

| What | Behaviour |
|------|-----------|
| Persistence | `config/hires_queue.json`, written atomically. A crash mid-download re-queues that item on the next start instead of losing it. |
| Ordering | Move items up/down; the queue is processed top to bottom. |
| Pause | Stops the *next* item from starting. A running download is not interrupted. |
| Retries | A failed item can be retried until `max_attempts` (default 3). "Retry failed" re-queues everything retryable at once. |
| Status | `•` waiting · `▶` running · `✓` done · `✗` failed · `–` cancelled |

The queue does not replace OrpheusDL's download logic — it feeds it. Every
download still goes through the stock code path, so per-platform rate limits,
pauses and cancellation behave exactly as before.

### How completion is detected

`gui.py` computes download success inside a local closure that cannot be reached
from outside. The queue therefore judges by observable effect: it records the
target folder before starting and checks afterwards whether an audio file
appeared or an existing one was rewritten. Cancellation is read from the GUI's
`stop_event`.

This is a heuristic. It is accurate for normal downloads, but a download that
writes nothing because the file already existed *and* `ignore_existing_files` is
enabled will be marked failed. Retrying such an item is harmless.

---

## TIDAL playlist browser

A new **TIDAL Playlists** tab lists your own playlists and your favourites,
read through the TIDAL module you already have installed and logged in.

- **Load playlists** — fetches your library (paginated; large libraries work).
- **Include favorites** — toggle between "mine only" and "mine + saved".
- **Add to queue** — queues the playlist as a single item, so OrpheusDL's own
  playlist handling (folder naming, `.m3u` writing) stays intact.

Requires the TIDAL module installed under `modules/tidal` and a logged-in
account. Without it the tab explains what is missing instead of failing silently.

---

## Spotify → TIDAL

A new **Spotify → TIDAL** tab. Give it a Spotify playlist, it finds each track
on TIDAL and queues the matches for hi-res download.

### Getting the playlist

Two backends, chosen automatically:

| Backend | Needs | Gives you |
|---------|-------|-----------|
| **Web API** | Your Spotify **Client ID + Secret** (already in *Settings → Spotify* if you use Spotify downloads) | Your own private playlists, Liked Songs, and **ISRCs** — which make matching near-perfect |
| **Embed** (fallback) | Nothing | Public playlists by link. Usually no ISRC, so matching falls back to fuzzy |

If the Web API is configured it is used first; on any failure the embed backend
takes over. Paste a link and it just works; click **My playlists** to sign in and
browse your own.

### Signing in

Clicking **My playlists** starts the sign-in automatically if it has not
happened yet:

1. Your browser opens Spotify's consent page (PKCE, no secret leaves the app).
2. Spotify redirects to `http://127.0.0.1:8888/callback`, where the app listens
   once to catch the authorization code. A random `state` is generated per
   attempt and verified, so a stray page cannot feed the app a code.
3. The refresh token is written to `config/hires_spotify_tokens.json` with
   `0600` permissions. That file is gitignored — never commit it.

If port 8888 is occupied the app says so instead of hanging. Needs the Client
ID and Secret from *Settings → Spotify*.

### Matching

Matching is **ISRC-first with a fuzzy fallback and a review step** — the same
approach Soundiiz-style converters use.

1. **ISRC** — an exact recording identifier. When Spotify supplies one and TIDAL
   has it, the match is exact. If several TIDAL releases share an ISRC (common
   for re-releases), the closest one wins and the rest become alternatives.
2. **Fuzzy** — artist (35%), title (45%) and duration (20%), with several search
   phrasings tried and merged.
3. **Version guard** — this is what stops the usual failure mode. Tags like
   *Live*, *Remix*, *Acoustic*, *Radio Edit*, *2011 Remaster*, *Instrumental*
   are extracted from both titles; a mismatch is penalised heavily. A live take
   will not be silently downloaded in place of the studio recording.

Each track lands in one of three buckets:

| Score | Result |
|-------|--------|
| ≥ 0.88 | **Matched** — queued directly |
| ≥ 0.62 | **Needs review** — shown to you first |
| < 0.62 | **Not found** — never queued |

**Queue matches** queues only the confident ones. **Review uncertain…** opens a
dialog where each track can be confirmed, swapped for an alternative, or
skipped. Nothing uncertain downloads without your say-so.

### Quality

Queued items are pinned to `hifi`, which on TIDAL resolves to
`HI_RES_LOSSLESS` (24 bit, up to 192 kHz FLAC) — regardless of the global
quality setting. This uses the same per-item `download_quality_override`
mechanism the search tab already uses for Atmos.

Converted playlists land in their own folder under your download path, named
after the playlist.

---

## What this does not do

- It does not create a playlist on TIDAL — it downloads the tracks. Nothing is
  written to your TIDAL account.
- It does not download from Spotify. Spotify is only read for metadata; the
  audio comes from TIDAL.
- Tracks that are not on TIDAL cannot be downloaded. They are listed as
  "not found" rather than quietly dropped.

You need your own valid TIDAL subscription. Hi-res streams require a plan that
includes them.

---

## Development

The suite is deliberately split so that everything except `gui_panel.py` is
free of tkinter and can be tested headlessly:

| Module | Purpose |
|--------|---------|
| `models.py` | Shared dataclasses — the contract every other module implements against |
| `queue_store.py` | Persistent, thread-safe queue |
| `tidal_library.py` | TIDAL playlist/track access |
| `spotify_source.py` | Spotify Web API + embed fallback |
| `matcher.py` | ISRC + fuzzy matching |
| `converter.py` | Playlist → matches → queue items |
| `quality.py` | Quality tiers and the hi-res override |
| `controllers.py` | All UI logic, no widgets |
| `gui_panel.py` | Widgets only |
| `integration.py` | The bridge into `gui.py` |

```bash
# Headless: everything except the widget tests (which skip themselves).
pip install pytest requests
python -m pytest tests/ -v

# Including the widget tests, on a machine with no display:
pip install customtkinter
xvfb-run -a --server-args="-screen 0 1280x900x24" python -m pytest tests/ -v
```

Two suites are worth knowing about:

- `tests/test_end_to_end.py` wires the real modules together and fakes only the
  two outside edges (the Spotify and TIDAL APIs), so it catches drift between
  modules that the per-module tests cannot.
- `tests/test_gui_panel.py` builds real widgets. It skips itself without
  tkinter or a display, and runs in CI under Xvfb.

### Threading

`widget.after()` is **not** thread-safe — called from a worker thread it is
silently dropped, and the callback simply never runs. Since all network work
happens on worker threads, results are handed over through
`UiDispatcher`, which puts them on a `queue.Queue` that a timer on the main
thread drains. Never call `after()` (or touch a widget) directly from a worker.

This is not theoretical: an earlier build used `after()` from worker threads,
which left "Load playlists" spinning forever and the Spotify conversion stuck at
"Reading playlist…". Only the widget tests catch it.

### Test the wiring, not just the parts

Every unit test injects its own providers, which is precisely how a bug in
`setup_tabs()` survived 424 passing tests: it passed the GUI's *global* quality
into the converter, so a user with "High" selected silently got 320 kbit AAC
while the UI promised 24 bit FLAC. `setup_tabs()` is the only place the app is
really assembled, and nothing exercised it.

`TestSetupTabsWiring` in `tests/test_gui_panel.py` now drives the real assembly
and asserts hi-res survives every global setting. When you add a provider or
change how the tabs are built, extend that class — a test that supplies its own
parameters cannot catch a wiring mistake.

The Accounts tab followed that rule: alongside its controller tests in
`tests/test_accounts.py`, `TestSetupTabsWiring` asserts against the *real*
`setup_tabs` output that the status providers are the ones it built, that the
sign-in button reaches the TIDAL module with `force=True`, and that the
Accounts and Spotify tabs share one controller — three things a test with
injected providers would pass without ever proving.
