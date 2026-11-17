# Hi-Res Suite

Additions to OrpheusDL-GUI, built on top of upstream `v2.0.3`:

1. **Accounts** — TIDAL, Spotify, SoundCloud and YouTube: sign in or check the
   token/cookie file before you queue anything.
2. **Download queue** — a persistent queue grouped by playlist that survives
   restarts, showing the quality each finished download really arrived in.
3. **Playlists** — browse playlists of Spotify, TIDAL, YouTube and SoundCloud
   and queue them whole or in part.
4. **Port** — read a playlist on one of the four services, find its tracks on
   another and queue the matches (12 directions; Spotify → TIDAL in hi-res is
   the default).
5. **Target format** — convert every download to WAV, FLAC or AIFF, and whole
   folders on demand (*Settings → Codecs*).
6. **Updates** — the Windows one-folder build updates without being set up
   again: `Update OrpheusDL.cmd` once, then auto-update from the app.

Everything lives in the `hires/` package. `gui.py` only wires it in: the lines
that add the tabs (wrapped in a `try/except` — if anything here breaks, the
stock GUI still starts, just without the extra tabs), the "?" help markers,
the two target-format settings and one call at the end of every download.

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

### The quality that really arrived

A finished entry shows what was actually downloaded — read from the files, not
taken from the request: `FLAC 24-bit/96 kHz`, `AAC 160 kbit/s`,
`WAV 16-bit/44.1 kHz`. A playlist entry with mixed files lists each label with
its count (`FLAC 16-bit/44.1 kHz ×11 · AAC 256 kbit/s ×1`); a converted
download adds the target (`AAC 160 kbit/s → AIFF`). Until an entry is done it
shows the tier it asked for.

The download thread reads the new files with mutagen right after they are
written (`hires/download_finish.py`) and hands the label to the queue runner,
keyed by that thread — a download you started by hand never lends its label to
a queue entry.

---

## Target format

*Settings → Codecs → Target format* (`off`, `wav`, `flac`, `aiff`; default
`off`, so nothing changes until you pick one) converts every finished download,
from any tab:

| Rule | Why |
|------|-----|
| Sample rate and bit depth stay as they arrived; lossy sources become 16 bit | No upsampling: a bigger number would not mean better sound |
| ffmpeg writes a `.part` file; it is checked (sample rate, bit depth, length ±50 ms) and tagged before it replaces the original | A failed or cut conversion never costs the original |
| All tags and covers are copied with mutagen: ID3 → WAV/AIFF frame by frame (Serato cue points, ratings, lyrics included); from FLAC/MP4 every field keeps its name (`TXXX` or Vorbis comment) | ffmpeg's WAV writer cannot store a cover; DJ software reads the ID3 chunk |
| A tag the new format cannot hold (e.g. Serato's binary cue frames into FLAC) keeps the original next to the copy, with a log line | Never delete the only file that still has it |
| Lossy sources (AAC, MP3, Opus, Vorbis) are converted with a note in the log | A WAV of a 160 kbit/s stream is still 160 kbit/s, just bigger |
| Dolby Atmos / more than two channels, and float or 32-bit sources into FLAC, are skipped; float WAV/AIFF stay float at their width | WAV would fold Atmos to stereo; FLAC cannot hold them losslessly |
| Only paths that did not exist when the download started, and were created since, are touched | A library file another program re-tagged meanwhile is never converted or deleted |
| One converter per target file; the result never replaces an existing file | The download hook and "Convert folder…" can run side by side safely |
| `.m3u` playlists written by the same download point at the new files | Playlist imports keep working |

**Keep Original** keeps the downloaded file next to the converted one; off
(default) replaces it once the new file checked out.

The conversion runs on the download thread before the GUI reports the download
as finished, so the queue starts its next item only afterwards. **Stop**
interrupts it between files; the file being converted keeps its original.

**Convert folder…** (next to the setting) does the same for a folder you
already have, sub-folders included: pick a folder, a format and Keep original.
It counts the files off the main thread, then — without Keep original — asks
first ("Replace originals?", Cancel is the default). It refuses to start while
a download runs, and the queue starts no new item while it converts; a
download started by hand meanwhile shows a hint instead of starting. Progress
shows files done, failures and the time left; Cancel stops at once (the file
being converted keeps its original). Playlists in the folder are pointed at the
new files. Where two files share a name (`x.flac`, `x.m4a`) the lossless one is
converted. Measured: 200 files in 12 s, 200 in, 200 out, 0 lost.

One case stays open by design: a file another program copies *into* the
download folder while a download runs is new to the folder too, and is
converted with it. Keep the download folder out of sync tools (OneDrive,
Dropbox, Syncthing) — the help text of *Download path* says the same.

Recommendation for DJ use: FLAC, or AIFF where a player or USB export needs
uncompressed files with tags; WAV only when nothing else is accepted.

---

## SoundCloud module (fork)

The build takes the SoundCloud module from
[CTreitges/orpheusdl-soundcloud](https://github.com/CTreitges/orpheusdl-soundcloud)
at a pinned commit (upstream `e1eff2e` plus one fix), because measuring seven
real tracks found two faults in upstream:

- **Go+ tracks without Go+ were saved as their 30-second preview** — no error,
  a 30 s file under the full track's name. SoundCloud marks those streams
  `snipped`; the fork skips them and reports "only a 30-second preview is
  available" instead.
- **Without a token nothing downloaded**: the module's built-in public client
  id is out of date (401). The fork reads the current one from soundcloud.com,
  as yt-dlp does.

Otherwise the module already picks the best stream: the uploader's original
file where downloads are allowed (also WAV), else AAC 160 kbit/s (free) — Go+
accounts get the full-length Go+ streams. Tracks offered only as encrypted
streams cannot be downloaded. `tests/test_build_workflow.py` keeps the build on
the pinned fork until upstream has the fix.

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
Spotify, TIDAL, YouTube and SoundCloud — 12 directions. **Swap** swaps them;
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
("Artist - Title (Official Video)" → artist, title). Where the title has no
artist, the channel name stands in, without endings that are not part of the
name: "Daft Punk - Topic", "BASTILLEvideos" → "BASTILLE",
"MartinGarrixVEVO" → "Martin Garrix", "Dua Lipa Official", "RobinSchulzMusic".
A glued ending only counts at a word boundary ("MTV", "Unofficial" stay), and
"Music"/"TV" after a space stay too — "Roxy Music" is a band. Measured on ten
artist channels (live, 17.11.): 8 → 9 of 10 channel names give the artist.

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
all* — and **Port & queue N** works through them one after another, queueing
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

**Port & queue** (next to *Port*) does both in one click: it ports the link —
or the highlighted playlist — and queues the confident matches right away as
one group; the uncertain ones wait in *Review uncertain…* and join the same
group once confirmed. A track is never queued twice, whichever way you go.
A playlist ported by link shows as a row in the list.

### Quality

Queued items are pinned to `hifi`, which on TIDAL resolves to
`HI_RES_LOSSLESS` (24 bit, up to 192 kHz FLAC) — regardless of the global
quality setting. This uses the same per-item `download_quality_override`
mechanism the search tab already uses for Atmos.

Converted playlists land in their own folder under your download path, named
after the playlist.

---

## Updates

The Windows build is one folder: `OrpheusDL_GUI.exe` next to `config/` (logins,
settings, queue, Spotify token), `modules/`, `extensions/` and `downloads/`.
Both ways of updating replace only the program and the bundled modules;
`config/`, `downloads/` and `extensions/` are never written, moved or copied
(the tests hash them before and after). Both run the same code
(`hires/self_update.py`).

### Once: `Update OrpheusDL.cmd`

Every Windows release ZIP holds the EXE and `Update OrpheusDL.cmd`.

1. Close OrpheusDL (also from the tray).
2. Unpack the new ZIP **into** your old OrpheusDL folder — Windows' *Extract
   all* puts it there as a sub folder.
3. Double-click `Update OrpheusDL.cmd` in that sub folder.

The script only starts the new EXE with `--install-into <old folder>`; the
EXE does the rest: it checks that the folder above holds `OrpheusDL_GUI.exe`
and `config/`, that OrpheusDL is not running, then renames the old EXE to
`OrpheusDL_GUI.old.exe`, puts itself in its place, replaces the bundled
modules (the old ones go to `modules.backup-<version>/`, a module of your own
stays) and starts the updated app. Anything wrong — wrong folder, app still
running, no write access — and it says why and changes nothing. Running it
twice does nothing the second time. The sub folder can be deleted afterwards.

### From then on: auto-update

On start, at most once a day, the app asks GitHub for the latest release of
**CTreitges/OrpheusDL-GUI** (no pre-releases) — on a worker thread, so the
window never waits; without internet it stays silent (one log line).
*About → Check for updates* asks at any time; *Settings → Advanced → Check For
Updates* switches the daily check off.

When a newer release exists, a window shows the version and its notes and
asks. *Update now*:

| Step | What is checked |
|------|-----------------|
| Download of `OrpheusDL-GUI-windows.zip` into `update/` | only from this repository's release URL over HTTPS; redirects only to GitHub hosts; size as announced |
| SHA-256 of the ZIP | must equal the asset's `digest` in GitHub's API — a release without one is offered as a page, never installed |
| Taking the EXE out | only the entry `OrpheusDL_GUI.exe` at the top of the ZIP, a Windows program, at most 1 GiB; nothing else is unpacked |
| Waiting | while anything downloads (a download by hand, the queue, *Convert folder…*) the update waits and the queue starts nothing new; *Cancel update* lets the queue go on |
| Swap | the running EXE is renamed to `OrpheusDL_GUI.old.exe` (Windows allows renaming a running program, not overwriting it), the new one takes its place; a failure puts the old one back |
| Restart | the new EXE starts, waits until the old one has quit, replaces the bundled modules (backup as above) |

Anything failing leaves the installed app as it was and says why ("checksum
mismatch: the download is not the published file", "no write access to the
program folder"). Should the new version not start, rename
`OrpheusDL_GUI.old.exe` back to `OrpheusDL_GUI.exe`; it stays until the next
update. macOS and Linux builds only point to the release page. The update log
is `update/update.log` — versions and steps, no paths.

**Versions.** The release build writes its tag (`v2.0.3-hires.N`) into the app
(`hires/app_version.py`); releases are compared by the numbers, so `hires.10`
is newer than `hires.9`. A build without a tag is a development build and never
checks. For tests, `ORPHEUSDL_UPDATE_TEST_SERVER=http://127.0.0.1:<port>`
points the check at a local stand-in — loopback only, downloads only from that
same server.

**Releasing.** Push a tag `v2.0.3-hires.N`: the build bakes the version in and
attaches the ZIP; GitHub adds the SHA-256 `digest` to the asset by itself.

---

## Help markers and recommendations

Every setting on *Settings → Global* and on the Spotify, TIDAL, YouTube and
SoundCloud tabs has a **?** behind its label; hovering it explains the
setting and recommends a value for DJ use. Buttons of the hi-res tabs carry
the same kind of text as a tooltip. Both work by keyboard too: every "?" and
every control with help is a Tab stop with a visible ring, shows its text when
it gets the focus, and Enter or Space presses a button. The texts live in `hires/help_texts.py`;
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
| `audio_convert.py`, `audio_tags.py` | Target format: probing, conversion with checks, tags and cover |
| `download_finish.py` | After every download: actual quality, target format, `.m3u` fix-up |
| `convert_job.py`, `convert_panel.py` | "Convert folder…": worker job and its dialog |
| `integration.py` | The bridge into `gui.py` |
| `app_version.py`, `update_check.py` | Version of the build; release check against GitHub |
| `self_update.py` | Download, checks, EXE swap, modules, `--install-into` (script and app) |
| `update_flow.py`, `update_panel.py` | In-app update: steps and waiting, the dialog |

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
