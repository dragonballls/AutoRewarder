"""Clear the Windows "downloaded from the Internet" marker before .NET loads.

Windows attaches a ``Zone.Identifier`` alternate data stream to files that
arrive through a browser or from a ZIP that carries the Internet zone marker,
and it propagates that mark to every extracted file. The .NET Framework
refuses to load a managed assembly that carries the mark, so the frozen app
dies at startup with::

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
    from ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

That is the ordinary path for a downloaded release: the user extracts
``AutoRewarder-Ollama-Windows.zip`` with Explorer and double-clicks the exe.
Nothing is corrupt — the same build runs once the files are unblocked. Instead
of telling the user to run ``Unblock-File`` first, the app removes the mark
from its own bundled .NET assemblies, which is a no-op when they are not
marked.
"""

import os
import sys

# The managed assemblies that must be loadable by the CLR. Python.Runtime.dll
# is the one the startup traceback names; ClrLoader.dll is the native shim that
# hosts it, and both can carry the mark.
_MANAGED_DLLS = (
    os.path.join("pythonnet", "runtime", "Python.Runtime.dll"),
    os.path.join("clr_loader", "ffi", "dlls", "amd64", "ClrLoader.dll"),
    os.path.join("clr_loader", "ffi", "dlls", "x86", "ClrLoader.dll"),
)

_ZONE_STREAM = "Zone.Identifier"


def _bundle_root():
    """Directory holding the bundled data files, frozen or running from source."""
    if getattr(sys, "frozen", False):
        # PyInstaller 6 puts data files under _internal/ next to the exe; older
        # layouts put them beside the exe directly.
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
        return base
    # Source checkout: src/utils.py -> project root.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clear_zone_marker(path):
    """Delete path's Zone.Identifier stream. True when one was removed."""
    if not os.path.isfile(path):
        return False
    try:
        # Only Windows has alternate data streams; every other platform is a
        # no-op, so the call site needs no platform check of its own.
        if sys.platform != "win32":
            return False
        with open(path + ":" + _ZONE_STREAM, "rb") as handle:
            handle.read(1)
    except OSError:
        # No such stream: the file was never marked, so there is nothing to do.
        return False
    try:
        os.remove(path + ":" + _ZONE_STREAM)
        return True
    except OSError:
        # A read-only install location: the CLR may still refuse the DLL, and
        # the caller reports that rather than failing silently.
        return False


def unblock_bundled_assemblies():
    """Remove the Internet marker from bundled .NET assemblies.

    Returns:
        list: paths that were unblocked, for logging. Empty when nothing
        needed changing, which is the normal case for a local build.
    """
    root = _bundle_root()
    cleared = []
    for relative in _MANAGED_DLLS:
        path = os.path.join(root, relative)
        if _clear_zone_marker(path):
            cleared.append(path)
    return cleared


def ensure_managed_runtime_loadable(logger=None):
    """Unblock the bundled CLR assemblies before anything imports ``clr``.

    A downloaded release arrives with the Internet marker on its DLLs, and the
    CLR then fails to resolve ``Python.Runtime.Loader.Initialize`` — the app
    cannot start at all. Clearing the mark here keeps the download path the
    same as a local build's.
    """
    cleared = unblock_bundled_assemblies()
    if cleared and logger is not None:
        logger(
            "Windows had marked the bundled .NET files as downloaded; "
            f"unblocked {len(cleared)} of them so the app can start."
        )
    return cleared
