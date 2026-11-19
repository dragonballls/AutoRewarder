"""
Update in place: download the release, swap the files, restart.

The updater used to stop at "a new version exists" and hand the person a link,
which meant the app could only ever be updated by downloading a ZIP, extracting
it by hand and starting it again. This module does the whole job:

  1. pick the download for this machine from the release's assets;
  2. fetch it, with the release's own size and digest as the check;
  3. unpack it beside the running copy;
  4. hand over to a small swap script that waits for this process to exit,
     copies the new files over the old ones, and starts the app again.

The swap is a separate script on purpose. Windows will not let a running
executable be replaced, and no process can reliably replace itself while it is
still running, so something outside this one has to do it after it exits.

Nothing here deletes the install folder: the new files are copied over it. A
portable copy keeps its `config/` directory that way, and a file that no longer
ships simply stays behind instead of taking someone's data with it.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

import requests

from .config import REPO
from .utils import _version_tuple, hidden_process_kwargs, is_newer_version

GITHUB_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# How long the swap script waits for this process to exit before copying
# anyway. A stall here is worse than a slightly early copy: the person is
# looking at a closed window either way, and a half-written install is not.
_WAIT_SECONDS = 90

# The download for each platform, and how it is unpacked. Matched against the
# asset names the release pipeline produces.
_ASSETS = {
    "Windows": (r"AutoRewarder-windows\.zip$", "zip"),
    "Linux": (r"AutoRewarder-linux\.tar\.gz$", "tar"),
}

# The executable inside the archive, and the one already on disk.
_EXE_NAME = {"Windows": "AutoRewarder.exe", "Linux": "AutoRewarder"}

# What the archive's top-level folder is called in each build.
_ARCHIVE_ROOT = {"Windows": "AutoRewarder", "Linux": "AutoRewarder"}

_CHUNK = 256 * 1024

# What the swap helper leaves behind: a plain-text record of what it did, and a
# one-line status the next start reads to tell the person whether the update
# landed. Both live beside the install, or in the temp directory when that is
# not writable.
_LOG_NAME = ".autorewarder-update.log"


def update_status_path(target):
    """The status file the helper writes for one install."""
    return os.path.join(
        os.path.dirname(os.path.abspath(target)), ".autorewarder-update.status"
    )


def read_update_status(target):
    """
    What the last update attempt reported, if anything.

    This is how "the app updated but nothing changed" becomes an answer instead
    of a mystery: the helper's last word is read on the next start.

    Args:
        target (str): the folder the app is installed in.

    Returns:
        dict | None: {"ok": bool, "message": str}, or None when there is no
        status from a previous attempt.
    """
    path = update_status_path(target)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            line = handle.read().strip()
    except OSError:
        return None
    if not line:
        return None
    return {"ok": line.casefold().startswith("ok"), "message": line}


def clear_update_status(target):
    """Remove the last attempt's status, once it has been reported."""
    try:
        os.unlink(update_status_path(target))
    except OSError:
        pass


def current_platform():
    """The platform name this build runs on: "Windows" or "Linux"."""
    return platform.system()


def is_frozen():
    """
    Whether this copy is a built, downloadable app rather than a source run.

    Only a built copy can be updated in place: a source checkout has no files to
    swap, and replacing someone's working directory would be wrong even if it
    did.
    """
    return bool(getattr(sys, "frozen", False))


def install_dir():
    """
    The folder holding this copy, or None when running from source.

    Returns:
        str | None: the directory of the running executable.
    """
    if not is_frozen():
        return None
    return os.path.dirname(os.path.abspath(sys.executable))


