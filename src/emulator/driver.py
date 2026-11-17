"""Edge WebDriver setup for per-account profiles."""

import os
import platform
import random
import subprocess
import time

from selenium import webdriver
from selenium.common.exceptions import (
    SessionNotCreatedException,
    WebDriverException,
)
from selenium.webdriver.edge.options import Options


def _is_closed_session(error_message):
    """
    Whether an error means the browser was closed, not that the work failed.

    Selenium reports a closed window in a handful of ways depending on how it
    was closed: the window is gone ("target window already closed"), the session
    behind it is gone ("invalid session id"), or the driver has disconnected.
    All of them are the normal end of First Setup, where the person closes the
    browser to say they are done. Before this, closing the browser while a click
    was still settling dumped a Chromium backtrace into the log and marked the
    setup failed — one of the four accounts in the report died exactly this way.

    Args:
        error_message (str): the exception text, already lowercased.

    Returns:
        bool: True when the session or window went away.
    """
    text = str(error_message or "").lower()
    return any(
        marker in text
        for marker in (
            "target window already closed",
            "no such window",
            "invalid session id",
            "session deleted because of page crash",
            "disconnected",
            "not reachable",
            "chrome not reachable",
            "connection refused",
        )
    )


def describe_start_failure(exc):
    """
    Turn a driver-start exception into an explanation someone can act on.

    Selenium's own message for a failed launch is a Chromium backtrace
    ("DevToolsActivePort file doesn't exist", "session not created"), which says
    nothing about the cause. The causes are usually one of a handful, so name
    them and give the way out.

    Args:
        exc (Exception): the exception raised by webdriver.Edge(...).

    Returns:
        str: an explanation, or "" when the cause isn't recognised.
    """
    text = str(exc)
    lowered = text.casefold()

    if "devtoolsactiveport" in lowered:
        return (
            "Microsoft Edge started but never opened a debugging port, then "
            "exited. On Windows this almost always means the account's browser "
            "profile is still locked by another Edge window, or a half-closed "
            "session left it in a bad state.\n"
            "Try, in this order:\n"
            "  1. Close every Microsoft Edge window, including any in the "
            "system tray, then run again.\n"
            "  2. In Edge settings, turn off 'Startup boost' and 'Continue "
            "running background extensions and apps when Microsoft Edge is "
            "closed', then run again.\n"
            "  3. If it still fails, sign in again through First Setup, which "
            "reuses the account's profile."
        )

    if "user data directory is already in use" in lowered or (
        "lock" in lowered and "profile" in lowered
    ):
        return (
            "That account's browser profile is open in another Edge window. "
            "Close every Microsoft Edge window and run again."
        )

    if "msedgedriver" in lowered and (
        "cannot find" in lowered or "not found" in lowered
    ):
        return (
            "The Edge driver is missing. The app downloads it on first use, so "
            "this usually means that download was blocked. Check the internet "
            "connection or any firewall, then run again."
        )

    if "version" in lowered and "driver" in lowered:
        return (
            "The Edge driver does not match the installed Microsoft Edge "
            "version. Update Microsoft Edge, then run again."
        )

    return ""


