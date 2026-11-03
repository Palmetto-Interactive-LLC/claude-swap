"""Pure, import-safe presentation models for the macOS menu-bar surface.

This module intentionally contains no AppKit, switcher actions, account
mutation, credential access, or network work. It only shapes an already-collected
:class:`~claude_swap.models.AccountsSnapshot` and formats its store-backed usage
measurements. ``MenuBarSettings.load`` and ``save`` are the sole filesystem
operations, because display preferences are owned by the menu bar.

The typed popover models are the production boundary for a native compact account
popover. They preserve a live sentinel over its last-known-good measurement,
freshness, reset-time roll-forward, scoped limits, spend, and weekly pace state
without making a second fetch or mutating the source snapshot.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias

from claude_swap import pace, subscription
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.models import AccountsSnapshot
from claude_swap.switcher import SENTINEL_NOTES
from claude_swap.usage_store import SERVE_TTL_S, STALE_OK_S, UsageEntry

REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
SWITCH_HISTORY_LIMIT = 10
_WEEKLY_PERIOD_S = 7 * 86400

UsagePayload: TypeAlias = dict[str, object]
DisplayUsage: TypeAlias = UsagePayload | str | None


@dataclass
class MenuBarSettings:
    """User-configurable menu bar display behavior, persisted as JSON.

    Only display preferences and the auto-switch on/off toggle live here.
    Auto-switch *policy* (threshold, cooldown, hysteresis, …) is core config,
    read/written through ``claude_swap.settings`` (the ``autoswitch.*`` keys),
    so the CLI and the menu bar share one source of truth.
    """

    show_account_name: bool = True
    title_pct: str = "both"  # one of TITLE_PCT_CHOICES
    title_scoped: bool = False  # append per-model weekly limits (e.g. Fable) to the title
    refresh_interval: int = 60
    auto_switch_enabled: bool = False

    @classmethod
    def load(cls, path: Path) -> "MenuBarSettings":
        """Load settings, falling back to defaults on any problem.

        Unknown keys are ignored; a value whose type doesn't match the field
        default is dropped (that field keeps its default). A missing or
        unparseable file yields all-defaults.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            if f.name in raw and isinstance(raw[f.name], type(getattr(defaults, f.name))):
                kwargs[f.name] = raw[f.name]
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write settings as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


class CapacityState(StrEnum):
    """Semantic capacity state for a usage row, independent of its renderer."""

    AVAILABLE = "available"
    NEAR_LIMIT = "near_limit"
    LIMIT_REACHED = "limit_reached"
    UNAVAILABLE = "unavailable"


class FreshnessState(StrEnum):
    """Whether an account has a current, stale, or absent measurement."""

    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class UsageScope(StrEnum):
    """The quota kind represented by a compact popover row."""

    SPEND = "spend"
    FIVE_HOUR = "five_hour"
    SEVEN_DAY = "seven_day"
    SCOPED = "scoped"


@dataclass(frozen=True)
class UsageRowViewModel:
    """One compact, renderer-neutral usage row for an account popover."""

    label: str
    scope: UsageScope
    used_percent: float
    reset_text: str | None
    state: CapacityState
    ahead_of_pace: bool = False
    limit_reached: bool = False
    amount_text: str | None = None
    reset_clock: str | None = None


@dataclass(frozen=True)
class PopoverAccountViewModel:
    """Immutable compact presentation state for one managed account."""

    number: str
    email: str
    alias: str
    display_name: str
    is_active: bool
    disabled: bool
    freshness: FreshnessState
    freshness_detail: str
    sentinel: str | None
    sentinel_note: str | None
    rows: tuple[UsageRowViewModel, ...]
    session_available: bool
    plan_label: str | None = None
    rejected_text: str | None = None


@dataclass(frozen=True)
class MenuBarPopoverViewModel:
    """Immutable complete account view for a compact menu-bar popover."""

    accounts: tuple[PopoverAccountViewModel, ...]
    active_number: str | None


# ---- Formatting helpers ------------------------------------------------------