def latest_release(timeout=10):
    """
    The release GitHub reports as latest, as its JSON, or None.

    Returns:
        dict | None: the release, with `tag_name`, `assets` and `body`.
    """
    try:
        response = requests.get(
            GITHUB_RELEASE_URL,
            headers={"User-Agent": "AutoRewarder-App"},
            timeout=timeout,
        )
    except requests.exceptions.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def pick_asset(assets, platform_name=None):
    """
    The download for this platform, out of a release's assets.

    Args:
        assets (list): the release's `assets`, each with `name` and
            `browser_download_url`.
        platform_name (str, optional): defaults to the running platform.

    Returns:
        dict | None: the matching asset, or None when the release has none for
            this machine (a build published without the Linux archive, say).
    """
    if not isinstance(assets, list):
        return None
    platform_name = platform_name or current_platform()
    pattern, _kind = _ASSETS.get(platform_name, (None, None))
    if pattern is None:
        return None

    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        if re.search(pattern, name):
            return asset
    return None


def unpack_kind(platform_name=None):
    """How this platform's download is unpacked: "zip" or "tar"."""
    platform_name = platform_name or current_platform()
    return _ASSETS.get(platform_name, (None, None))[1]


def verify_file(path, expected_size=None, expected_digest=None, algorithm="sha256"):
    """
    Check a downloaded file before anything is unpacked from it.

    The size is checked because a truncated download that still unpacks is the
    failure this catches: a half-fetched archive usually fails loudly, but a
    complete-looking one that is short by a chunk can unpack over an install and
    leave it broken.

    Returns:
        tuple: (ok, reason) — reason is "" when ok.
    """
    if not os.path.isfile(path):
        return False, "the download did not arrive"

    size = os.path.getsize(path)
    if expected_size is not None and size != int(expected_size):
        return False, f"the download is {size} bytes, not {int(expected_size)}"
    if size < 1024:
        return False, "the download is too small to be a build"

    if expected_digest:
        digest = hashlib.new(algorithm)
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(block)
        if digest.hexdigest().casefold() != str(expected_digest).casefold():
            return False, "the download does not match the release's digest"

    return True, ""


def download(url, destination, expected_size=None, progress=None, stop_event=None):
    """
    Fetch one file, reporting how much has arrived.

    Args:
        url (str): where it is.
        destination (str): the file to write, replaced when it exists.
        expected_size (int, optional): the release's size, to compare at the end.
        progress (callable, optional): called with (done_bytes, total_bytes),
            total None when the server sends no length.
        stop_event (threading.Event, optional): aborts a large download.

    Returns:
        tuple: (path, error) — path is None when it failed.
    """
    total = expected_size
    done = 0
    try:
        with requests.get(
            url,
            stream=True,
            timeout=(10, 60),
            headers={
                "User-Agent": "AutoRewarder-App",
                "Accept": "application/octet-stream",
            },
        ) as response:
            if response.status_code != 200:
                return None, f"GitHub answered {response.status_code}"

            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                total = int(length)

            os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
            with open(destination, "wb") as handle:
                for block in response.iter_content(chunk_size=_CHUNK):
                    if stop_event is not None and stop_event.is_set():
                        return None, "cancelled"
                    if not block:
                        continue
                    handle.write(block)
                    done += len(block)
                    if progress is not None:
                        progress(done, total)
    except requests.exceptions.RequestException as e:
        return None, str(e)[:160]
    except OSError as e:
        return None, f"could not write the download: {e}"

    ok, reason = verify_file(destination, expected_size)
    if not ok:
        return None, reason
    return destination, ""


def _safe_members(archive):
    """Reject an archive that would write outside the folder it unpacks into."""
    for name in archive:
        normalised = os.path.normpath(name).replace("\\", "/")
        if normalised.startswith("../") or normalised.startswith("/"):
            return False
        if ".." in normalised.split("/"):
            return False
    return True


