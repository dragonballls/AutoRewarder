"""Shared utility helpers for AutoRewarder."""

import time
import random
import requests

from .config import CURRENT_VERSION, REPO


def human_typing(element, text):
    """
    Simulate human-like typing by sending keys to a web element with random delays.

    Args:
        element: The web element to send keys to.
        text: The text to type into the element.
    """

    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.18))


def _build_keyboard_augmenter():
    """
    Build the nlpaug keyboard augmenter, or return None when nlpaug is unusable.

    nlpaug is an optional dependency: it is bundled into the frozen builds, but a
    missing or partially installed copy (and a frozen build whose package
    metadata was not collected, which raises PackageNotFoundError from inside
    nlpaug) must not take the run down with it.
    """
    try:
        import nlpaug.augmenter.char as nac  # type: ignore
    except Exception:
        return None

    try:
        return nac.KeyboardAug(
            # 100% probability because the 20% overall chance
            # handled outside by the `random` module
            aug_char_p=1.0,
            aug_word_p=1.0,
            # Only one typo per query
            aug_char_max=1,
            aug_word_max=1,
            # Ignore short words
            min_char=4,
            include_upper_case=False,
            include_special_char=False,
            include_numeric=True,
        )
    except Exception:
        return None


def humanize_queries(queries, logger=None):
    """
    Substitute character by keyboard distance to simulate human typing errors.

    This sits on the static-query fallback, which is exactly the path taken when
    LLM query generation fails — so it must never raise. A missing typo engine
    degrades to the queries as they are: the run still collects its searches.

    Args:
        queries (list): A list of query strings.
        logger (callable, optional): Logger used to report a skipped pass.
    Returns:
        humanized_queries (list): Modified query strings with simulated typing
        errors, or the queries unchanged when nlpaug is unavailable.
    """
    mod = _build_keyboard_augmenter()

    if mod is None:
        message = (
            "[WARNING] Skipping query humanizing — the nlpaug typo engine is "
            "unavailable. Searching the queries as-is."
        )
        if logger:
            logger(message)
        else:
            print(message)
        return list(queries)

    humanized_queries = []

    for query in queries:
        if random.random() < 0.2:
            try:
                modified_query = mod.augment(query)
                typo_query = (
                    modified_query[0]
                    if isinstance(modified_query, list)
                    else modified_query
                )
            except Exception:
                # A per-query augment failure is not worth losing the query.
                humanized_queries.append(query)
                continue
            humanized_queries.append(typo_query)
        else:
            humanized_queries.append(query)

    return humanized_queries


def _version_tuple(value):
    """
    Turn "v4.5" / "4.5.1" / "v4.10" into a comparable tuple of ints.

    The updater used to compare tag strings with `!=`, which reports "an update
    is available" whenever the strings differ — including when the published
    version is *older* than the running one, so downgrading was offered as an
    upgrade. It also can't order them: "v4.10" sorts below "v4.9" as text.

    Returns:
        tuple: e.g. (4, 5, 0); (0,) for anything unparseable, which compares
        below every real version and so never wins by accident.
    """
    text = str(value or "").strip().lstrip("vV")
    parts = []
    for piece in text.split("."):
        # Stop at the first non-numeric piece: "4.5-beta" is 4.5.
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts) if parts else (0,)


def is_newer_version(latest, current):
    """
    Whether `latest` is a strictly newer version than `current`.

    Args:
        latest (str): the tag or version published upstream.
        current (str): the version this build reports.

    Returns:
        bool: True only when `latest` is newer, so an older or equal release
        never prompts an update.
    """
    return _version_tuple(latest) > _version_tuple(current)


def check_for_updates(logger=None):
    """
    Check GitHub API for the latest release and compare it to the current version.

    Args:
        logger (callable, optional): A function to log messages. Defaults to None.

    Returns:
        tuple: (is_update_available (bool), latest_version (str or None))
    """
    try:
        headers = {"User-Agent": "AutoRewarder-App"}

        response = requests.get(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers=headers,
            timeout=5,
        )
        if response.status_code == 200:
            latest = response.json().get("tag_name")
            if latest:
                return is_newer_version(latest, CURRENT_VERSION), latest
        elif response.status_code == 429:
            if logger:
                logger("[WARNING] GitHub API rate limit reached (429).")
                logger("Try again later or check manually for updates.")
        elif response.status_code == 403:

            is_rate_limit = response.headers.get("X-Ratelimit-Remaining") == "0"

            if logger:
                if is_rate_limit:
                    logger(
                        "[WARNING] GitHub API rate limit exceeded (403). Try again later."
                    )
                else:
                    logger(
                        "[WARNING] GitHub access forbidden (403). Check your VPN or connection."
                    )
        else:
            if logger:
                logger(
                    f"[WARNING] GitHub update check failed. Status: {response.status_code}"
                )

    except requests.exceptions.RequestException as e:
        if logger:
            logger(f"[WARNING] Network error while checking for updates: {e}")
    except Exception as e:
        if logger:
            logger(f"[ERROR] Unexpected error while checking for updates: {e}")

    return False, None
