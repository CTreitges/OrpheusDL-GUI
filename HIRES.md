# Hi-Res Suite

Four additions to OrpheusDL-GUI, built on top of upstream `v2.0.3`:

1. **Accounts** — TIDAL, Spotify, SoundCloud and YouTube: sign in or check the
   token/cookie file before you queue anything.
2. **Download queue** — a persistent queue grouped by playlist that survives restarts.
3. **Playlists** — browse playlists of Spotify, TIDAL, YouTube and SoundCloud
   and queue them whole or in part.
4. **Port** — read a playlist on one of the four services, find its tracks on
   another and queue the matches (12 directions; Spotify → TIDAL in hi-res is
   the default).

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

**Guest sessions read as signed out**, which is the distinction the whole tab
rests on. A guest session is a fully authenticated object at the API level —
`TidalGuestSession.auth()` runs a client-credentials grant and holds a real
access token — and the only thing separating it from a login is that it never
sets a `user_id`. So "logged in" is decided by `user_id`, not by "has a
session". If that ever inverted, the tab would show a green tick for someone
who never signed in and their downloads would quietly not be hi-res;
`TestGuestIsNotAuthenticated` in `tests/test_tidal_library.py` pins it.

### Spotify

Runs the same PKCE flow described under *Port* below, just started
explicitly. It needs the Client ID and Secret from *Settings → Spotify*; without
them the tab reports "Setup needed" rather than sending you to a consent page
that can only be rejected.

The Accounts tab and the Spotify tab share one source object, so signing in
here immediately counts over there.

### While a download runs

Sign-in is refused with a message. Re-authenticating replaces the session the
running transfer is using.

### SoundCloud and YouTube

Neither has a sign-in flow, so their cards report whether the credential the
module runs on is there and usable, and their button opens the matching
settings page instead of a browser.

| Service | Reads | Shows |
|---------|-------|-------|
| SoundCloud | `web_access_token` from *Settings → SoundCloud*, checked through the SoundCloud module's own API client with the *current* token | Go+ or not (Go+ = full-length AAC 256 kbit/s), the account name, or "token rejected — probably expired" |
| YouTube | the `cookies.txt` named under *Settings → YouTube* | whether it exists, holds a YouTube/Google login, when it was exported and when the login expires. Not checked online. |

Secrets never leave `hires/account_providers.py`: the token only goes to the
module's client, cookie values are dropped while the file is read, and an
error from the API is replaced by a fixed message because it may quote the
request.

### Automatic re-checks

A service that is not signed in when the tab opens is often just slow — the
TIDAL module loads after the window. The tab therefore checks again by
itself, at most five times, 2, 4, 8, 15 and 30 s apart ("Checking… (n/5)"),
then stops: "Stopped after 5 checks — press Refresh to check again." Refresh
cancels a running schedule and starts the count from zero. Everything runs on
one worker thread (`hires/auto_refresh.py`); the window only hears about it
through the dispatcher.

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

### Playlists as groups

The queue is shown per playlist: a header with the name, progress ("12 of 60
done · running") and two buttons, the tracks below it. Which group an entry
belongs to is derived from fields every entry already has — `group_id` (one
playlist run), else `group_label` (a playlist or folder queued as one), else
the collective group "Single tracks" — so a queue file from an older version
loads unchanged.

- **⇧ To front** moves the whole playlist ahead of everything else. The track
  that is downloading is never interrupted: it finishes, then the moved
  playlist runs, then the interrupted playlist continues with its remaining
  tracks in their old order.
- **✕** removes a playlist's entries after asking; a running track of it
  finishes.
- Clicking a header folds the playlist away. Long queues show at most 150
  track lines; a header whose tracks did not fit opens on click.
- **Clear all…** empties the whole queue after a confirmation (Cancel is the
  default button). A track the queue is downloading right now is stopped and
  removed; a download you started by hand is left alone.

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

## Playlists

The **Playlists** tab (it replaced "TIDAL Playlists") shows playlists of one
service at a time — pick **Spotify**, **TIDAL**, **YouTube** or **SoundCloud**
at the top.

- **My playlists** — your own playlists, where the service's login allows it.
- **Open link** — any public playlist link of the chosen service.
- Clicking a playlist shows its **tracks** on the right. Click, Shift+click or
  Ctrl+click picks some of them; **Queue picked tracks** queues only those, as
  one group named after the playlist.
- **Add to queue** — queues the highlighted (or every ticked) playlist as a
  whole, as a single item, so OrpheusDL's own playlist handling (folder naming,
  `.m3u` writing) stays intact.