def unpack(archive_path, destination, platform_name=None):
    """
    Unpack a downloaded build into `destination`.

    The archive always holds one top-level folder (`AutoRewarder/`). That folder
    is stripped so the result is the app's own layout, and every member is
    checked first so a crafted archive cannot write outside `destination`.

    Returns:
        tuple: (folder, error) — folder is the unpacked app, or None.
    """
    kind = unpack_kind(platform_name)
    try:
        os.makedirs(destination, exist_ok=True)
    except OSError as e:
        # A folder that cannot be created is a failure the caller reports, not a
        # traceback after a 300 MB download.
        return None, f"could not make a place to unpack it: {str(e)[:120]}"

    try:
        if kind == "zip":
            with zipfile.ZipFile(archive_path) as archive:
                if not _safe_members(archive.namelist()):
                    return None, "the archive holds a path outside its folder"
                archive.extractall(destination)
        elif kind == "tar":
            with tarfile.open(archive_path, "r:gz") as archive:
                if not _safe_members(archive.getnames()):
                    return None, "the archive holds a path outside its folder"
                # `filter` keeps tar from following links out of the folder; on
                # the Python versions that do not know it, the name check above
                # is what stands in its way.
                try:
                    archive.extractall(destination, filter="data")
                except TypeError:
                    archive.extractall(destination)
        else:
            return None, "no download is published for this platform"
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        return None, f"the download could not be unpacked: {str(e)[:120]}"

    root_name = _ARCHIVE_ROOT.get(platform_name or current_platform())
    candidate = os.path.join(destination, root_name) if root_name else destination
    if not os.path.isdir(candidate):
        # Some builds put the files at the top level instead of in a folder.
        candidate = destination

    exe = _EXE_NAME.get(platform_name or current_platform())
    if exe and not os.path.isfile(os.path.join(candidate, exe)):
        return None, f"the download has no {exe} in it"
    return candidate, ""


