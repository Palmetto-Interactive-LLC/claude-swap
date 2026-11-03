"""Public façade for the native macOS ``cswap menubar`` surface.

Import-safe presentation helpers remain available to CLI and test callers on
all platforms. The PyObjC implementation is imported only at launch time.
"""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import sys
from pathlib import Path

from claude_swap.menubar_controller import MenuBarController
from claude_swap.menubar_viewmodel import (
    AUTO_THRESHOLD_CHOICES,
    EMPTY_SNAPSHOT,
    ICON,
    REFRESH_CHOICES,
    SWITCH_HISTORY_LIMIT,
    TITLE_PCT_CHOICES,
    CapacityState,
    FreshnessState,
    MenuBarPopoverViewModel,
    MenuBarSettings,
    PopoverAccountViewModel,
    UsageRowViewModel,
    UsageScope,
    _account_display_usage,
    _adapt_snapshot,
    _live_countdown,
    _local_part,
    _resets_at_ts,
    _rolled_weekly_window,
    _usage_log_key,
    _window_pct,
    format_account_label,
    format_title,
    format_usage_log,
    parse_switch_history,
    popover_view_model,
    tightest_pct,
    usage_summary,
)
from claude_swap.printer import warning
from claude_swap.switcher import SENTINEL_NOTES

NOTIFICATION_BUNDLE_ID = "com.claude-swap.menubar"


def ensure_notification_identity(
    executable: Path | None = None,
    *,
    platform: str = sys.platform,
) -> Path | None:
    """Ensure rumps can resolve a bundle identifier for notifications.

    Fallback for runs outside the wrapper bundle (``claude_swap.app_bundle``),
    whose Info.plist already carries the identifier: a bare command-line
    interpreter has no app bundle, so rumps looks for an ``Info.plist`` beside
    it. uv/pipx reinstalls can recreate that environment; repair the tiny
    plist on every launch when needed.
    """
    if platform != "darwin":
        return None
    path = (executable or Path(sys.executable)).parent / "Info.plist"
    data: dict = {}
    try:
        if path.exists():
            try:
                loaded = plistlib.loads(path.read_bytes())
            except Exception:
                loaded = None  # unreadable/corrupt — rebuild from scratch
            if isinstance(loaded, dict):
                data = loaded
        changed = False
        if not data.get("CFBundleIdentifier"):
            data["CFBundleIdentifier"] = NOTIFICATION_BUNDLE_ID
            changed = True
        if not data.get("CFBundleName"):
            data["CFBundleName"] = "claude-swap"
            changed = True
        if changed or not path.exists():
            # atomic: an interrupted write must not leave a half-written plist
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(plistlib.dumps(data))
            os.replace(tmp, path)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        logging.getLogger("claude-swap").warning(
            "Could not prepare menu-bar notification identity: %s", exc
        )
        return None
    return path


# macOS 26 stopped drawing status items for processes launched through an
# exec trampoline, and a CPython *framework* build is exactly that: its
# ``bin/python3.x`` is a stub that posix_spawns into ``Python.app``
# (Mac/Tools/pythonw.c). Homebrew and python.org ship framework builds;
# uv-managed and most other interpreters do not.
#
# Measured on macOS 26.6.2 with rumps 0.4.0, same bare rumps app throughout:
#
#   Homebrew 3.14.6   sys._framework 'Python'   no status item
#   Homebrew 3.10.21  sys._framework 'Python'   no status item
#   uv 3.14.7         sys._framework ''         status item drawn
#   uv 3.13.15        sys._framework ''         status item drawn
#
# The interpreter version is not the variable; the build is.


MIN_AFFECTED_MACOS = 26


def _macos_major(mac_ver: str | None = None) -> int | None:
    """Major version of the running macOS, or None if it cannot be read."""
    raw = platform.mac_ver()[0] if mac_ver is None else mac_ver
    head = raw.split(".")[0]
    return int(head) if head.isdigit() else None


