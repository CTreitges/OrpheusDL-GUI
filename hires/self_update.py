"""Install a new release into the one-folder app -- shared by script and app.

Two ways in, one code path:

* **Update script** -- ``Update OrpheusDL.cmd`` from a new release ZIP runs the
  *new* EXE with ``--install-into <old folder>`` (:func:`run_cli`). The new
  EXE copies itself over the old one and refreshes the bundled modules from
  its own bundle.
* **Auto-update** -- the running app downloads the release ZIP
  (:func:`download`), checks its SHA-256 against GitHub's ``digest``, takes the
  EXE out of it (:func:`extract_exe`), swaps it in (:func:`swap_exe`) and
  restarts (:func:`restart`). The new version refreshes its modules on start
  (:func:`refresh_modules_on_start`).

What this module may write in the app folder, and nothing else:
``OrpheusDL_GUI.exe``, ``OrpheusDL_GUI.old.exe`` (the previous EXE, kept as a
fallback), ``OrpheusDL_GUI.new.exe`` (staging), ``modules/<bundled name>``,
``modules/.bundled-version``, ``modules.backup-<version>/`` and ``update/``
(download, state, log). ``config/``, ``downloads/`` and ``extensions/`` are
never opened for writing, moved or copied.

Windows lets a running EXE be renamed but not overwritten; that is why the
swap renames the running EXE to ``.old.exe`` first and then moves the new one
into its place.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from . import update_check as uc
from .app_version import short_label

EXE_NAME = "OrpheusDL_GUI.exe"
OLD_EXE = "OrpheusDL_GUI.old.exe"
NEW_EXE = "OrpheusDL_GUI.new.exe"
UPDATE_DIR = "update"
LOG_NAME = "update.log"
STATE_NAME = "state.json"
MODULES = "modules"
MARKER = ".bundled-version"
MUTEX_NAME = "OrpheusDL-GUI-Mutex"
#: Folders that hold the user's data. Never written; tests hash them.
PROTECTED = ("config", "downloads", "extensions")
MAX_EXE_BYTES = 1 << 30
CHUNK = 256 * 1024

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


class UpdateError(Exception):
    """An update step failed. The message is for the user and holds no paths."""


# -- small helpers ------------------------------------------------------------

def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def update_dir(app_dir: str) -> str:
    return os.path.join(app_dir, UPDATE_DIR)


def state_path(app_dir: str) -> str:
    return os.path.join(update_dir(app_dir), STATE_NAME)


def log(app_dir: str, line: str) -> None:
    """Append a line to ``update/update.log``. Versions and steps only."""
    try:
        os.makedirs(update_dir(app_dir), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(update_dir(app_dir), LOG_NAME), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {line}\n")
    except OSError:
        pass


def write_problem(folder: str) -> Optional[str]:
    """Why ``folder`` cannot take new files, or ``None`` if it can (N4)."""
    probe = os.path.join(folder, f".update-probe-{os.getpid()}")
    try:
        with open(probe, "wb") as handle:
            handle.write(b"x")
    except OSError as exc:
        return f"no write access to the program folder ({type(exc).__name__})"
    finally:
        _remove(probe)
    return None


# -- download -----------------------------------------------------------------

def download(
    url: str,
    dest: str,
    *,
    size: int,
    sha256: str,
    channel: uc.Channel,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    opener: Optional[urllib.request.OpenerDirector] = None,
    timeout: float = 30.0,
) -> str:
    """Fetch ``url`` to ``dest`` and prove it is the file GitHub announced.

    Written to ``dest + '.part'`` first; only a complete file with the right
    size and SHA-256 is renamed to ``dest``. Anything else removes the part
    file and raises :class:`UpdateError` (N2).
    """
    if not uc.url_allowed(url, channel):
        raise UpdateError("the download address is not this app's release page")
    if not sha256 or size <= 0:
        raise UpdateError("the release has no checksum to verify against")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    opener = opener or uc.make_opener(channel)
    request = urllib.request.Request(url, headers={"User-Agent": uc.USER_AGENT})
    digest = hashlib.sha256()
    received = 0
    try:
        with opener.open(request, timeout=timeout) as response, open(part, "wb") as out:
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateError("download cancelled")
                block = response.read(CHUNK)
                if not block:
                    break
                received += len(block)
                if received > size:
                    raise UpdateError("the download is larger than announced")
                digest.update(block)
                out.write(block)
                if progress is not None:
                    progress(received, size)
        if received != size:
            raise UpdateError("the download was cut off")
        if digest.hexdigest() != sha256.lower():
            raise UpdateError("checksum mismatch: the download is not the published file")
        os.replace(part, dest)
        return dest
    except UpdateError:
        _remove(part)
        raise
    except urllib.error.HTTPError as exc:
        _remove(part)
        raise UpdateError(f"download failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, OSError) as exc:
        _remove(part)
        raise UpdateError(f"download failed ({type(exc).__name__})") from None


def extract_exe(zip_path: str, dest: str) -> str:
    """Take ``OrpheusDL_GUI.exe`` out of the release ZIP, nothing else (T4).

    Only an entry named exactly that, at the top of the archive, is read; a
    path inside the ZIP is never used to build a path on disk.
    """
    part = dest + ".part"
    try:
        with zipfile.ZipFile(zip_path) as archive:
            entry = next((i for i in archive.infolist() if i.filename == EXE_NAME), None)
            if entry is None:
                raise UpdateError("the release ZIP holds no OrpheusDL_GUI.exe")
            if entry.flag_bits & 0x1:
                raise UpdateError("the release ZIP is encrypted")
            if entry.file_size <= 0 or entry.file_size > MAX_EXE_BYTES:
                raise UpdateError("the EXE in the release ZIP has an implausible size")
            written = 0
            with archive.open(entry) as source, open(part, "wb") as out:
                head = source.read(2)
                if head != b"MZ":
                    raise UpdateError("the file in the release ZIP is not a Windows program")
                out.write(head)
                written = 2
                for block in iter(lambda: source.read(CHUNK), b""):
                    written += len(block)
                    if written > MAX_EXE_BYTES:
                        raise UpdateError("the EXE in the release ZIP is too large")
                    out.write(block)
        os.replace(part, dest)
        return dest
    except UpdateError:
        _remove(part)
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
        _remove(part)
        raise UpdateError(f"the release ZIP is damaged ({type(exc).__name__})") from None


# -- swapping the EXE ---------------------------------------------------------

def swap_exe(app_dir: str, new_exe: str, *, exe_name: str = EXE_NAME) -> None:
    """Put ``new_exe`` in place of ``app_dir/exe_name``; keep the old one (T7).

    1. the new EXE is copied to ``OrpheusDL_GUI.new.exe`` in the app folder
       (same drive, so the last step is a rename), unless it already is that;
    2. a previous ``.old.exe`` is removed;
    3. the current EXE -- possibly the running one -- is renamed to ``.old.exe``;
    4. the new EXE is renamed into its place. If that fails, step 3 is undone.
    """
    target = os.path.join(app_dir, exe_name)
    old = os.path.join(app_dir, OLD_EXE)
    staged = os.path.join(app_dir, NEW_EXE)
    if not os.path.isfile(target):
        raise UpdateError(f"{exe_name} is missing in the program folder")
    problem = write_problem(app_dir)
    if problem:
        raise UpdateError(problem)
    if os.path.abspath(new_exe) != os.path.abspath(staged):
        try:
            shutil.copyfile(new_exe, staged)
        except OSError as exc:
            _remove(staged)
            raise UpdateError(f"could not copy the new EXE ({type(exc).__name__})") from None
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        _remove(staged)
        raise UpdateError("the previous backup OrpheusDL_GUI.old.exe is in use") from None
    try:
        os.rename(target, old)
    except OSError as exc:
        _remove(staged)
        raise UpdateError(f"the current EXE cannot be moved aside ({type(exc).__name__})") from None
    try:
        os.rename(staged, target)
    except OSError as exc:
        try:
            os.rename(old, target)
        except OSError:
            # Neither EXE is at its place now. Keep both and say how to fix it.
            raise UpdateError(
                f"the new EXE could not be put in place ({type(exc).__name__}); the previous "
                f"program is {OLD_EXE} and the new one {NEW_EXE} -- rename one of them to {EXE_NAME}"
            ) from None
        _remove(staged)
        raise UpdateError(f"the new EXE could not be put in place ({type(exc).__name__})") from None


# -- modules ------------------------------------------------------------------

@dataclass
class ModuleRefresh:
    replaced: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    backup_dir: str = ""


def read_marker(app_dir: str) -> str:
    try:
        with open(os.path.join(app_dir, MODULES, MARKER), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _backup_dir(app_dir: str, label: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in label) or "previous"
    base = os.path.join(app_dir, f"modules.backup-{safe}")
    path, n = base, 2
    while os.path.exists(path):
        path, n = f"{base}-{n}", n + 1
    return path


def refresh_modules(app_dir: str, bundled: str, new_version: str) -> ModuleRefresh:
    """Replace every bundled module in ``app_dir/modules`` with the new one.

    Old copies move to ``modules.backup-<old version>/``; modules the bundle
    does not ship (the user's own) are left alone. A module that fails to copy
    is put back from the backup before the error is raised.
    """
    result = ModuleRefresh()
    if not os.path.isdir(bundled):
        return result
    names = sorted(d for d in os.listdir(bundled) if os.path.isdir(os.path.join(bundled, d)))
    if not names:
        return result
    modules = os.path.join(app_dir, MODULES)
    os.makedirs(modules, exist_ok=True)
    old_label = read_marker(app_dir) or f"before-{short_label(new_version)}"
    backup = ""
    for name in names:
        dst = os.path.join(modules, name)
        moved = ""
        if os.path.exists(dst):
            if not backup:
                backup = _backup_dir(app_dir, old_label)
                os.makedirs(backup)
            moved = os.path.join(backup, name)
            try:
                shutil.move(dst, moved)
            except OSError as exc:
                raise UpdateError(f"module {name} is in use ({type(exc).__name__})") from None
        try:
            shutil.copytree(os.path.join(bundled, name), dst)
        except OSError as exc:
            shutil.rmtree(dst, ignore_errors=True)
            if moved:
                shutil.move(moved, dst)
            raise UpdateError(f"module {name} could not be copied ({type(exc).__name__})") from None
        (result.replaced if moved else result.added).append(name)
    with open(os.path.join(modules, MARKER), "w", encoding="utf-8") as handle:
        handle.write(new_version)
    result.backup_dir = backup
    return result


def refresh_modules_on_start(app_dir: str, bundle_dir: Optional[str], version: Optional[str]) -> Optional[ModuleRefresh]:
    """On the first start of a new version: refresh the bundled modules.

    Does nothing for a development build, without a bundle, or when the
    modules already carry this version. Never raises -- a failed refresh
    leaves the old modules, which still work, and logs why.
    """
    if not version or not bundle_dir:
        return None
    bundled = os.path.join(bundle_dir, MODULES)
    if not os.path.isdir(bundled) or read_marker(app_dir) == version:
        return None
    try:
        result = refresh_modules(app_dir, bundled, version)
    except UpdateError as exc:
        log(app_dir, f"modules for {version} not refreshed: {exc}")
        return None
    except OSError as exc:  # its text names paths -- the log does not
        log(app_dir, f"modules for {version} not refreshed ({type(exc).__name__})")
        return None
    log(
        app_dir,
        f"modules for {version}: replaced {', '.join(result.replaced) or '-'}; "
        f"added {', '.join(result.added) or '-'}; backup {os.path.basename(result.backup_dir) or '-'}",
    )
    return result


def cleanup_leftovers(app_dir: str) -> None:
    """Remove what an interrupted update left behind (staged EXE, part files).

    A staged EXE is only removed while the program itself is in place: after a
    crash between the two renames it may be the only complete new program.
    """
    if os.path.isfile(os.path.join(app_dir, EXE_NAME)):
        _remove(os.path.join(app_dir, NEW_EXE))
    folder = update_dir(app_dir)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.endswith(".part") or name.endswith(".zip"):
            _remove(os.path.join(folder, name))


# -- auto-update: download, stage, swap, restart -------------------------------

def stage_release(
    app_dir: str,
    release: uc.ReleaseInfo,
    channel: uc.Channel,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> str:
    """Download and verify the release, leave its EXE as ``.new.exe``.

    Nothing of the installed app is touched here; on any failure the download
    and the staged EXE are removed again (N2).
    """
    if not release.installable:
        raise UpdateError("this release cannot be installed automatically")
    problem = write_problem(app_dir)
    if problem:
        raise UpdateError(problem)
    zip_path = os.path.join(update_dir(app_dir), f"{release.tag}.zip")
    staged = os.path.join(app_dir, NEW_EXE)
    try:
        download(
            release.asset_url, zip_path, size=release.size, sha256=release.sha256,
            channel=channel, progress=progress, cancelled=cancelled,
        )
        extract_exe(zip_path, staged)
    except UpdateError:
        _remove(staged)
        raise
    finally:
        _remove(zip_path)
    return staged


def independent_env() -> dict:
    """Environment for an EXE that must outlive this process.

    A one-file PyInstaller app passes its private variables (_PYI_*) to its
    children; a child started from them re-uses this instance's temp folder,
    which is deleted when this process ends -- the restarted EXE died with
    "Fatal Error". PYINSTALLER_RESET_ENVIRONMENT=1 makes it a new instance.
    Context7: PyInstaller (/pyinstaller/pyinstaller) >= 6.10, "Implementing
    application restart".
    """
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}


def restart(app_dir: str, exe_name: str = EXE_NAME, popen: Callable = subprocess.Popen):
    """Start the (new) EXE, telling it to wait until this process has gone."""
    exe = os.path.join(app_dir, exe_name)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return popen([exe, "--after-update"], cwd=app_dir, close_fds=True, creationflags=flags,
                 env=independent_env())


# -- single instance ----------------------------------------------------------

def mutex_exists(name: str = MUTEX_NAME) -> bool:
    """True while an OrpheusDL instance holds its single-instance mutex."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def wait_for_previous_instance(argv: Sequence[str], *, timeout: float = 60.0,
                               exists: Callable[[], bool] = mutex_exists,
                               sleep: Callable[[float], None] = time.sleep) -> bool:
    """After a restart: wait until the old process released the mutex.

    Only with ``--after-update`` in ``argv``; returns True when it waited.
    """
    if "--after-update" not in argv:
        return False
    deadline = time.monotonic() + timeout
    while exists() and time.monotonic() < deadline:
        sleep(0.25)
    return True