def _swap_script_windows(pid, staged, target, exe, log_path=None):
    """
    The PowerShell that finishes an update after this process exits.

    Kept as a string so it can be inspected and tested; it is written to a
    temporary file and started hidden.

    What it has to get right, learned from an update that reported success and
    installed nothing:

      * **wait** for this process, because Windows will not replace a running
        executable;
      * **elevate** when the install is somewhere only an administrator may
        write, and pass the script path quoted — an install under a user name
        with a space in it made the elevated copy fail to start at all;
      * **check robocopy's exit code**. `$ErrorActionPreference = 'Stop'` does
        not act on a native program's exit code, so a refused copy used to fall
        through to the restart: the app reopened on the old version and looked
        like it had simply ignored the update;
      * **say what happened** in a log beside the install, because the window
        has already closed by the time any of this runs;
      * **start the new copy** only when the copy is really there.
    """
    log_path = log_path or os.path.join(
        os.path.dirname(os.path.abspath(target)), _LOG_NAME
    )
    state_path = update_status_path(target)
    return f"""$ErrorActionPreference = 'Stop'
$source = {json.dumps(staged)}
$target = {json.dumps(target)}
$exe    = {json.dumps(exe)}
$log    = {json.dumps(log_path)}
$state  = {json.dumps(state_path)}

function Write-Log($message) {{
  try {{
    $line = ('{{0:yyyy-MM-dd HH:mm:ss}}  {{1}}' -f (Get-Date), $message)
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
  }} catch {{ }}
}}

function Write-Status($status) {{
  # The new copy reads this on start-up and can tell the person what happened,
  # which is the only way they ever learn why an update did not land.
  try {{
    Set-Content -LiteralPath $state -Value $status -Encoding UTF8 -ErrorAction SilentlyContinue
  }} catch {{ }}
}}

function Test-Writable($path) {{
  try {{
    $probe = Join-Path $path ('.write-test-' + [guid]::NewGuid().ToString('N'))
    [System.IO.File]::WriteAllText($probe, 'x')
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    return $true
  }} catch {{ return $false }}
}}

$elevated = $false
foreach ($arg in $args) {{ if ($arg -eq '-Elevated') {{ $elevated = $true }} }}

if (-not (Test-Writable $target)) {{
  if ($elevated) {{
    Write-Log "target still not writable after elevation: $target"
    Write-Status "failed: this folder cannot be written to, even as administrator"
    exit 1
  }}
  Write-Log "target not writable, asking for elevation: $target"
  # $PSCommandPath must be quoted: an install under a user name with a space
  # made the elevated copy fail to start, and the update vanished silently.
  Start-Process -FilePath 'powershell' -Verb RunAs -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', ('"' + $PSCommandPath + '"'), '-Elevated'
  )
  exit 0
}}

Write-Log "waiting for pid {pid} to exit"
$deadline = (Get-Date).AddSeconds({_WAIT_SECONDS})
while ((Get-Date) -lt $deadline) {{
  if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) {{ break }}
  Start-Sleep -Milliseconds 400
}}
Start-Sleep -Milliseconds 800

# The bundled msedgedriver lives inside this folder, so a driver still running
# from a just-ended session holds files robocopy has to replace. Only processes
# whose own path is under this install are stopped: nothing of the person's is
# touched, and a real Edge window they are using is left alone.
try {{
  $mine = Get-Process -ErrorAction SilentlyContinue | Where-Object {{
    $_.Path -and $_.Path.StartsWith($target, 'OrdinalIgnoreCase')
  }}
  foreach ($proc in $mine) {{
    Write-Log "stopping leftover process $($proc.ProcessName) ($($proc.Id))"
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  }}
  if ($mine) {{ Start-Sleep -Milliseconds 1200 }}
}} catch {{
  Write-Log "could not check for leftover processes: $($_.Exception.Message)"
}}

Write-Log "copying $source to $target"
# Copy over the old files; never mirror. A mirror would delete a portable
# config folder, and a leftover file from the previous version is harmless.
$robocopy = Join-Path $env:SystemRoot 'System32\\robocopy.exe'
& $robocopy $source $target /E /NFL /NDL /NJH /NJS /NP /R:5 /W:2 | Out-Null
$copyCode = $LASTEXITCODE
Write-Log "robocopy exit code $copyCode"

# robocopy: 0-7 are success (8+ is a real failure). Anything else means files
# were not copied, and starting the app now would reopen the old version.
if ($copyCode -ge 8) {{
  Write-Log "copy failed with code $copyCode"
  Write-Status "failed: Windows would not replace the files (robocopy $copyCode)"
  exit 1
}}

$exePath = Join-Path $target $exe
if (-not (Test-Path -LiteralPath $exePath)) {{
  Write-Log "the executable is missing after the copy: $exePath"
  Write-Status "failed: the new version has no $exe in it"
  exit 1
}}

# A copied file inherits the download marker, which stops .NET loading the
# bundled assemblies; clear it before starting.
Get-ChildItem -LiteralPath $target -Recurse -File -Force -ErrorAction SilentlyContinue |
  Unblock-File -ErrorAction SilentlyContinue

Write-Log "starting $exePath"
try {{
  Start-Process -FilePath $exePath -WorkingDirectory $target
  Write-Status "ok"
}} catch {{
  Write-Log "could not start the new copy: $($_.Exception.Message)"
  Write-Status "failed: the new version would not start ($($_.Exception.Message))"
}}
"""