What each service needs — and the tab says so in a line under the link field
whenever something is missing, instead of showing an empty list:

| Service | Own playlists | Links |
|---------|---------------|-------|
| TIDAL | signed in on the Accounts tab (also *Include favorites*, folders) | signed in |
| Spotify | signed in (Client ID and Secret under *Settings → Spotify*) | public links without anything |
| YouTube | a `cookies.txt` under *Settings → YouTube* | public links without anything |
| SoundCloud | the token under *Settings → SoundCloud* | public links without anything |

Downloads from TIDAL are pinned to hi-res as before; the other services
download at the quality set in Settings.

SoundCloud is read through the SoundCloud module's own API client. Its built-in
public client id is out of date (SoundCloud answers 401 without a token), so the
tab looks up the id the web player currently uses — once, the same way yt-dlp
does. YouTube is read through yt-dlp; the cookie file is only handed over as a
temporary copy and never read by the suite itself.

### Folders (TIDAL)

Playlists appear inside your TIDAL folders where TIDAL will tell us about them.

That is a real caveat: **TIDAL has no public folder API.** Folders live on
`listen.tidal.com/v2/my-collection/playlists/folders` — the web player's own
internal endpoint, which is undocumented and may reject the module's TV client
token outright. So the folder view is treated as a bonus, never as a
requirement: any failure at all — refused, timed out, unrecognisable response —
falls back to the flat list you had before, plus nothing worse than losing the
grouping. The playlists themselves always come from the supported v1 endpoint,
so no playlist can go missing because of a folder problem.

A playlist that no folder claims stays at the top level, which means a
partially readable layout still shows everything.

### Downloading more than one at a time

Every row has a checkbox, and so does every folder — ticking a folder ticks
everything inside it, sub-folders included. **Queue N** then queues each
playlist as its own item, grouped under the folder name.

Separate items rather than one big one, so a playlist that fails can be retried
without touching the ones that already worked.

Ticking and clicking are independent: clicking a row still highlights it for
the single-playlist actions, and with boxes ticked the button acts on the ticks.

---

## Port

The **Port** tab (it replaced "Spotify → TIDAL") reads a playlist on one
service and finds every track on another: **From** and **To** take any two of
Spotify, TIDAL, YouTube and SoundCloud — 12 directions. **⇄** swaps them;
picking the other side's service swaps them too. The matches become one group
in the queue.

Porting only **reads** the source and only **searches** the target. Nothing is
created, changed or liked in any account.

| | Read from (source) | Search on (target) |
|---|---|---|
| Spotify | public links; own playlists after sign-in | needs Client ID and Secret (app token, no sign-in) |
| TIDAL | signed in | signed in |
| YouTube | public links; own playlists with `cookies.txt` | always |
| SoundCloud | public links; own playlists with the token | always |

Matching works as described below for every direction: ISRC first where both
sides have one (Spotify and TIDAL look ISRCs up; SoundCloud label uploads carry
them as a source), otherwise title, artist and length with the version guard.
YouTube and SoundCloud titles are split into artist and title first
("Artist - Title (Official Video)" → artist, title; channel names lose
"- Topic" and "VEVO").