# -- update script: --install-into --------------------------------------------

@dataclass
class InstallResult:
    code: int
    message: str


def install_into(
    target_dir: str,
    *,
    own_exe: str,
    bundle_dir: Optional[str],
    new_version: Optional[str],
    is_running: Callable[[], bool] = mutex_exists,
) -> InstallResult:
    """Install the running (new) EXE and its modules into ``target_dir``.

    Refuses without changing anything when the folder is not an OrpheusDL
    folder or the app runs (N6). If the target already has this exact EXE --
    the ZIP was unpacked over the old folder -- only the modules are refreshed.
    """
    target_dir = os.path.abspath(target_dir)
    version = new_version or "this version"
    if not (os.path.isfile(os.path.join(target_dir, EXE_NAME))
            and os.path.isdir(os.path.join(target_dir, "config"))):
        return InstallResult(
            EXIT_REFUSED,
            "No OrpheusDL folder found here. Put the new folder INTO your old "
            "OrpheusDL folder (the one with OrpheusDL_GUI.exe and config) and run "
            "the update again. Nothing was changed.",
        )
    if is_running():
        return InstallResult(
            EXIT_REFUSED,
            "OrpheusDL is still running. Close it (also from the tray), then run "
            "the update again. Nothing was changed.",
        )
    old_label = read_marker(target_dir) or "previous version"
    target_exe = os.path.join(target_dir, EXE_NAME)
    same_exe = os.path.abspath(own_exe) == target_exe or sha256_file(own_exe) == sha256_file(target_exe)
    swapped = False
    try:
        if not same_exe:
            swap_exe(target_dir, own_exe)
            swapped = True
        bundled = os.path.join(bundle_dir, MODULES) if bundle_dir else ""
        if new_version and read_marker(target_dir) != new_version:
            modules = refresh_modules(target_dir, bundled, new_version)
        else:
            modules = ModuleRefresh()
    except (UpdateError, OSError) as exc:
        reason = str(exc) if isinstance(exc, UpdateError) else type(exc).__name__  # no paths
        log(target_dir, f"script update to {version} failed: {reason}")
        if swapped:
            return InstallResult(
                EXIT_FAILED,
                f"The program is updated, but its modules are not ({reason}). Close everything "
                "and run the update again. Your settings are untouched.",
            )
        return InstallResult(EXIT_FAILED, f"Update failed: {reason}. Your settings are untouched.")
    if same_exe and not modules.replaced and not modules.added:
        log(target_dir, f"script update: already {version}")
        return InstallResult(EXIT_OK, f"OrpheusDL is already {short_label(version)}. Nothing to do.")
    log(
        target_dir,
        f"script update {old_label} -> {version}: exe {'kept' if same_exe else 'replaced'}, "
        f"modules replaced {', '.join(modules.replaced) or '-'}, "
        f"backup {os.path.basename(modules.backup_dir) or '-'}",
    )
    message = f"Updated {short_label(old_label)} -> {short_label(version)}."
    if not same_exe:
        message += f" The previous EXE is kept as {OLD_EXE}."
    return InstallResult(EXIT_OK, message + " Settings, logins and queue are unchanged.")