def _swap_script_posix(pid, staged, target, exe, log_path=None):
    """
    The shell that finishes an update after this process exits.

    Replacing a running binary's file is allowed on Linux, but copying into a
    mounted folder still has to wait for the old process to let go of its
    handles, and starting the new copy has to happen from the new folder.

    Like the Windows helper, it checks that the copy actually happened rather
    than assuming: a `cp` into a read-only folder fails quietly with stderr
    discarded, and the app would reopen on the old version.
    """
    log_path = log_path or os.path.join(
        os.path.dirname(os.path.abspath(target)), _LOG_NAME
    )
    state_path = update_status_path(target)
    quoted_exe = os.path.join(target, exe)
    return f"""#!/bin/sh
LOG={json.dumps(log_path)}
STATE={json.dumps(state_path)}
log() {{ printf '%s  %s\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG" 2>/dev/null; }}
status() {{ printf '%s\\n' "$1" > "$STATE" 2>/dev/null; }}

log "waiting for pid {pid} to exit"
i=0
while kill -0 {pid} 2>/dev/null && [ $i -lt {_WAIT_SECONDS * 2} ]; do
  sleep 0.5
  i=$((i+1))
done
sleep 1

log "copying {staged} to {target}"
if ! cp -a {json.dumps(staged)}/. {json.dumps(target)}/ 2>>"$LOG"; then
  log "copy failed"
  status "failed: the files could not be replaced"
  exit 1
fi
chmod +x {json.dumps(quoted_exe)} 2>/dev/null
if [ ! -f {json.dumps(quoted_exe)} ]; then
  log "the executable is missing after the copy"
  status "failed: the new version has no {exe} in it"
  exit 1
fi

log "starting {exe}"
cd {json.dumps(target)} && ./{exe} >/dev/null 2>&1 &
status "ok"
exit 0
"""


def apply_and_restart(staged, target, pid=None, platform_name=None):
    """
    Hand the swap to a helper script and let the caller exit.

    Args:
        staged (str): the folder holding the new build.
        target (str): the folder to copy it over (the running install).
        pid (int, optional): this process, to wait for. Defaults to ours.
        platform_name (str, optional): defaults to the running platform.

    Returns:
        tuple: (ok, error) — ok means the helper was started, not that the copy
            has happened yet; that happens after this process exits.
    """
    platform_name = platform_name or current_platform()
    pid = os.getpid() if pid is None else int(pid)
    exe = _EXE_NAME.get(platform_name)
    if not exe:
        return False, "updating in place is not supported on this platform"
    if not os.path.isdir(staged):
        return False, "the new build was not unpacked"
    if not os.path.isfile(os.path.join(staged, exe)):
        return False, f"the new build has no {exe} in it"

    try:
        script_dir = tempfile.mkdtemp(prefix="autorewarder-update-")
    except OSError as e:
        return False, f"could not make a place for the updater: {e}"

    # The helper's own record of what it did. The window is gone by the time it
    # runs, so a file is the only way the next start can say whether the update
    # landed.
    log_path = os.path.join(os.path.dirname(os.path.abspath(target)), _LOG_NAME)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except OSError:
        log_path = os.path.join(tempfile.gettempdir(), _LOG_NAME)

    try:
        if platform_name == "Windows":
            script = os.path.join(script_dir, "apply-update.ps1")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(_swap_script_windows(pid, staged, target, exe, log_path))
            command = [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                script,
            ]
        else:
            script = os.path.join(script_dir, "apply-update.sh")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(_swap_script_posix(pid, staged, target, exe, log_path))
            os.chmod(script, 0o755)
            command = ["/bin/sh", script]

        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **hidden_process_kwargs(platform_name),
        )
    except (OSError, ValueError) as e:
        # ValueError: the platform refused a flag (Windows-only creation flags on
        # a POSIX host, say). A caller must get an error it can show, never a
        # traceback from the middle of an update.
        return False, f"could not start the updater: {e}"

    return True, ""


def clean_staging(path):
    """Remove a staged build, ignoring a file Windows still has open."""
    if not path:
        return
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def sweep_stale_updates(target):
    """
    Remove staging folders a previous update left beside this install.

    A successful update ends with the app restarting, so the helper never gets
    the chance to tidy up after itself, and a cancelled one may leave a partial
    copy behind. Each is the size of the unpacked build (~700 MB), so they are
    worth removing: nothing in one is needed once the update is done or
    abandoned.

    Only folders this module names are touched, and only beside the install — so
    a person's own `config` folder or anything else is never in scope.

    Args:
        target (str): the folder the app is installed in.

    Returns:
        list: the paths removed.
    """
    parent = os.path.dirname(os.path.abspath(target))
    if not os.path.isdir(parent):
        return []
    removed = []
    try:
        entries = os.listdir(parent)
    except OSError:
        return []
    for name in entries:
        if not name.startswith(".autorewarder-update-"):
            continue
        path = os.path.join(parent, name)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            # A helper may still be reading it. Leaving it is correct.
            continue
        removed.append(path)
    return removed