def _window_pct(usage: dict | str | None, key: str) -> float | None:
    """Utilization pct for a usage window (``five_hour``/``seven_day``), or None."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a usage window's ``resets_at``; inf if missing/bad."""
    if isinstance(window, dict):
        ra = window.get("resets_at")
        if isinstance(ra, str):
            try:
                return datetime.fromisoformat(ra).timestamp()
            except ValueError:
                pass
    return float("inf")


def _live_countdown(window: dict | str | None, now: float) -> str | None:
    """Time until a usage window resets, computed live from ``resets_at``.

    The cached usage dict's ``countdown`` string is frozen at fetch time, so a
    stale (e.g. last-known-good) entry would show a wrong remaining time. Deriving
    it from the absolute ``resets_at`` keeps it correct between/without refetches.
    Returns ``None`` when there's no ``resets_at`` or it has already passed.
    """
    ts = _resets_at_ts(window)
    if ts == float("inf"):
        return None
    remaining = int(ts - now)
    if remaining <= 0:
        return None
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _rolled_weekly_window(window: dict | None, now: float) -> dict | None:
    """A weekly window with a passed reset advanced to its next 7-day boundary.

    Weekly limits reset on a fixed weekly cadence, so once the stored
    ``resets_at`` is in the past we know the window rolled over — the stored pct
    belongs to a window that no longer exists. Return a copy reflecting the reset
    state (``pct`` 0, ``resets_at`` advanced to the next future boundary) so the
    menu bar shows the reset from the static schedule alone, without waiting to
    spend tokens on a fresh fetch. Missing/future/unparseable windows are
    returned unchanged.
    """
    if not isinstance(window, dict):
        return window
    ts = _resets_at_ts(window)
    if ts == float("inf") or ts > now:
        return window
    missed = int((now - ts) // _WEEKLY_PERIOD_S) + 1
    new_ts = ts + missed * _WEEKLY_PERIOD_S
    rolled = dict(window)
    rolled["pct"] = 0.0
    rolled["resets_at"] = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat()
    rolled.pop("countdown", None)  # recomputed live from the rolled resets_at
    rolled.pop("clock", None)
    return rolled


def _local_part(email: str, limit: int = 12) -> str:
    """Email text before '@', truncated with a trailing '*' marker."""
    local = email.split("@", 1)[0]
    if len(local) > limit:
        return local[: limit - 1] + "*"
    return local


def format_title(
    active_email: str | None,
    active_usage: dict | str | None,
    settings: MenuBarSettings,
    now: float | None = None,
    alias: str | None = None,
) -> str:
    """Build the menu-bar title from the active account and settings."""
    if active_email is None:
        return ""
    if now is None:
        now = time.time()
    segments: list[str] = []
    if settings.show_account_name:
        segments.append(alias if alias else _local_part(active_email))
    if isinstance(active_usage, dict) and isinstance(active_usage.get("rejected"), dict):
        segments.append("rejected (!)")
    if settings.title_pct in ("5h", "both"):
        p = _window_pct(active_usage, "five_hour")
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_pct in ("7d", "both"):
        seven = active_usage.get("seven_day") if isinstance(active_usage, dict) else None
        seven = _rolled_weekly_window(seven, now)  # reflect a passed weekly reset
        p = seven["pct"] if isinstance(seven, dict) and isinstance(seven.get("pct"), (int, float)) else None
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_scoped and isinstance(active_usage, dict):
        # Per-model weekly limits (e.g. Fable), same shape/roll-forward as the
        # dropdown rows; named so multiple scoped models stay distinguishable.
        for window in active_usage.get("scoped") or []:
            window = _rolled_weekly_window(window, now)
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
                segments.append(f"{window['name']} {window['pct']:.0f}%")
    if not segments:
        return ""
    return " · ".join(segments)


def format_usage_log(email: str, usage: dict | str | None) -> str | None:
    """A log line of an account's session (5h) and weekly (7d) limits.

    Uses each window's absolute reset ``clock`` rather than a live countdown,
    since log lines are already timestamped. Returns ``None`` when no numeric
    window is available (sentinels, ``None``, or spend-only) so callers can skip
    logging nothing.
    """
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = _window_pct(usage, key)
        if pct is None:
            continue
        window = usage.get(key)  # a dict — _window_pct found a numeric pct in it
        clock = window.get("clock") if isinstance(window, dict) else None
        seg = f"{label} {pct:.0f}%"
        if clock:
            seg += f" (resets {clock})"
        parts.append(seg)
    if not parts:
        return None
    return f"usage {email}: " + " · ".join(parts)


def _usage_log_key(usage: dict | str | None) -> tuple[float | None, float | None]:
    """De-dupe key for usage logging: the (5h, 7d) percentages only.

    Reset clocks change every refresh; keying on the percentages means an idle
    account isn't re-logged every cycle.
    """
    return (_window_pct(usage, "five_hour"), _window_pct(usage, "seven_day"))


_SWITCH_LOG_RE = re.compile(r"Switched from account (\d+) to (\d+)")


def parse_switch_history(log_text: str, limit: int = SWITCH_HISTORY_LIMIT) -> list[str]:
    """Recent account switches from the log, most-recent first.

    Reads the ``Switched from account X to Y`` lines the switcher logs and pairs
    each with its timestamp (trimmed to the minute). Returns at most ``limit``
    entries like ``"3 → 1   2026-06-27 02:06"``. Any unparseable line is skipped.
    """
    out: list[str] = []
    for line in log_text.splitlines():
        m = _SWITCH_LOG_RE.search(line)
        if not m:
            continue
        stamp = line.split(" - ", 1)[0].strip()[:16]  # "YYYY-MM-DD HH:MM"
        out.append(f"{m.group(1)} → {m.group(2)}   {stamp}")
    return out[-limit:][::-1]


# ---- Snapshot adaptation -----------------------------------------------------

def _account_display_usage(entry) -> dict | str | None:
    """Menu-display usage for a ``UsageEntry``.

    A human-readable note for a sentinel state (token expired / API key /
    keychain unavailable), otherwise the last-good measurement dict, otherwise
    ``None``.
    """
    if entry.sentinel:
        return SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)
    return entry.last_good


