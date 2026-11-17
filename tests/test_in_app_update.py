"""Updating in place: no download page, no installer, no hands.

The old updater ended at "a new version is available" and a link. What is tested
here is the rest of the job — picking the right download, refusing a broken one,
unpacking it, and handing the swap to a helper that outlives this process — plus
the one rule that makes it safe: nothing is replaced until the download has been
checked, and nothing is trusted from a response.
"""

import hashlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src import updater  # noqa: E402
from src.updater import (  # noqa: E402
    _swap_script_posix,
    _swap_script_windows,
    apply_and_restart,
    describe_plan,
    download,
    install_dir,
    is_frozen,
    pick_asset,
    staging_dir,
    unpack,
    unpack_kind,
    verify_file,
)


def _release(tag="v9.9", assets=None):
    return {
        "tag_name": tag,
        "assets": (
            assets
            if assets is not None
            else [
                {
                    "name": "AutoRewarder-windows.zip",
                    "browser_download_url": "https://example.test/win.zip",
                    "size": 2048,
                },
                {
                    "name": "AutoRewarder-linux.tar.gz",
                    "browser_download_url": "https://example.test/linux.tar.gz",
                    "size": 4096,
                },
            ]
        ),
    }


class AssetTests(unittest.TestCase):
    def test_each_platform_gets_its_own_download(self):
        windows = pick_asset(_release()["assets"], "Windows")
        linux = pick_asset(_release()["assets"], "Linux")
        self.assertTrue(windows["name"].endswith(".zip"))
        self.assertTrue(linux["name"].endswith(".tar.gz"))

    def test_a_release_without_this_platforms_build_reports_nothing(self):
        assets = [{"name": "AutoRewarder-windows.zip", "browser_download_url": "u"}]
        self.assertIsNone(pick_asset(assets, "Linux"))

    def test_an_unknown_platform_is_not_guessed(self):
        self.assertIsNone(pick_asset(_release()["assets"], "Plan9"))

    def test_the_unpacking_matches_the_asset(self):
        self.assertEqual(unpack_kind("Windows"), "zip")
        self.assertEqual(unpack_kind("Linux"), "tar")


class PlanTests(unittest.TestCase):
    def test_a_newer_release_is_available_with_its_download(self):
        plan = describe_plan(_release("v99.0"), "Linux")
        self.assertTrue(plan["available"])
        self.assertEqual(plan["asset"]["name"], "AutoRewarder-linux.tar.gz")

    def test_the_current_version_is_not_offered(self):
        from src.config import CURRENT_VERSION

        plan = describe_plan(_release(CURRENT_VERSION), "Windows")
        self.assertFalse(plan["available"])

    def test_an_older_release_is_never_offered(self):
        plan = describe_plan(_release("v1.0"), "Windows")
        self.assertFalse(plan["available"])

    def test_an_unreachable_check_says_so(self):
        plan = describe_plan(None, "Windows")
        self.assertFalse(plan["available"])
        self.assertEqual(plan["reason"], "unreachable")

    def test_a_release_with_no_download_for_this_platform_says_so(self):
        plan = describe_plan(
            _release(
                "v99.0",
                assets=[
                    {"name": "AutoRewarder-windows.zip", "browser_download_url": "u"}
                ],
            ),
            "Linux",
        )
        self.assertFalse(plan["available"])
        self.assertEqual(plan["reason"], "no_download")

    def test_a_source_run_is_flagged_so_the_ui_does_not_offer_the_wrong_thing(self):
        plan = describe_plan(_release("v99.0"), "Linux")
        if not is_frozen():
            self.assertEqual(plan["reason"], "source_run")
        self.assertFalse(is_frozen())
        self.assertIsNone(install_dir())