def _message_box(text: str, error: bool) -> None:
    if os.name != "nt":
        print(text)
        return
    try:
        import ctypes

        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, "Update OrpheusDL", flags | 0x10000)
    except Exception:
        pass


def run_cli(argv: Sequence[str], *, own_exe: Optional[str] = None,
            bundle_dir: Optional[str] = None, new_version: Optional[str] = None,
            show: Callable[[str, bool], None] = _message_box,
            popen: Callable = subprocess.Popen) -> Optional[int]:
    """Handle ``--install-into <folder>``; ``None`` when not asked to.

    ``--quiet`` skips the message window, ``--no-start`` does not start the
    updated app afterwards. Called by gui.py before anything else happens, so
    the installer never creates a ``config`` folder next to itself.
    """
    args = list(argv[1:])
    if "--install-into" not in args:
        return None
    index = args.index("--install-into")
    if index + 1 >= len(args):
        show("Update OrpheusDL: no target folder given.", True)
        return EXIT_REFUSED
    target = args[index + 1]
    own_exe = own_exe or sys.executable
    if bundle_dir is None:
        bundle_dir = getattr(sys, "_MEIPASS", None)
    if new_version is None:
        from .app_version import current_version
        new_version = current_version()
    try:
        result = install_into(target, own_exe=own_exe, bundle_dir=bundle_dir, new_version=new_version)
    except Exception as exc:  # never fall through into starting the app here
        result = InstallResult(EXIT_FAILED, f"Update failed ({type(exc).__name__}). Your settings are untouched.")
    if "--quiet" not in args:
        show(result.message, result.code != EXIT_OK)
    if result.code == EXIT_OK and "--no-start" not in args:
        exe = os.path.join(os.path.abspath(target), EXE_NAME)
        try:
            flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
            popen([exe], cwd=os.path.abspath(target), close_fds=True, creationflags=flags,
                  env=independent_env())
        except OSError:
            pass
    return result.code