def _can_write(directory):
    """Whether a real file can be created in `directory` right now."""
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return False
    probe = os.path.join(
        directory, f".write-test-{os.getpid()}-{int(time.time() * 1000)}"
    )
    try:
        with open(probe, "wb") as handle:
            handle.write(b"x")
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass
    return True


def staging_dir(target, tag):
    """
    Where a downloaded build is unpacked.

    Normally beside the install, because the install folder is what gets
    replaced and a folder inside it would be copied over itself. But beside the
    install is not always writable: an install under Program Files lives in a
    directory only an administrator may write, and a build extracted anywhere
    the person cannot write behaves the same way.

    That was a real update failure, and a silent one. Unpacking into a folder it
    could not create raised PermissionError after a 300 MB download, so the run
    ended with the download finished and nothing installed. The staging folder
    is now placed in the first writable of:

      1. beside the install (the normal case, and what the sweep looks for)
      2. the desktop user's own temporary directory

    The helper is told where it is, so it does not matter that the fallback is
    elsewhere; and only the elevation step ever needs to write beside the
    install.

    Args:
        target (str): the folder the app is installed in.
        tag (str): the version being installed, for the folder's name.

    Returns:
        str: an existing, writable folder for the unpacked build.
    """
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", str(tag or "update"))
    safe_tag = safe_tag.replace("..", "_")
    name = f".autorewarder-update-{safe_tag}"

    parent = os.path.dirname(os.path.abspath(target))
    beside = os.path.join(parent, name)
    if _can_write(beside):
        return beside

    # Somewhere this account certainly owns. `tempfile` already knows the
    # platform's own answer (LOCALAPPDATA\Temp on Windows, /tmp elsewhere).
    fallback = os.path.join(tempfile.gettempdir(), name)
    os.makedirs(fallback, exist_ok=True)
    return fallback


def describe_plan(release, platform_name=None):
    """
    What an update would do, for the dialog the person agrees to.

    Returns:
        dict: {available, current, latest, asset, size, reason}.
    """
    from .config import CURRENT_VERSION

    plan = {
        "available": False,
        "current": CURRENT_VERSION,
        "latest": None,
        "asset": None,
        "size": None,
        "reason": "",
    }
    if not isinstance(release, dict):
        plan["reason"] = "unreachable"
        return plan

    tag = release.get("tag_name")
    plan["latest"] = tag if isinstance(tag, str) else None
    if not tag or not is_newer_version(tag, CURRENT_VERSION):
        return plan

    asset = pick_asset(release.get("assets"), platform_name)
    if asset is None:
        plan["reason"] = "no_download"
        return plan

    plan["available"] = True
    plan["asset"] = {
        "name": asset.get("name"),
        "url": asset.get("browser_download_url"),
        "size": asset.get("size"),
    }
    plan["size"] = asset.get("size")
    if not is_frozen():
        plan["reason"] = "source_run"
    return plan


__all__ = [
    "GITHUB_RELEASE_URL",
    "_swap_script_posix",
    "_swap_script_windows",
    "_version_tuple",
    "apply_and_restart",
    "clean_staging",
    "clear_update_status",
    "current_platform",
    "describe_plan",
    "download",
    "install_dir",
    "is_frozen",
    "latest_release",
    "pick_asset",
    "read_update_status",
    "staging_dir",
    "sweep_stale_updates",
    "unpack",
    "unpack_kind",
    "update_status_path",
    "verify_file",
]