class VerifyTests(unittest.TestCase):
    def test_a_matching_size_passes(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"x" * 4096)
            path = handle.name
        self.addCleanup(os.remove, path)
        ok, reason = verify_file(path, expected_size=4096)
        self.assertTrue(ok, reason)

    def test_a_truncated_download_is_refused(self):
        """The failure worth catching: it looks like a file but is short."""
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"x" * 4096)
            path = handle.name
        self.addCleanup(os.remove, path)
        ok, reason = verify_file(path, expected_size=100000)
        self.assertFalse(ok)
        self.assertIn("not", reason)

    def test_a_tiny_file_is_refused_even_without_an_expected_size(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"nope")
            path = handle.name
        self.addCleanup(os.remove, path)
        ok, _reason = verify_file(path)
        self.assertFalse(ok)

    def test_the_release_digest_is_checked_when_a_caller_has_one(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"y" * 2048)
            path = handle.name
        self.addCleanup(os.remove, path)
        good = hashlib.sha256(b"y" * 2048).hexdigest()
        self.assertTrue(verify_file(path, expected_digest=good)[0])
        self.assertFalse(verify_file(path, expected_digest="0" * 64)[0])


class _FakeResponse:
    def __init__(self, payload, status=200, length=None):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Length": str(length)} if length else {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size=1024):
        for start in range(0, len(self._payload), chunk_size):
            end = start + chunk_size
            yield self._payload[start:end]


class DownloadTests(unittest.TestCase):
    def _download(self, payload, status=200, expected_size=None, length=None):
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        path = os.path.join(base, "download.bin")
        with mock.patch.object(
            updater.requests,
            "get",
            return_value=_FakeResponse(payload, status=status, length=length),
        ):
            result, error = download(
                "https://example.test/build",
                path,
                expected_size=expected_size,
            )
        return result, error, path

    def test_a_good_download_is_written(self):
        payload = b"z" * 4096
        result, error, path = self._download(payload, expected_size=4096)
        self.assertTrue(result, error)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload)

    def test_progress_is_reported(self):
        seen = []
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        with mock.patch.object(
            updater.requests, "get", return_value=_FakeResponse(b"z" * 4096)
        ):
            download(
                "https://example.test/build",
                os.path.join(base, "f.bin"),
                progress=lambda done, total: seen.append((done, total)),
            )
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], 4096)

    def test_a_failed_http_answer_is_not_a_download(self):
        result, error, _path = self._download(b"", status=404)
        self.assertIsNone(result)
        self.assertIn("404", error)

    def test_a_short_download_is_refused(self):
        result, error, _path = self._download(b"z" * 100, expected_size=999999)
        self.assertIsNone(result)
        self.assertTrue(error)

    def test_a_cancel_stops_without_leaving_a_file_to_use(self):
        import threading

        stop = threading.Event()
        stop.set()
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        with mock.patch.object(
            updater.requests, "get", return_value=_FakeResponse(b"z" * 4096)
        ):
            result, error = download(
                "https://example.test/build",
                os.path.join(base, "f.bin"),
                stop_event=stop,
            )
        self.assertIsNone(result)
        self.assertEqual(error, "cancelled")