def framework_build_warning(
    framework=None, install_method=None, mac_ver: str | None = None
) -> str | None:
    """Text to show when this interpreter cannot draw a status item.

    Returns None wherever the menu bar is known to work. Both halves of the
    condition matter: the evidence is a framework build *on macOS 26*, and
    framework builds draw fine on earlier releases — gating on the build alone
    would nag every Homebrew user on macOS 14 or 15, on every launch and in
    the service log on every restart.

    Nothing here can fix the incompatibility. The point is that it fails
    silently, with a healthy process and empty logs, so it is worth one line.
    """
    fw = getattr(sys, "_framework", "") if framework is None else framework
    if not fw:
        return None

    major = _macos_major(mac_ver)
    if major is None or major < MIN_AFFECTED_MACOS:
        return None

    if install_method is None:
        from claude_swap.update_check import _detect_install_method

        install_method = _detect_install_method()

    if install_method == "uv":
        remedy = (
            "  uv tool install --managed-python --force 'claude-swap[menubar]'"
        )
    elif install_method == "pipx":
        remedy = (
            "  Reinstall against a non-framework interpreter, e.g. one from "
            "`uv python install 3.13`:\n"
            "  pipx install --force --python <that python> 'claude-swap[menubar]'"
        )
    else:
        remedy = (
            "  Reinstall against a non-framework interpreter "
            "(uv-managed ones are; Homebrew and python.org are not)."
        )

    return (
        "This is a framework build of Python, which on macOS 26 has been "
        "observed not to draw the menu bar icon: the process runs and logs "
        "nothing, but no status item appears.\n" + remedy
    )


def run(switcher) -> int:
    """Launch the native AppKit status item and transient compact popover."""
    from claude_swap import app_bundle
    from claude_swap.menubar_appkit import require_appkit, run_native_menubar

    # Fail with the install hint before building a bundle around a venv that
    # has no AppKit in it.
    require_appkit()

    # Hop into the wrapper .app before anything AppKit-side is set up, so the
    # status item's owner has a bundle identifier that menu bar managers can
    # key on. Returns only when staying in this interpreter (already bundled,
    # opted out, framework build, or the bundle could not be built).
    app_bundle.maybe_reexec(switcher.backup_dir)
    if not app_bundle.in_bundle():
        ensure_notification_identity()
    _warn = framework_build_warning()
    if _warn:
        # stderr, not stdout: launchd sends stdout to the .log file where it
        # would sit in a block buffer for the life of the process, and the
        # install output points the user at the .err file anyway. stderr stays
        # line-buffered even when redirected, so it lands immediately.
        warning(_warn, file=sys.stderr)

    return run_native_menubar(switcher)


__all__ = [
    "AUTO_THRESHOLD_CHOICES",
    "CapacityState",
    "EMPTY_SNAPSHOT",
    "FreshnessState",
    "MIN_AFFECTED_MACOS",
    "ICON",
    "MenuBarController",
    "MenuBarPopoverViewModel",
    "MenuBarSettings",
    "NOTIFICATION_BUNDLE_ID",
    "PopoverAccountViewModel",
    "REFRESH_CHOICES",
    "SENTINEL_NOTES",
    "SWITCH_HISTORY_LIMIT",
    "TITLE_PCT_CHOICES",
    "UsageRowViewModel",
    "UsageScope",
    "_account_display_usage",
    "_adapt_snapshot",
    "_live_countdown",
    "_local_part",
    "_macos_major",
    "_resets_at_ts",
    "_rolled_weekly_window",
    "_usage_log_key",
    "_window_pct",
    "ensure_notification_identity",
    "framework_build_warning",
    "format_account_label",
    "format_title",
    "format_usage_log",
    "parse_switch_history",
    "popover_view_model",
    "run",
    "tightest_pct",
    "usage_summary",
]