def _format_duration(seconds: float) -> str:
    """Compact duration for freshness labels."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(total // 3600, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _freshness(entry: UsageEntry) -> tuple[FreshnessState, str]:
    """Derive renderer-neutral freshness without changing store semantics."""
    if entry.last_good is None:
        return FreshnessState.UNAVAILABLE, "Usage unavailable"
    if entry.age_s is None:
        return FreshnessState.STALE, "Last updated unknown"
    if entry.age_s <= STALE_OK_S:
        detail = "Updated just now" if entry.age_s <= SERVE_TTL_S else f"Updated {_format_duration(entry.age_s)} ago"
        return FreshnessState.FRESH, detail
    return FreshnessState.STALE, f"Last updated {_format_duration(entry.age_s)} ago"


def _capacity_state(percent: float) -> CapacityState:
    if percent >= 90:
        return CapacityState.LIMIT_REACHED
    if percent >= 70:
        return CapacityState.NEAR_LIMIT
    return CapacityState.AVAILABLE


def _spend_amount_text(window: dict) -> str | None:
    used, limit = window.get("used"), window.get("limit")
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
        return None
    currency = window.get("currency")
    prefix = "$" if currency in (None, "USD") else f"{currency} "
    return f"{prefix}{used:,.2f} / {prefix}{limit:,.2f}"


def _reset_clock(window: dict, now: float) -> str | None:
    """When a window resets, local time: ``19:00`` today, else ``Nov 8 10:00``."""
    timestamp = _resets_at_ts(window)
    if timestamp == float("inf") or timestamp <= now:
        return None
    moment = datetime.fromtimestamp(timestamp)
    if moment.date() == datetime.fromtimestamp(now).date():
        return moment.strftime("%H:%M")
    return f"{moment.strftime('%b')} {moment.day} {moment.strftime('%H:%M')}"


def _usage_row(
    label: str,
    scope: UsageScope,
    window: dict,
    now: float,
    fetched_at: float | None,
) -> UsageRowViewModel | None:
    """Shape one valid raw window into an immutable compact row."""
    raw_percent = window.get("pct")
    if not isinstance(raw_percent, (int, float)):
        return None
    percent = float(raw_percent)
    is_weekly = scope in (UsageScope.SEVEN_DAY, UsageScope.SCOPED)
    pace_result = pace.compute_pace(window, fetched_at=fetched_at) if is_weekly else None
    limit_reached = scope is UsageScope.SCOPED and percent >= 100
    state = _capacity_state(percent)
    return UsageRowViewModel(
        label=label,
        scope=scope,
        used_percent=percent,
        reset_text=_live_countdown(window, now),
        state=state,
        ahead_of_pace=bool(pace_result and pace_result.ahead) and not limit_reached,
        limit_reached=limit_reached,
        amount_text=_spend_amount_text(window) if scope is UsageScope.SPEND else None,
        reset_clock=_reset_clock(window, now),
    )


def _popover_rows(entry: UsageEntry, now: float) -> tuple[UsageRowViewModel, ...]:
    """Shape last-good data, rolling only weekly/scoped display windows."""
    usage = entry.last_good
    if not isinstance(usage, dict):
        return ()
    rows: list[UsageRowViewModel] = []
    spend = usage.get("spend")
    if isinstance(spend, dict):
        row = _usage_row("Spend", UsageScope.SPEND, spend, now, entry.fetched_at)
        if row is not None:
            rows.append(row)
    for key, label, scope in (
        ("five_hour", "5h", UsageScope.FIVE_HOUR),
        ("seven_day", "7d", UsageScope.SEVEN_DAY),
    ):
        raw_window = usage.get(key)
        if not isinstance(raw_window, dict):
            continue
        window = _rolled_weekly_window(raw_window, now) if scope is UsageScope.SEVEN_DAY else raw_window
        if window is None:
            continue
        row = _usage_row(label, scope, window, now, entry.fetched_at)
        if row is not None:
            rows.append(row)
    scoped = usage.get("scoped")
    if isinstance(scoped, list):
        for raw_window in scoped:
            if not isinstance(raw_window, dict):
                continue
            name = raw_window.get("name")
            if not isinstance(name, str) or not name:
                continue
            window = _rolled_weekly_window(raw_window, now)
            if window is None:
                continue
            row = _usage_row(name, UsageScope.SCOPED, window, now, entry.fetched_at)
            if row is not None:
                rows.append(row)
    return tuple(rows)


def _rejected_text(entry: UsageEntry, now: float) -> str | None:
    """A refused setup-token probe: at its limit until then, whatever the windows read."""
    usage = entry.last_good
    rejected = usage.get("rejected") if isinstance(usage, dict) else None
    if not isinstance(rejected, dict):
        return None
    countdown = _live_countdown(rejected, now)
    return f"Rejected {countdown}" if countdown else "Rejected"


def popover_view_model(
    snapshot: AccountsSnapshot, now: float | None = None
) -> MenuBarPopoverViewModel:
    """Adapt an already-collected snapshot to immutable compact popover models.

    This is a pure transformation. It retains sentinel state separately from
    the rows built from last-good data, allowing a UI to show both the current
    problem and the last measurement without pretending a sentinel erased it.
    """
    if now is None:
        now = time.time()
    accounts: list[PopoverAccountViewModel] = []
    for account in snapshot.accounts:
        freshness, freshness_detail = _freshness(account.usage)
        alias = account.alias
        display_name = alias if alias else _local_part(account.email)
        sentinel = account.usage.sentinel
        rows = _popover_rows(account.usage, now)
        accounts.append(
            PopoverAccountViewModel(
                number=account.number,
                email=account.email,
                alias=alias,
                display_name=display_name,
                is_active=account.is_active,
                disabled=account.disabled,
                freshness=freshness,
                freshness_detail=freshness_detail,
                sentinel=sentinel,
                sentinel_note=SENTINEL_NOTES.get(sentinel, sentinel) if sentinel else None,
                rows=rows,
                # The active default profile already owns this account's refresh
                # token. A second profile can drift after token rotation, so only
                # inactive OAuth accounts may start an isolated session.
                session_available=account.kind == "oauth" and not account.is_active,
                plan_label=subscription.plan_label((account.subscription_plan or {}).get("plan")),
                rejected_text=_rejected_text(account.usage, now),
            )
        )
    return MenuBarPopoverViewModel(tuple(accounts), snapshot.active_number)


def account_login_expired(account: PopoverAccountViewModel) -> bool:
    """Whether this account's refresh token is dead and needs a fresh login."""
    return account.sentinel == USAGE_RELOGIN_REQUIRED


