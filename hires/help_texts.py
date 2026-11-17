"""Help texts behind every "?" -- what a setting does and what to pick.

Tkinter-free on purpose: ``tests/test_help_coverage.py`` reads the setting keys
out of gui.py and checks this table against them, so a new setting without a
help text fails the suite instead of shipping unexplained.

Keys:

* ``general.quality`` and friends -- the Global settings tab, same keys as the
  ``tooltip_texts`` table in gui.py. Where gui.py already explains a setting,
  that text is shown and this table only adds the recommendation.
* ``credentials.<Service>.<field>`` -- the settings tab of one service.
* ``hires.<tab>.<control>`` -- buttons and fields of the hi-res tabs.

``short`` is the one-liner shown *visibly* next to a field (pauses, parallel
downloads, quality); everything else only appears on hover. The pause and
quality recommendations come from the research notes of the DJ upgrade run
(sources in HIRES.md, "Recommendations").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class HelpEntry:
    #: What the setting does. Empty when gui.py's own tooltip says it already.
    text: str = ""
    #: What to choose, ideally with the reason. Always set.
    recommendation: str = ""
    #: Visible hint next to the field. Only for the few settings where a bad
    #: value costs a ban, a failed batch or a file the DJ software rejects.
    short: str = ""


H = HelpEntry

HELP: Dict[str, HelpEntry] = {
    # -- Global: general -----------------------------------------------------
    "general.output_path": H(
        recommendation="A local drive, not a cloud-synced folder: syncing while files are written can lock or duplicate them.",
    ),
    "general.quality": H(
        recommendation=(
            "For DJ use pick lossless (16-bit/44.1 kHz FLAC): CD quality, plays on every club player and "
            "takes a third of the space of 24/96. Pick hifi only for CDJ-3000, CDJ-2000NXS2 or Engine DJ "
            "players — XDJ-RX3/XDJ-XZ stop at 48 kHz, and no Pioneer player plays 192 kHz files."
        ),
        short="DJ use: lossless (16/44.1) plays everywhere; hifi only for CDJ-3000/NXS2-class players.",
    ),
    "general.search_limit": H(recommendation="25 is plenty; higher values make the Search tab slower."),
    "general.disabled_search_platforms": H(
        recommendation="Tick only the services you have an account for; every extra one slows an 'All' search."
    ),
    "general.concurrent_downloads": H(
        # Replaces gui.py's own tooltip, whose "4-6 for most systems" is about
        # bandwidth and contradicts the block-risk advice below.
        text="Number of tracks downloaded at the same time (1–10).",
        recommendation=(
            "3–4 for TIDAL and SoundCloud, 1–2 for Spotify and YouTube; 5 or more only for small jobs. "
            "streamrip, another TIDAL downloader, defaults to 6 at once; YouTube allows guests only about "
            "300 videos an hour, and parallel audio from one Spotify account looks unlike a real player "
            "(rule of thumb, no official limit). New installs start at 3."
        ),
        short="Recommended: 3–4 (TIDAL, SoundCloud), 1–2 (Spotify, YouTube).",
    ),
    "general.play_sound_on_finish": H(recommendation="Personal taste; on is handy for long playlists."),
    "general.minimize_to_tray": H(recommendation="On if you leave long queues running in the background."),
    "general.throttle_batch_size": H(
        recommendation=(
            "50 for long Spotify or YouTube playlists, 200 for TIDAL, 0 (off) for single albums. "
            "Rule of thumb derived from the services' hourly limits; none of them publishes a batch size."
        ),
        short="Recommended: 50 for long Spotify/YouTube playlists (200 for TIDAL), 0 = off.",
    ),
    "general.throttle_pause_seconds": H(
        recommendation=(
            "300 s for Spotify and YouTube batches, 600 s for TIDAL. 30 s is too short to take load "
            "off an hourly limit. Only used when Throttle Batch Size is above 0."
        ),
        short="Recommended: 300 s between batches (600 s for TIDAL).",
    ),
    # -- Global: artist downloading -----------------------------------------
    "artist_downloading.return_credited_albums": H(
        recommendation="Off for DJ crates: credited appearances bring in compilations you rarely want."
    ),
    "artist_downloading.separate_tracks_skip_downloaded": H(recommendation="On, so nothing is downloaded twice."),
    "artist_downloading.prefer_highest_quality_edition": H(recommendation="On; one edition per album keeps the library clean."),
    "artist_downloading.explicit_content": H(
        recommendation="prefer_explicit for clubs; non_explicit_only for radio or family events."
    ),
    # -- Global: formatting -------------------------------------------------
    "formatting.discography_format": H(recommendation="Keep the default '{name} {quality}'."),
    "formatting.album_format": H(recommendation="Keep '{artist}/{name}' so DJ software can browse by folder."),
    "formatting.playlist_format": H(recommendation="Keep '{name}': one folder per playlist matches one crate."),
    "formatting.track_filename_format": H(
        recommendation="'{artist} - {name}' reads well in every DJ browser; add {track_number} only for albums."
    ),
    "formatting.single_full_path_format": H(recommendation="Keep the default '{artist} - {name}'."),
    "formatting.metadata_separator": H(recommendation="Keep ';' — most DJ software splits multiple artists on it."),
    "formatting.filename_separator": H(recommendation="Leave empty to reuse the metadata separator."),
    "formatting.split_metadata": H(recommendation="Keep on unless your DJ software shows only the first artist."),
    "formatting.enable_zfill": H(recommendation="On, so tracks sort 01, 02 … 10 correctly."),
    "formatting.force_album_format": H(recommendation="Off; single tracks then land flat in the download folder."),
    "formatting.use_playlist_position": H(recommendation="On if you play sets in playlist order."),
    "formatting.use_album_position": H(recommendation="Off; per-disc numbering is what tag editors expect."),
    # -- Global: codecs -----------------------------------------------------
    "codecs.proprietary_codecs": H(recommendation="Off: MQA and similar formats do not play on most DJ players."),
    "codecs.spatial_codecs": H(recommendation="Off for DJ use unless you really want Dolby Atmos files."),
    "codecs.include_dolby_atmos": H(recommendation="Off: Atmos files are not playable on club players."),
    "codecs.target_format": H(
        text=(
            "Converts every finished download to WAV, FLAC or AIFF. Sample rate and bit depth stay as they "
            "arrived (never upsampled), tags and cover are copied, and the original is only replaced once "
            "the new file checked out. Dolby Atmos files are left alone. 'Convert folder…' does the same "
            "for files you already have."
        ),
        recommendation=(
            "off or FLAC for most DJ software (rekordbox, Serato, Traktor, Engine DJ read FLAC). AIFF if a "
            "player or USB export needs uncompressed files with tags; WAV only if nothing else is accepted — "
            "WAV tags are not read everywhere. A lossy source (SoundCloud, YouTube AAC/Opus) does not sound "
            "better after converting, it only gets bigger."
        ),
        short="DJ use: FLAC, or AIFF for uncompressed with tags. Lossy sources do not get better.",
    ),
    "codecs.target_keep_original": H(
        text="Keeps the downloaded file next to the converted one instead of replacing it.",
        recommendation="Off, so the library holds one file per track; on while you try a new format.",
    ),
    "hires.convert.folder": H(
        text="Converts every audio file in a folder and its sub-folders to the chosen format.",
        recommendation="Try it on a copy of one crate first; with 'Keep original' off the originals are replaced.",
    ),
    "hires.convert.cancel": H(
        text="Stops at once; the file being converted keeps its original, files already done stay converted.",
        recommendation="Safe to press at any time.",
    ),
    # -- Global: module defaults --------------------------------------------
    "module_defaults.lyrics": H(recommendation="Keep 'default'."),
    "module_defaults.covers": H(recommendation="Keep 'default'."),
    "module_defaults.credits": H(recommendation="Keep 'default'."),
    # -- Global: lyrics ------------------------------------------------------
    "lyrics.embed_lyrics": H(recommendation="Off for DJ files: lyrics bloat the tags and no player shows them."),
    "lyrics.embed_synced_lyrics": H(recommendation="Off unless you use a player that shows synced lyrics."),
    "lyrics.save_synced_lyrics": H(recommendation="Off for DJ use; the .lrc files clutter the folders."),
    # -- Global: covers ------------------------------------------------------
    "covers.embed_cover": H(recommendation="On: DJ software shows the artwork in the browser."),
    "covers.main_compression": H(recommendation="High keeps files small; artwork in DJ software is tiny anyway."),
    "covers.main_resolution": H(
        recommendation="600–1400 px. Very large embedded covers slow down library scans on some players."
    ),
    "covers.save_original_cover_size": H(recommendation="Off, so every cover respects Main Resolution."),
    "covers.save_external": H(recommendation="Off unless you keep artwork separately."),
    "covers.external_format": H(recommendation="png for quality, jpg for size."),
    "covers.external_compression": H(recommendation="low (better quality) — external files are not embedded."),
    "covers.external_resolution": H(recommendation="3000 px only if you print or show artwork large."),
    "covers.save_animated_cover": H(recommendation="Off for DJ use; no player shows them."),
    # -- Global: playlist -----------------------------------------------------
    "playlist.save_m3u": H(recommendation="On: rekordbox, Serato and Traktor can import an .m3u as a playlist."),
    "playlist.paths_m3u": H(recommendation="relative if you move the folder to a USB stick, absolute otherwise."),
    "playlist.extended_m3u": H(recommendation="On; harmless and keeps track lengths in the file."),
    "playlist.group_by_album": H(recommendation="Off for DJ crates: one flat folder per playlist is easier to import."),
    # -- Global: advanced -----------------------------------------------------
    "advanced.advanced_login_system": H(recommendation="Off unless a module's documentation asks for it."),
    "advanced.codec_conversions": H(
        recommendation="Keep the defaults. To get WAV or AIFF for DJ software, a conversion after download is safer than remapping here."
    ),
    "advanced.conversion_flags.aac.audio_bitrate": H(
        recommendation="256k or 320k. Below 256k artefacts become audible on a club system.",
        short="DJ use: 256k or more.",
    ),
    "advanced.conversion_flags.flac.compression_level": H(
        recommendation="5 (default). Higher levels only save a few percent and the sound is identical."
    ),
    "advanced.conversion_flags.mp3.setting": H(
        text="MP3 encoding: 128k–320k are constant bitrate (CBR); VBR -V0 is the highest-quality variable bitrate.",
        recommendation="320k for DJ use: constant bitrate gives exact beatgrids and seeking on every player.",
        short="DJ use: 320k (constant bitrate).",
    ),
    "advanced.conversion_flags.opus.b:a": H(
        recommendation=(
            "Avoid Opus as an output format for DJ use: rekordbox players, Engine DJ, Traktor and "
            "Serato do not list it. If you must, 192k or more."
        ),
        short="DJ use: avoid Opus — DJ players do not support it.",
    ),
    "advanced.conversion_keep_original": H(recommendation="Off, unless you want to compare both files."),
    "advanced.ffmpeg_path": H(recommendation="Leave 'ffmpeg' if the bundled or system ffmpeg is found."),
    "advanced.cover_variance_threshold": H(recommendation="Keep 8."),
    "advanced.debug_mode": H(recommendation="Off; turn on only while reproducing a problem."),
    "advanced.disable_subscription_checks": H(recommendation="Off; the check explains a failed download early."),
    "advanced.enable_undesirable_conversions": H(recommendation="Off: lossy-to-lossy conversions only make files worse."),
    "advanced.ignore_existing_files": H(recommendation="On, so a re-run of a playlist only fetches what is missing."),
    "advanced.ignore_different_artists": H(recommendation="Keep on."),
    "advanced.hide_ffmpeg_warning": H(recommendation="Off, so you notice when conversions cannot run."),
    "advanced.check_for_updates": H(
        recommendation=(
            "On: you are asked before anything is installed, and an update keeps settings, logins, "
            "queue and downloads. Without internet the check just stays silent."
        ),
    ),
    # -- SoundCloud -----------------------------------------------------------
    "credentials.SoundCloud.web_access_token": H(
        text=(
            "Your SoundCloud OAuth token. Sign in on soundcloud.com in your browser, open the developer "
            "tools (F12) → Network, reload, and copy the value after 'OAuth ' from the Authorization "
            "header of any api-v2.soundcloud.com request. Never share it."
        ),
        recommendation=(
            "Set it if you have SoundCloud Go+: only then are full-length tracks in AAC 256 kbit/s available. "
            "Without Go+ you get the free streams (AAC 160 kbit/s, measured) and Go+ tracks are skipped, because SoundCloud only serves their 30-second previews."
        ),
    ),
    # -- Spotify ---------------------------------------------------------------
    "credentials.Spotify.username": H(
        text="Your Spotify user name, used by the download module to sign in.",
        recommendation="Use a Spotify Premium account; free accounts get low bitrates.",
    ),
    "credentials.Spotify.download_pause_seconds": H(
        text="Delay in seconds between two Spotify track downloads.",
        recommendation=(
            "30–60 s between tracks, never below 15 s, and a spare account if you can. Spotify publishes "
            "no limit; the download library itself warns that accounts can be banned (rule of thumb)."
        ),
        short="Recommended: 30–60 s; never below 15 s.",
    ),
    "credentials.Spotify.client_id": H(
        text="Client ID of your own Spotify developer app (developer.spotify.com → Dashboard).",
        recommendation="Create your own app; needed for your private playlists and for exact ISRC matching.",
    ),
    "credentials.Spotify.client_secret": H(
        text="Client Secret of the same Spotify developer app.",
        recommendation="Keep it private; it is stored only in this app's settings file.",
    ),
    "credentials.Spotify.cookies_path": H(
        text="Path to a cookies.txt (Netscape format, with sp_dc) from a browser signed in to Spotify.",
        recommendation="Optional; only needed for synced lyrics, which DJ files do not need.",
    ),
    # -- TIDAL ------------------------------------------------------------------
    "credentials.TIDAL.tv_atmos_token": H(
        text="Client token of the TIDAL TV app the module signs in with (not your password).",
        recommendation="Keep the default.",
    ),
    "credentials.TIDAL.tv_atmos_secret": H(
        text="Client secret belonging to the TV token above (not your password).",
        recommendation="Keep the default.",
    ),
    "credentials.TIDAL.mobile_atmos_hires_token": H(
        text="Client token of the TIDAL mobile app used for Atmos and Hi-Res streams.",
        recommendation="Keep the default.",
    ),
    "credentials.TIDAL.mobile_hires_token": H(
        text="Client token of the TIDAL mobile app used for Hi-Res streams.",
        recommendation="Keep the default.",
    ),
    "credentials.TIDAL.enable_mobile": H(
        text="Use TIDAL mobile client credentials when possible.",
        recommendation="On; it is what gives access to Hi-Res FLAC.",
    ),
    "credentials.TIDAL.prefer_ac4": H(
        text="Choose AC-4 instead of E-AC-3 JOC for Dolby Atmos.",
        recommendation="Off: AC-4 plays on very few players.",
    ),
    "credentials.TIDAL.fix_mqa": H(
        text="Repair MQA headers after download.",
        recommendation="On; harmless for normal FLAC.",
    ),
    "credentials.TIDAL.throttle": H(
        text="Random pause of about 3–5 s between track starts and at most 60 metadata calls per minute.",
        recommendation=(
            "On — it matches the 60 requests per minute streamrip uses for TIDAL. If TIDAL keeps "
            "answering 'too many requests', pause the queue for a while instead of retrying at once."
        ),
        short="Recommended: on (about 60 requests per minute).",
    ),
    # -- YouTube ----------------------------------------------------------------
    "credentials.YouTube.cookies_path": H(
        text="Path to a cookies.txt in Netscape format from a browser signed in to YouTube.",
        recommendation=(
            "Export cookies.txt from a browser where you are signed in to YouTube (an extension such as "
            "'Get cookies.txt LOCALLY'). Re-export when downloads start asking you to sign in."
        ),
    ),
    "credentials.YouTube.download_pause_seconds": H(
        text="Delay in seconds between two YouTube track downloads.",
        recommendation=(
            "10–20 s, the pause yt-dlp's own 'sleep' preset uses; 5 s is the bare minimum. YouTube lets "
            "guests fetch only about 300 videos an hour, and a signed-in account that downloads too much "
            "can be blocked. New installs start at 10 s."
        ),
        short="Recommended: 10–20 s; 5 s is the minimum.",
    ),
    "credentials.YouTube.download_mode": H(
        text="Sequential downloads one track at a time with the pause above; Concurrent runs several at once.",
        recommendation="Sequential: parallel YouTube downloads trigger bot checks quickly.",
        short="Recommended: Sequential.",
    ),
    # -- Hi-res tabs: Queue -------------------------------------------------------
    "hires.queue.pause": H(
        text="Stops the next item from starting. A download that is running finishes.",
        recommendation="Pause before changing settings; nothing is interrupted.",
    ),
    "hires.queue.move_up": H(text="Moves the selected entry one place up.", recommendation="Use 'To front' on a playlist header to move a whole playlist."),
    "hires.queue.move_down": H(text="Moves the selected entry one place down.", recommendation="Use 'To front' on a playlist header to move a whole playlist."),
    "hires.queue.remove": H(text="Removes the selected entry from the queue.", recommendation="Select an entry first by clicking it."),
    "hires.queue.retry_failed": H(
        text="Puts every failed entry that has attempts left back in line.",
        recommendation="Wait a few minutes after a rate limit before retrying.",
    ),
    "hires.queue.clear_finished": H(text="Removes done, failed and cancelled entries.", recommendation="Use it after a long session to keep the list short."),
    "hires.queue.clear_all": H(
        text="Empties the whole queue, after asking. A track that is downloading is stopped and removed.",
        recommendation="Use 'Clear finished' if you only want to tidy up.",
    ),
    "hires.queue.group_to_front": H(
        text="Downloads this playlist next. The track that is running finishes first; the interrupted playlist continues afterwards.",
        recommendation="Use it when you need a playlist for tonight's set first.",
    ),
    "hires.queue.group_remove": H(
        text="Removes this playlist's entries, after asking. A running track of it finishes.",
        recommendation="Use 'Clear finished' if you only want to hide finished tracks.",
    ),
    "hires.queue.group_toggle": H(
        text="Click a playlist to show or hide its tracks.",
        recommendation="Hide finished playlists to keep the running one in view.",
    ),
    # -- Hi-res tabs: Accounts ----------------------------------------------------
    "hires.accounts.refresh": H(
        text="Checks every service again. Not-signed-in services are re-checked automatically up to 5 times.",
        recommendation="Press it after changing a token or cookie file in the settings.",
    ),
    "hires.accounts.sign_in": H(
        text="Starts the service's own sign-in in your browser.",
        recommendation="Sign in before queueing, so the first download does not open a browser window.",
    ),
    "hires.accounts.open_settings": H(
        text="Opens this service's settings, where its token or cookie file is entered.",
        recommendation="Press Refresh afterwards to see the new status.",
    ),
    # -- Hi-res tabs: Playlists ----------------------------------------------------
    "hires.playlists.service": H(
        text="The service whose playlists are shown: Spotify, TIDAL, YouTube or SoundCloud.",
        recommendation="TIDAL downloads in hi-res; the others at the quality set in Settings.",
    ),
    "hires.playlists.load": H(
        text="Lists your own playlists on the chosen service, where its login allows it.",
        recommendation="Sign in on the Accounts tab first; without a login, paste a link instead.",
    ),
    "hires.playlists.include_favorites": H(
        text="TIDAL only: also lists playlists you saved but do not own.",
        recommendation="On, to see every playlist you use.",
    ),
    "hires.playlists.link": H(
        text="A playlist link of the chosen service.",
        recommendation="Public playlists open without any login.",
    ),
    "hires.playlists.open_link": H(
        text="Opens the pasted playlist and shows its tracks.",
        recommendation="Check the track count against the website before queueing.",
    ),
    "hires.playlists.select_all": H(
        text="Ticks or unticks every playlist.",
        recommendation="Tick folders to queue a whole set in one go.",
    ),
    "hires.playlists.add_to_queue": H(
        text="Queues the ticked playlists, or the highlighted one, each as a whole.",
        recommendation="Each playlist becomes its own group in the queue.",
    ),
    "hires.playlists.tracks": H(
        text="Tracks of the highlighted playlist. Click, Shift+click or Ctrl+click to pick some.",
        recommendation="Pick tracks only when you do not want the whole playlist.",
    ),
    "hires.playlists.queue_tracks": H(
        text="Queues only the tracks picked in the list, as one group named after the playlist.",
        recommendation="Use it for a few tracks of a long playlist.",
    ),
    # -- Hi-res tabs: Port --------------------------------------------------------
    "hires.port.source": H(
        text="Where the playlist is read from. Nothing is changed there.",
        recommendation="Spotify gives ISRCs, which make matching near-exact.",
    ),
    "hires.port.target": H(
        text="Where every track is searched and then downloaded from.",
        recommendation="TIDAL for lossless; YouTube and SoundCloud only for tracks missing elsewhere.",
    ),
    "hires.port.swap": H(
        text="Swaps source and target.",
        recommendation="Ports never write to an account, so swapping is safe.",
    ),
    "hires.port.url": H(
        text="A playlist link of the source service.",
        recommendation="Public links work without signing in.",
    ),
    "hires.port.convert": H(
        text="Reads the linked playlist and searches every track on the target.",
        recommendation="Review uncertain matches before queueing.",
    ),
    "hires.port.my_playlists": H(
        text="Lists your own playlists on the source service.",
        recommendation="Spotify needs Client ID and Secret; SoundCloud a token; YouTube a cookie file.",
    ),
    "hires.port.queue_matches": H(
        text="Queues the confident matches as one group; uncertain ones wait for review.",
        recommendation="Tick several playlists to port and queue them one after another.",
    ),
    "hires.port.review": H(
        text="Confirm, swap or skip each uncertain match.",
        recommendation="Check live versions, remixes and re-uploads here.",
    ),
    "hires.port.select_all": H(
        text="Ticks or unticks every playlist.",
        recommendation="Untick playlists you already ported.",
    ),
    # -- About ----------------------------------------------------------------------
    "hires.about.check_for_updates": H(
        text=(
            "Asks GitHub now whether a newer version exists. If so, a window shows what changed and "
            "asks before anything is downloaded; the update is checked (SHA-256) and keeps settings, "
            "logins, queue and downloads."
        ),
        recommendation="Leave the daily check on (Settings > Advanced); use this after a release.",
    ),
}


def entry(key: str) -> Optional[HelpEntry]:
    return HELP.get(key)


def help_message(key: str, base_text: str = "") -> str:
    """Hover text for ``key``: what it does, then what to pick.

    ``base_text`` is gui.py's own tooltip for the setting, when it has one --
    shown instead of repeating it here. An entry's own ``text`` wins over it:
    that is how a gui.py tooltip that contradicts the recommendation is
    replaced rather than shown next to it.
    """
    item = HELP.get(key)
    text = ((item.text if item else "") or base_text).strip()
    recommendation = item.recommendation.strip() if item else ""
    if recommendation:
        return f"{text}\n\nRecommended: {recommendation}" if text else f"Recommended: {recommendation}"
    return text


def short_hint(key: str) -> str:
    item = HELP.get(key)
    return item.short if item else ""