def _windows_zip(path, exe="AutoRewarder.exe"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AutoRewarder/AutoRewarder.exe", "binary")
        archive.writestr("AutoRewarder/_internal/gui/index.html", "<html>")
        archive.writestr("AutoRewarder/Start-AutoRewarder.ps1", "# launcher")
    return path


def _linux_tar(path):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (
            ("AutoRewarder/AutoRewarder", b"binary"),
            ("AutoRewarder/_internal/gui/index.html", b"<html>"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


class UnpackTests(unittest.TestCase):
    def test_a_windows_build_unpacks_to_the_archive_root(self):
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        archive = _windows_zip(os.path.join(base, "w.zip"))
        folder, error = unpack(archive, os.path.join(base, "out"), "Windows")
        self.assertTrue(folder, error)
        self.assertTrue(os.path.isfile(os.path.join(folder, "AutoRewarder.exe")))
        self.assertTrue(
            os.path.isfile(os.path.join(folder, "_internal", "gui", "index.html"))
        )

    def test_a_linux_build_unpacks_the_same_way(self):
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        archive = _linux_tar(os.path.join(base, "l.tar.gz"))
        folder, error = unpack(archive, os.path.join(base, "out"), "Linux")
        self.assertTrue(folder, error)
        self.assertTrue(os.path.isfile(os.path.join(folder, "AutoRewarder")))

    def test_a_build_without_its_executable_is_refused(self):
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        archive = os.path.join(base, "bad.zip")
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("AutoRewarder/readme.txt", "no exe here")
        folder, error = unpack(archive, os.path.join(base, "out"), "Windows")
        self.assertIsNone(folder)
        self.assertIn("AutoRewarder.exe", error)

    def test_an_archive_that_climbs_out_of_its_folder_is_refused(self):
        """A crafted name must never write into the parent directory."""
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        archive = os.path.join(base, "evil.zip")
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../../escaped.txt", "bad")
            handle.writestr("AutoRewarder/AutoRewarder.exe", "binary")
        folder, error = unpack(archive, os.path.join(base, "out"), "Windows")
        self.assertIsNone(folder)
        self.assertIn("outside", error)
        self.assertFalse(os.path.exists(os.path.join(base, "escaped.txt")))

    def test_a_corrupt_archive_is_reported_not_raised(self):
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        archive = os.path.join(base, "corrupt.zip")
        with open(archive, "wb") as handle:
            handle.write(b"not a zip at all")
        folder, error = unpack(archive, os.path.join(base, "out"), "Windows")
        self.assertIsNone(folder)
        self.assertTrue(error)


class StagingTests(unittest.TestCase):
    def test_staging_sits_beside_the_install_not_inside_it(self):
        """
        Inside it, the swap would copy the new build over its own source — and
        the folder it lives in is the one being replaced.
        """
        target = os.path.join("/apps", "AutoRewarder")
        staged = staging_dir(target, "v4.7")
        self.assertEqual(os.path.dirname(staged), "/apps")
        self.assertIn("v4.7", staged)

    def test_a_tag_cannot_walk_out_of_that_folder(self):
        staged = staging_dir("/apps/AutoRewarder", "../../etc")
        self.assertEqual(os.path.dirname(staged), "/apps")
        self.assertNotIn("..", staged)

    def test_leftover_updates_are_swept_but_only_their_own_folders(self):
        """
        A successful update restarts the app, so the helper never tidies up and
        ~700 MB stays beside the install. The new copy clears it — but must
        never touch a folder it did not make.
        """
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        install = os.path.join(base, "AutoRewarder")
        os.makedirs(os.path.join(install, "config"))
        for name in (".autorewarder-update-v4.6", ".autorewarder-update-v4.7"):
            os.makedirs(os.path.join(base, name, "AutoRewarder"))
        os.makedirs(os.path.join(base, "Backups"))

        removed = updater.sweep_stale_updates(install)

        self.assertEqual(len(removed), 2)
        self.assertFalse(
            os.path.exists(os.path.join(base, ".autorewarder-update-v4.6"))
        )
        self.assertTrue(os.path.isdir(os.path.join(base, "Backups")))
        self.assertTrue(os.path.isdir(os.path.join(install, "config")))


class SwapScriptTests(unittest.TestCase):
    def test_the_windows_script_waits_then_copies_over(self):
        script = _swap_script_windows(
            4242, r"C:\stage", r"C:\apps\AR", "AutoRewarder.exe"
        )
        # It waits for the process that is still running.
        self.assertIn("Get-Process -Id 4242", script)
        # It copies over the target instead of mirroring it, which would delete
        # a portable config folder.
        self.assertIn("robocopy", script)
        self.assertIn("/E", script)
        self.assertNotIn("/MIR", script)
        # And it clears the download marker the copied files inherit.
        self.assertIn("Unblock-File", script)
        # Then starts the new copy.
        self.assertIn("Start-Process", script)

    def test_the_posix_script_waits_then_copies_and_starts(self):
        script = _swap_script_posix(4242, "/tmp/stage", "/apps/AR", "AutoRewarder")
        self.assertIn("kill -0 4242", script)
        self.assertIn("cp -a", script)
        self.assertIn("chmod +x", script)
        self.assertIn("./AutoRewarder", script)

    def test_a_path_with_spaces_is_quoted(self):
        """An install under "Program Files\\My Games" must still be copied."""
        script = _swap_script_windows(
            1, r"C:\my stage", r"C:\apps\my app", "AutoRewarder.exe"
        )
        # JSON-quoted, so PowerShell keeps them as single arguments.
        self.assertIn(r'"C:\\my stage"', script)
        self.assertIn(r'"C:\\apps\\my app"', script)
        self.assertIn('$exe    = "AutoRewarder.exe"', script)

    def test_an_elevated_install_asks_for_the_rights_it_needs(self):
        """
        Program Files is not writable by a normal user. Failing silently there
        would leave the app on the old version with no idea why.
        """
        script = _swap_script_windows(
            1, "s", r"C:\Program Files\AutoRewarder", "AutoRewarder.exe"
        )
        self.assertIn("Test-Writable", script)
        self.assertIn("-Verb RunAs", script)
        self.assertIn("-WindowStyle Hidden", script)


class ApplyTests(unittest.TestCase):
    def _staged(self, exe="AutoRewarder.exe"):
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        folder = os.path.join(base, "stage")
        os.makedirs(folder)
        with open(os.path.join(folder, exe), "wb") as handle:
            handle.write(b"binary")
        return folder, base

    def test_the_helper_is_started_hidden_on_windows(self):
        folder, base = self._staged()
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            ok, error = apply_and_restart(
                folder, os.path.join(base, "install"), pid=1234, platform_name="Windows"
            )
        self.assertTrue(ok, error)
        _args, kwargs = popen.call_args
        command = _args[0]
        self.assertIn("-WindowStyle", command)
        self.assertIn("Hidden", command)
        self.assertEqual(kwargs.get("creationflags"), 0x08000000)
        self.assertIs(kwargs.get("stdout"), updater.subprocess.DEVNULL)

    def test_the_helper_waits_for_this_process(self):
        folder, base = self._staged()
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            apply_and_restart(
                folder, os.path.join(base, "install"), pid=4321, platform_name="Windows"
            )
        command = popen.call_args[0][0]
        script_path = command[-1]
        with open(script_path, encoding="utf-8") as handle:
            self.assertIn("4321", handle.read())

    def test_a_posix_update_uses_a_shell_script(self):
        folder, base = self._staged(exe="AutoRewarder")
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            ok, error = apply_and_restart(
                folder, os.path.join(base, "install"), pid=1, platform_name="Linux"
            )
        self.assertTrue(ok, error)
        self.assertEqual(popen.call_args[0][0][0], "/bin/sh")

    def test_an_empty_stage_is_refused_before_a_helper_is_started(self):
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            ok, _error = apply_and_restart(
                os.path.join(base, "missing"), base, platform_name="Windows"
            )
        self.assertFalse(ok)
        self.assertFalse(popen.called)

    def test_a_stage_without_the_executable_is_refused(self):
        base = tempfile.mkdtemp()
        self.addCleanup(updater.clean_staging, base)
        folder = os.path.join(base, "stage")
        os.makedirs(folder)
        with open(os.path.join(folder, "readme.txt"), "w") as handle:
            handle.write("hi")
        ok, error = apply_and_restart(folder, base, platform_name="Windows")
        self.assertFalse(ok)
        self.assertIn("AutoRewarder.exe", error)


if __name__ == "__main__":
    unittest.main()