def account_can_activate(account: PopoverAccountViewModel) -> bool:
    """Whether this account can become the live login without signing in first.

    Rotation has no confirmation, so nothing else stands between a click and
    replacing a working login with a dead refresh token.
    """
    return not account.is_active and not account_login_expired(account)


def account_initial(account: PopoverAccountViewModel) -> str:
    """Badge letter: the alias's, else the email domain's.

    Several addresses can share a first letter (matt@this, matt@that), but their
    domains are what tell them apart.
    """
    source = account.alias or account.email.partition("@")[2] or account.email
    return source[:1].upper()


def account_status_text(account: PopoverAccountViewModel) -> str:
    """The short state at the end of an account's first line, when there is one."""
    if account.is_active:
        return "Active"
    if account_login_expired(account):
        return "Signed out"
    if account.disabled:
        return "Disabled"
    if account.rejected_text:
        return account.rejected_text
    if account_at_limit(account):
        return "At limit"
    return ""


def account_at_limit(account: PopoverAccountViewModel) -> bool:
    """Whether a rate-limit window, as opposed to a spend budget, is used up."""
    return any(
        row.scope is not UsageScope.SPEND and row.used_percent >= 100 for row in account.rows
    )


def compact_usage_rows(
    account: PopoverAccountViewModel, limit: int = 5
) -> tuple[UsageRowViewModel, ...]:
    """The limits to list: 5h, 7d, model limits tightest first, then spend.

    An expired login's last-known windows say nothing about what it can do now,
    so it lists none.
    """
    if account_login_expired(account):
        return ()
    windows = [row for row in account.rows if row.scope in (UsageScope.FIVE_HOUR, UsageScope.SEVEN_DAY)]
    scoped = sorted(
        (row for row in account.rows if row.scope is UsageScope.SCOPED),
        key=lambda row: row.used_percent,
        reverse=True,
    )
    spend = [row for row in account.rows if row.scope is UsageScope.SPEND]
    return tuple((windows + scoped + spend)[:limit])