Searches are paced — at least 1 s between two YouTube or SoundCloud requests
(yt-dlp's own "sleep" preset spaces requests 0.75 s apart). An HTTP 429 or a
bot check stops the whole port at once instead of moving on to the next track.

Spotify → TIDAL is the default direction and behaves exactly as the former tab:

### Spotify → TIDAL

### Getting the playlist

Two backends, chosen automatically:

| Backend | Needs | Gives you |
|---------|-------|-----------|
| **Web API** | Your Spotify **Client ID + Secret** (already in *Settings → Spotify* if you use Spotify downloads) | Your own private playlists, Liked Songs, and **ISRCs** — which make matching near-perfect |
| **Embed** (fallback) | Nothing | Public playlists by link. Usually no ISRC, so matching falls back to fuzzy |

If the Web API is configured it is used first; on any failure the embed backend
takes over. Paste a link and it just works; click **My playlists** to sign in and
browse your own.

### Folders: there are none to show

Unlike TIDAL, this is not a limitation being worked around — Spotify's Web API
[does not expose playlist folders at all][spotify-folders], and never has. The
only way to read them is to parse the desktop client's local LevelDB cache,
which needs the desktop app installed, mostly closed (it holds a write lock on
the live log file), and differs per platform and per install type.

That is not shipped here. The list is flat, and **the checkboxes cover what
folders would have been used for**: tick several playlists — or hit *Select
all* — and **Convert & queue N** works through them one after another, queueing
each playlist's matches as soon as that playlist is done. A long run is
therefore useful even if a later playlist fails: the earlier downloads have
already started.

[spotify-folders]: https://developer.spotify.com/documentation/web-api/concepts/playlists

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

## Help markers and recommendations

Every setting on *Settings → Global* and on the Spotify, TIDAL, YouTube and
SoundCloud tabs has a **?** behind its label; hovering it explains the
setting and recommends a value for DJ use. Buttons of the hi-res tabs carry
the same kind of text as a tooltip. The texts live in `hires/help_texts.py`;
`tests/test_help_coverage.py` reads the setting keys out of `gui.py` and fails
for any setting without one.

Where a bad value costs a block or a file the DJ software rejects, the
recommendation is also shown next to the field:

| Setting | Recommendation | Basis |
|---------|----------------|-------|
| Concurrent downloads | 3–4 for TIDAL and SoundCloud, 1–2 for Spotify and YouTube | streamrip defaults to 6 at once; YouTube's guest limit is about 300 videos/hour; Spotify: rule of thumb |
| Spotify pause | 30–60 s, never below 15 s | librespot warns of account bans; no published limit (rule of thumb) |
| YouTube pause | 10–20 s, 5 s minimum | yt-dlp's `-t sleep` preset (10–20 s); yt-dlp wiki limits |
| TIDAL throttle | on (≈60 requests/min) | streamrip `requests_per_minute = 60`; Tidekeeper: raise the delay on repeated HTTP 429 |
| Batch throttle | 50 tracks / 300 s for Spotify and YouTube, 200 / 600 s for TIDAL | derived from the hourly limits (rule of thumb) |
| Quality | lossless (16/44.1 FLAC); hifi only for CDJ-3000/CDJ-2000NXS2-class players | rekordbox FAQ: no Pioneer player above 96 kHz, XDJ-RX3/XDJ-XZ up to 48 kHz |
| AAC / MP3 / Opus | AAC ≥ 256k, MP3 320k CBR, avoid Opus | Digital DJ Tips; Engine DJ and Traktor format lists (no Opus) |

The recommendations are hints. Two defaults follow them for **new installs
only** -- 3 concurrent downloads (was 5) and a 10 s YouTube pause (was 5 s);
an existing `settings.json` keeps its values, because loading lets every value
in the file win over the defaults.

## What this does not do

- It does not create a playlist anywhere — it downloads the tracks. Nothing is
  written to any account (TIDAL, Spotify, YouTube, SoundCloud).
- Porting from Spotify does not download from Spotify. Spotify is only read for
  metadata; the audio comes from the target service.
- Tracks the target service does not have cannot be downloaded. They are listed
  as "not found" rather than quietly dropped.

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
| `spotify_source.py` | Spotify Web API + embed fallback, track search |
| `playlist_providers.py` | One interface for all four services (read, open by link, search, ISRC) |
| `soundcloud_source.py`, `youtube_source.py` | SoundCloud and YouTube behind that interface |
| `provider_registry.py` | Builds the four providers from the running GUI |
| `playlists_controller.py`, `port_controller.py` | Logic of the Playlists and Port tabs |
| `matcher.py` | ISRC + fuzzy matching |
| `converter.py` | Playlist → matches → queue items |
| `quality.py` | Quality tiers and the hi-res override |
| `controllers.py` | All UI logic, no widgets |
| `queue_groups.py` | Which entries form a playlist group, group progress |
| `auto_refresh.py` | Bounded automatic re-checks of the account states |
| `account_providers.py` | SoundCloud/YouTube status, service explanations |
| `help_texts.py` | Help text and recommendation per setting and control |
| `gui_panel.py` | Widgets only: Accounts tab, shared playlist list and review dialog, `build_tabs` |
| `queue_panel.py` | Widgets of the grouped Queue tab |
| `playlists_panel.py`, `port_panel.py` | Widgets of the Playlists and Port tabs |
| `help_widget.py`, `dialogs.py`, `ui_style.py` | "?" marker and tooltip, confirmation dialog, palette |
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
Accounts and Port tabs share one controller — three things a test with
injected providers would pass without ever proving.