class DriverManager:
    """
    Manages the Selenium WebDriver for MS Edge.

    Each DriverManager instance is bound to a specific Edge --user-data-dir
    (i.e. one account). Switching account = rebuilding this manager with a
    different profile_path.
    """

    # Realistic iPhone UA so Microsoft Rewards credits the searches as mobile.
    MOBILE_USER_AGENT = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
        "Mobile/15E148 Safari/604.1"
    )
    MOBILE_WINDOW_SIZE = "412,915"
    DESKTOP_WINDOW_SIZE = "1920,1080"

    # A launch is retried this many times before the failure is reported. A
    # profile left briefly locked by a just-closed Edge is the most common cause
    # and it clears on its own, so giving up on the first error loses runs for
    # no reason.
    START_ATTEMPTS = 3

    def __init__(self, profile_path=None, hide_browser=False):
        """
        Args:
            profile_path (str | None): Absolute path to the Selenium --user-data-dir
                directory. None when no account is selected (empty state). In that
                case setup_driver will raise, since there is nothing to launch.
            hide_browser (bool): Whether to run the browser in headless mode.
        """
        self.profile_path = profile_path
        self.hide_browser = hide_browser

    # -- Preparation -----------------------------------------------------------

    def _prepare_profile_dir(self):
        """
        Make sure the profile directory exists and is writable.

        Chromium does not create a missing --user-data-dir reliably, and a fresh
        account has no directory yet: the launch then fails with the same
        "DevToolsActivePort" error as a locked profile, which sends people
        looking for the wrong cause.
        """
        try:
            os.makedirs(self.profile_path, exist_ok=True)
        except OSError as e:
            raise RuntimeError(
                "The account's browser profile folder could not be created at "
                f"{self.profile_path}: {e}. Check that the folder is writable, "
                "then run again."
            ) from e

        if not os.access(self.profile_path, os.W_OK):
            raise RuntimeError(
                "The account's browser profile folder is not writable: "
                f"{self.profile_path}. Check the folder's permissions, then run "
                "again."
            )

    def _remove_singleton_locks(self):
        """
        Delete this profile's abandoned Chromium lock files.

        A singleton lock survives an unclean exit, and Edge then refuses to
        start on that profile until it is removed — the failure that looks like
        a permanent crash.

        Returns:
            bool: True when the locks are gone (or were never there).
        """
        names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
        paths = [os.path.join(self.profile_path, name) for name in names]
        existing = [path for path in paths if os.path.lexists(path)]
        if not existing:
            return True

        for path in existing:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError:
                # Held by a live process: leaving it alone is correct. The
                # caller decides whether that is fatal.
                return False

        return not any(os.path.lexists(path) for path in paths)

    def close_running_edge(self):
        """
        Close Edge processes that are holding *this* profile open.

        Only processes whose command line names this profile are closed, so a
        browser the user is actually using for something else is left alone.
        Windows only: closing browsers by name elsewhere would be far too broad.
        """
        if platform.system() != "Windows" or not self.profile_path:
            return

        profile = os.path.normcase(os.path.abspath(self.profile_path)).casefold()
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
            "Where-Object { $_.CommandLine -and "
            f"$_.CommandLine.ToLower().Contains('{profile}') }} | "
            "ForEach-Object { $_.ProcessId }"
        )

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return

        for line in (result.stdout or "").splitlines():
            pid = line.strip()
            if not pid.isdigit():
                continue
            try:
                subprocess.run(
                    ["taskkill", "/PID", pid, "/F"],
                    capture_output=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                continue

    # -- Options ---------------------------------------------------------------

    def _build_options(self, headless, disable_identity, mobile):
        """Build the Edge options for one launch attempt."""
        options = Options()
        options.add_argument(f"--user-data-dir={self.profile_path}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--no-first-run")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        if mobile:
            options.add_argument(f"--user-agent={self.MOBILE_USER_AGENT}")
            window_size = self.MOBILE_WINDOW_SIZE
        else:
            window_size = self.DESKTOP_WINDOW_SIZE
        options.add_argument(f"--window-size={window_size}")

        # Edge's own background services hold the profile open after the last
        # window closes, which is what makes the next launch fail with
        # "DevToolsActivePort file doesn't exist". These keep our launch
        # self-contained instead of competing with whatever Edge is doing.
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-component-update")

        if disable_identity:
            # Kill the various Chromium/Edge paths that silently sign the user
            # in with the Windows-level Microsoft identity.
            options.add_argument(
                "--disable-features=msImplicitSignin,AadSsoUrlInterceptionEnabled,"
                "WebOtpBackendAuto,IdentityConsistency,msIdentityWebSignIn,"
                "msEdgeIdentitySyncInterception"
            )
            options.add_argument("--disable-sync")

        if headless:
            options.add_argument("--headless=new")
            # Disabled to allow the "browse 30 min" task to progress.
            # options.add_argument("--disable-gpu")
            options.add_argument("--window-position=-32000,-32000")

        return options

    # -- Launch ----------------------------------------------------------------

    def setup_driver(self, headless=None, disable_identity=False, mobile=False):
        """
        Set up the Selenium WebDriver for MS Edge using this manager's profile.

        A launch is attempted several times, clearing what outlives a single
        attempt: a profile folder that doesn't exist yet, abandoned Chromium
        locks, and Edge processes still holding the profile. Only after those
        are handled is a failure reported, and it is reported with an
        explanation rather than a Chromium backtrace.

        Args:
            headless: Headless override. Falls back to self.hide_browser.
            disable_identity: When True, add Edge/Chromium flags that disable
                the Windows-account-based auto sign-in. Used during First
                Setup so a second MSA can actually log in.
            mobile: When True, launch Edge with an iPhone user agent and a
                mobile-sized viewport so Rewards credits the searches as
                mobile. When False, use the desktop viewport.

        Returns:
            webdriver.Edge: The configured WebDriver instance.

        Raises:
            RuntimeError: If profile_path is None (no account selected), or the
                browser cannot be started after every recovery attempt.
        """
        if not self.profile_path:
            raise RuntimeError(
                "No account selected: cannot start the browser. "
                "Create or select an account first."
            )

        if headless is None:
            headless = self.hide_browser

        self._prepare_profile_dir()

        last_error = None
        driver = None
        for attempt in range(1, self.START_ATTEMPTS + 1):
            # Recover before every attempt, not only the first: retrying is
            # pointless while the lock that broke the previous attempt remains.
            locks_clear = self._remove_singleton_locks()
            if not locks_clear or attempt > 1:
                self.close_running_edge()
                time.sleep(1.5 * attempt)

            options = self._build_options(headless, disable_identity, mobile)
            try:
                driver = webdriver.Edge(options=options)
                break
            except (SessionNotCreatedException, WebDriverException) as e:
                last_error = e
                if attempt < self.START_ATTEMPTS:
                    # Jittered: a profile released by a closing Edge is usually
                    # free within a second or two.
                    time.sleep(random.uniform(1.0, 2.0))

        if driver is None:
            explanation = describe_start_failure(last_error)
            message = (
                f"Microsoft Edge could not be started after "
                f"{self.START_ATTEMPTS} attempts."
            )
            if explanation:
                message = f"{message}\n{explanation}"
            else:
                first_line = str(last_error).splitlines()[0]
                message = f"{message}\n{first_line}"
            raise RuntimeError(message) from last_error

        if mobile:
            # Turn the session into a genuine mobile one at the engine level.
            # Beyond the UA string, this makes `navigator.maxTouchPoints > 0`,
            # `window.matchMedia("(pointer: coarse)")` true, the viewport match
            # iPhone metrics, and touch events fire for real — so sites that
            # fingerprint using the DOM/CSS touch surface see a real mobile.
            try:
                driver.execute_cdp_cmd(
                    "Emulation.setTouchEmulationEnabled",
                    {"enabled": True, "maxTouchPoints": 5},
                )
                driver.execute_cdp_cmd(
                    "Emulation.setEmitTouchEventsForMouse",
                    {"enabled": True, "configuration": "mobile"},
                )
                driver.execute_cdp_cmd(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": 412,
                        "height": 915,
                        "deviceScaleFactor": 3,
                        "mobile": True,
                    },
                )
                driver.execute_cdp_cmd(
                    "Emulation.setUserAgentOverride",
                    {
                        "userAgent": self.MOBILE_USER_AGENT,
                        "platform": "iPhone",
                        "userAgentMetadata": {
                            "platform": "iOS",
                            "platformVersion": "17.2.1",
                            "architecture": "",
                            "model": "iPhone",
                            "mobile": True,
                        },
                    },
                )
            except Exception:
                # CDP is best-effort; fall back to the UA+window-size flags.
                pass

        return driver