def usage_time_left(row: UsageRowViewModel) -> str:
    """The time-left column, never blank: a window with no reset time says why."""
    if row.reset_text:
        return row.reset_text
    return "not started" if row.used_percent <= 0 else "reset due"


def account_tooltip(account: PopoverAccountViewModel) -> str:
    """Everything the compact row leaves out, one fact per line."""
    lines = [account.email]
    if account.plan_label:
        lines.append(account.plan_label)
    lines.append(account.freshness_detail)
    if account.sentinel_note:
        lines.append(account.sentinel_note)
    for row in account.rows:
        value = row.amount_text or f"{row.used_percent:.0f}%"
        line = f"{row.label}: {value}"
        if row.reset_text:
            line += f", resets in {row.reset_text}"
            if row.reset_clock:
                line += f" ({row.reset_clock})"
        if row.ahead_of_pace:
            line += ", ahead of pace"
        lines.append(line)
    return "\n".join(lines)


def _adapt_snapshot(snap) -> dict:
    """Adapt an ``AccountsSnapshot`` to the menu bar's render dict.

    Shape: ``{"accounts": [(num, email, is_active, display_usage, last_good, alias, disabled, fetched_at), ...],
    "active_email": str | None, "active_usage": dict | str | None,
    "active_alias": str | None}``. The snapshot itself is produced by
    ``SnapshotSource`` (the paced read path), so this is a pure transform — no
    fetching, no I/O. Per-account ``fetched_at`` is the underlying
    measurement's fetch time, used only for the pace marker (issue #125).
    """
    accounts = []
    active_email = None
    active_usage = None
    active_alias = None
    for acc in snap.accounts:
        display = _account_display_usage(acc.usage)
        accounts.append(
            (
                acc.number, acc.email, acc.is_active, display, acc.usage.last_good,
                acc.alias, acc.disabled, acc.usage.fetched_at,
            )
        )
        if acc.is_active:
            active_email, active_usage, active_alias = acc.email, display, acc.alias
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_usage": active_usage,
        "active_alias": active_alias,
    }
