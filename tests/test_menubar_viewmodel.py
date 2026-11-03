"""Production compact-menu-popover view-model tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from claude_swap import menubar_viewmodel as viewmodel
from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import SENTINEL_NOTES
from claude_swap.usage_store import UsageEntry

_NOW = 1_000_000.0


def _iso(delta_s: float) -> str:
    return datetime.fromtimestamp(_NOW + delta_s, timezone.utc).isoformat()


def _account(
    number: str,
    entry: UsageEntry,
    *,
    active: bool = False,
    alias: str = "",
    disabled: bool = False,
    kind: str = "oauth",
) -> AccountSnapshot:
    return AccountSnapshot(
        number=number,
        email=f"account{number}@example.test",
        org_name="",
        org_uuid="",
        is_active=active,
        kind=kind,
        switchable=True,
        usage=entry,
        alias=alias,
        disabled=disabled,
    )


def test_popover_model_preserves_usage_semantics_without_fetching() -> None:
    """Rows carry the display semantics the native popover needs verbatim."""
    current = UsageEntry(
        last_good={
            "spend": {
                "pct": 25.0,
                "used": 12.5,
                "limit": 50.0,
                "currency": "USD",
                "resets_at": _iso(2 * 3600),
            },
            "five_hour": {"pct": 90.0, "resets_at": _iso(2 * 3600)},
            # One day into this weekly cycle, so 50% is meaningfully ahead.
            "seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)},
            "scoped": [
                {"name": "Fable", "pct": 100.0, "resets_at": _iso(6 * 86400)},
                {"name": "Opus", "pct": 50.0, "resets_at": _iso(6 * 86400)},
            ],
        },
        fetched_at=_NOW,
        age_s=12.0,
    )
    snapshot = AccountsSnapshot(
        active_number="1",
        accounts=(_account("1", current, active=True, alias="work", disabled=True),),
        taken_at=_NOW,
    )

    model = viewmodel.popover_view_model(snapshot, _NOW)
    account = model.accounts[0]
    rows = {row.label: row for row in account.rows}

    assert model.active_number == "1"
    assert account.display_name == "work"
    assert account.disabled and account.freshness is viewmodel.FreshnessState.FRESH
    assert account.capacity_summary == "0% left"
    assert [row.label for row in account.rows] == ["Spend", "5h", "7d", "Fable", "Opus"]
    assert rows["Spend"].amount_text == "$12.50 / $50.00"
    assert rows["Spend"].reset_text == "2h 0m"
    assert rows["5h"].state is viewmodel.CapacityState.LIMIT_REACHED
    assert not rows["5h"].ahead_of_pace  # 5h windows never carry pace.
    assert rows["7d"].ahead_of_pace and rows["7d"].reset_text == "6d 0h"
    assert rows["Fable"].limit_reached and not rows["Fable"].ahead_of_pace
    assert rows["Opus"].ahead_of_pace

    with pytest.raises(FrozenInstanceError):
        account.alias = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        rows["7d"].used_percent = 0.0  # type: ignore[misc]


def test_only_inactive_oauth_accounts_offer_isolated_sessions() -> None:
    entry = UsageEntry(last_good={"five_hour": {"pct": 10.0}})
    snapshot = AccountsSnapshot(
        active_number="1",
        accounts=(
            _account("1", entry, active=True),
            _account("2", entry),
            _account("3", entry, kind="api_key"),
        ),
        taken_at=_NOW,
    )

    active, inactive_oauth, api_key = viewmodel.popover_view_model(snapshot, _NOW).accounts

    assert not active.session_available
    assert inactive_oauth.session_available
    assert not api_key.session_available


def test_popover_model_retains_last_good_below_sentinel_and_rolls_weekly() -> None:
    stale_sentinel = UsageEntry(
        sentinel=USAGE_TOKEN_EXPIRED,
        last_good={
            # 5h is deliberately not rolled forward: it has no static cadence.
            "five_hour": {"pct": 92.0, "resets_at": _iso(-3600)},
            # The weekly values are display-only rolled to the following boundary.
            "seven_day": {"pct": 95.0, "resets_at": _iso(-86400)},
            "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(-86400)}],
        },
        fetched_at=_NOW - 3600,
        age_s=3600.0,
    )
    unknown_sentinel = UsageEntry(sentinel=USAGE_TOKEN_EXPIRED)
    snapshot = AccountsSnapshot(
        active_number="1",
        accounts=(
            _account("1", stale_sentinel, active=True),
            _account("2", unknown_sentinel),
        ),
        taken_at=_NOW,
    )

    stale, unavailable = viewmodel.popover_view_model(snapshot, _NOW).accounts
    stale_rows = {row.label: row for row in stale.rows}

    assert stale.sentinel == USAGE_TOKEN_EXPIRED
    assert stale.sentinel_note == SENTINEL_NOTES[USAGE_TOKEN_EXPIRED]
    assert stale.has_last_good
    assert stale.freshness is viewmodel.FreshnessState.STALE
    assert stale_rows["5h"].used_percent == 92.0
    assert stale_rows["5h"].reset_text is None
    assert stale_rows["7d"].used_percent == 0.0
    assert stale_rows["7d"].reset_text == "6d 0h"
    assert stale_rows["Fable"].used_percent == 0.0
    assert not stale_rows["Fable"].limit_reached
    assert not stale_rows["Fable"].ahead_of_pace

    assert unavailable.sentinel == USAGE_TOKEN_EXPIRED
    assert not unavailable.has_last_good
    assert unavailable.rows == ()
    assert unavailable.capacity_summary == "Capacity unavailable"
    assert unavailable.freshness is viewmodel.FreshnessState.UNAVAILABLE


def test_capacity_ignores_spend_and_freshness_does_not_overstate_age() -> None:
    entry = UsageEntry(
        last_good={
            "spend": {"pct": 90.0, "used": 90.0, "limit": 100.0},
            "five_hour": {"pct": 10.0, "resets_at": _iso(2 * 3600)},
        },
        fetched_at=_NOW - 200,
        age_s=200.0,
    )
    snapshot = AccountsSnapshot(
        active_number="1",
        accounts=(_account("1", entry, active=True),),
        taken_at=_NOW,
    )

    account = viewmodel.popover_view_model(snapshot, _NOW).accounts[0]

    assert account.freshness is viewmodel.FreshnessState.FRESH
    assert account.freshness_detail == "Updated 3m ago"
    assert account.capacity_summary == "90% left"


def test_capacity_summary_leads_with_a_rejected_probe_over_low_windows():
    row = viewmodel._usage_row(
        "5h",
        viewmodel.UsageScope.FIVE_HOUR,
        {"pct": 3.0},
        _NOW,
        None,
    )
    resets_at = datetime.fromtimestamp(_NOW + 3600, tz=timezone.utc).isoformat()

    assert viewmodel._capacity_summary((row,), {"resets_at": resets_at}, _NOW) == (
        "Rejected 1h 0m"
    )
    assert viewmodel._capacity_summary((row,), {}, _NOW) == "Rejected"
    assert viewmodel._capacity_summary((row,), None, _NOW) == "97% left"


def test_capacity_summary_does_not_report_room_on_an_expired_login():
    row = viewmodel._usage_row(
        "5h", viewmodel.UsageScope.FIVE_HOUR, {"pct": 0.0}, _NOW, None
    )

    assert (
        viewmodel._capacity_summary(
            (row,), None, _NOW, sentinel=viewmodel.USAGE_RELOGIN_REQUIRED
        )
        == "Login expired"
    )


def _compact_account(rows, *, sentinel=None, active=False, disabled=False):
    return viewmodel.PopoverAccountViewModel(
        number="2",
        email="person@example.test",
        alias="",
        display_name="person",
        is_active=active,
        disabled=disabled,
        freshness=viewmodel.FreshnessState.FRESH,
        freshness_detail="Updated just now",
        sentinel=sentinel,
        sentinel_note="re-login needed" if sentinel else None,
        has_last_good=True,
        capacity_summary="60% left",
        rows=rows,
        session_available=not active,
    )


def _compact_row(label, scope, percent):
    return viewmodel._usage_row(label, scope, {"pct": percent}, _NOW, None)


def test_compact_usage_rows_keep_windows_then_the_tightest_model_limit():
    scopes = viewmodel.UsageScope
    rows = (
        _compact_row("Spend", scopes.SPEND, 90.0),
        _compact_row("5h", scopes.FIVE_HOUR, 10.0),
        _compact_row("7d", scopes.SEVEN_DAY, 20.0),
        _compact_row("Sonnet", scopes.SCOPED, 30.0),
        _compact_row("Fable", scopes.SCOPED, 80.0),
    )

    compact = viewmodel.compact_usage_rows(_compact_account(rows))

    assert [row.label for row in compact] == ["5h", "7d", "Fable"]


def test_expired_login_shows_no_meters_and_cannot_be_activated():
    row = _compact_row("5h", viewmodel.UsageScope.FIVE_HOUR, 10.0)
    expired = _compact_account((row,), sentinel=viewmodel.USAGE_RELOGIN_REQUIRED)

    assert viewmodel.compact_usage_rows(expired) == ()
    assert not viewmodel.account_can_activate(expired)
    assert viewmodel.account_status_text(expired) == "Login expired"


def test_status_text_and_activation_follow_account_state():
    active = _compact_account((), active=True)
    disabled = _compact_account((), disabled=True)
    plain = _compact_account(())

    assert viewmodel.account_status_text(active) == "Active"
    assert not viewmodel.account_can_activate(active)
    assert viewmodel.account_status_text(disabled) == "Disabled"
    assert viewmodel.account_can_activate(disabled)
    assert viewmodel.account_status_text(plain) == "60% left"
    assert viewmodel.account_can_activate(plain)


def test_tooltip_keeps_what_the_compact_row_leaves_out():
    scopes = viewmodel.UsageScope
    resets_at = datetime.fromtimestamp(_NOW + 3600, tz=timezone.utc).isoformat()
    row = viewmodel._usage_row(
        "7d", scopes.SEVEN_DAY, {"pct": 40.0, "resets_at": resets_at}, _NOW, None
    )
    tooltip = viewmodel.account_tooltip(_compact_account((row,)))

    assert "person@example.test" in tooltip
    assert "Updated just now" in tooltip
    assert "7d: 40%, resets in 1h 0m" in tooltip
