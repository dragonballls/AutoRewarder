"""Clear the Windows "downloaded from the Internet" marker on bundled files.

Windows attaches a ``Zone.Identifier`` alternate data stream to files that
arrive through a browser or from a ZIP carrying the Internet zone marker, and
it propagates that mark to every extracted file. The .NET Framework then
refuses to load a marked managed assembly, so the frozen app died at startup::

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
    from ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

and, once that one was unblocked, again on WebView2::

    Could not load file or assembly
    '...\\_internal\\webview\\lib\\Microsoft.Web.WebView2.Core.dll'
    ... An attempt was made to load an assembly from a network location

That is the ordinary path for a downloaded release: the user extracts
``AutoRewarder-Ollama-Windows.zip`` with Explorer and double-clicks the exe.
Nothing is corrupt — the same build runs once its files are unblocked, and a
locally built copy has no mark at all.

Listing the DLLs to unblock by hand does not hold: the bundle carries dozens
of assemblies, and which ones the CLR loads depends on the platform and the
UI backend. Every file in the bundle is cleared instead, so any assembly the
CLR reaches for has already lost the mark.
"""

import os
import sys

_ZONE_STREAM = "Zone.Identifier"


def _bundle_root():
    """Directory holding the app's own files, frozen or running from source."""
    if getattr(sys, "frozen", False):
        # Onedir layout: the exe sits beside _internal/. Walking the exe's own
        # directory covers both the exe and the bundled data, which matters
        # because a marked *exe* fails the same way a marked DLL does.
        roots = [os.path.dirname(os.path.abspath(sys.executable))]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and meipass not in roots:
            roots.append(meipass)
        return roots[0] if len(roots) == 1 else roots
    # Source checkout: src/clr_support.py -> project root.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _roots(root=None):
    """Every directory that must be swept for the Internet marker."""
    if root is not None:
        return [root] if isinstance(root, str) else list(root)
    found = _bundle_root()
    return [found] if isinstance(found, str) else list(found)


def has_zone_marker(path):
    """True when Windows has marked path as downloaded from the Internet."""
    if sys.platform != "win32" or not os.path.isfile(path):
        return False
    try:
        with open(path + ":" + _ZONE_STREAM, "rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _clear_zone_marker(path):
    """Remove path's Zone.Identifier stream. True when one was found."""
    if not has_zone_marker(path):
        return False
    try:
        os.remove(path + ":" + _ZONE_STREAM)
        return True
    except OSError:
        # A read-only install location. Report the file as still marked so the
        # caller can say so, instead of silently pretending it was fixed.
        return False


def unblocked_paths(root=None):
    """Paths under the given roots that still carry the Internet marker."""
    if sys.platform != "win32":
        return []
    marked = []
    for one_root in _roots(root):
        if not os.path.isdir(one_root):
            continue
        for directory, _dirs, files in os.walk(one_root):
            for name in files:
                path = os.path.join(directory, name)
                if has_zone_marker(path):
                    marked.append(path)
    return marked


def unblock_bundled_files(root=None):
    """Remove the Internet marker from every file the app loads.

    Returns:
        tuple: (cleared, still_marked) — paths that were unblocked, and paths
        that are marked and could not be cleared (a read-only install). Both
        are empty for a local build or on another platform, where nothing is
        ever marked.
    """
    if sys.platform != "win32":
        return [], []

    cleared = []
    still_marked = []
    for path in unblocked_paths(root):
        if _clear_zone_marker(path):
            cleared.append(path)
        else:
            still_marked.append(path)
    return cleared, still_marked


def describe_unblock(cleared, still_marked):
    """One log line for what the unblocking pass did, or None when it did nothing."""
    if cleared:
        message = (
            "Windows had marked this copy as downloaded from the Internet, so "
            f".NET could not load its own assemblies. Unblocked {len(cleared)} "
            "file(s) so the app can start."
        )
        if still_marked:
            message += (
                f" {len(still_marked)} file(s) could not be unblocked — move the "
                "folder somewhere writable, or right-click the ZIP, choose "
                "Properties and tick Unblock."
            )
        return message
    if still_marked:
        return (
            f"{len(still_marked)} bundled file(s) are marked as downloaded and "
            "could not be unblocked. Move the folder somewhere writable, or "
            "right-click the ZIP, choose Properties and tick Unblock."
        )
    return None


def ensure_managed_runtime_loadable(logger=None):
    """Unblock the bundle before anything imports ``webview``/``clr``.

    Returns:
        list: paths that were unblocked, for the caller to report.
    """
    cleared, still_marked = unblock_bundled_files()
    if logger is not None:
        message = describe_unblock(cleared, still_marked)
        if message:
            logger(message)
    return cleared
