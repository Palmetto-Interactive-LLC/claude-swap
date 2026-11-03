"""Controller contract tests without importing AppKit."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from claude_swap.autoswitch import SwitchEvent
from claude_swap.macos_terminal import TerminalLaunchResult
from claude_swap.menubar_controller import MenuBarController
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.usage_store import UsageEntry


class _SnapshotSource:
    def __init__(self, snapshot: AccountsSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[bool, bool]] = []

    def take(self, *, full: bool = False, store_only: bool = False) -> AccountsSnapshot:
        self.calls.append((full, store_only))
        return self.snapshot


class _Switcher:
    def __init__(self, backup_dir: Path) -> None:
        self.backup_dir = backup_dir
        self.config_path = backup_dir / ".claude.json"
        self.calls: list[tuple[object, ...]] = []
        self.current_account: tuple[str, str] | None = ("one@example.test", "")
        self.current_account_reads = 0
        self.probe_threads: list[int] = []
        self.switch_to_result: dict[str, object] = {
            "switched": True,
            "message": "Switched to Account-2 (two@example.test)",
            "warnings": [],
        }
        self.switch_result: dict[str, object] = {
            "switched": True,
            "message": "Switched account",
            "warnings": [],
        }

    def _get_claude_config_path(self) -> Path:
        self.probe_threads.append(threading.get_ident())
        return self.config_path

    def _get_current_account(self) -> tuple[str, str] | None:
        self.current_account_reads += 1
        self.probe_threads.append(threading.get_ident())
        return self.current_account

    def switch_to(self, slot: str, *, json_output: bool = False) -> dict[str, object]:
        self.calls.append(("switch_to", slot, json_output))
        return self.switch_to_result

    def switch(
        self, *, strategy: str | None = None, json_output: bool = False
    ) -> dict[str, object]:
        self.calls.append(("switch", strategy, json_output))
        return self.switch_result

    def add_account(self, *, slot: int | None = None) -> None:
        self.calls.append(("add_account", slot))

    def add_account_from_token(self, *, token: str, email: str, slot: int | None = None) -> None:
        self.calls.append(("add_account_from_token", token, email, slot))

    def set_account_disabled(self, slot: str, disabled: bool) -> None:
        self.calls.append(("set_account_disabled", slot, disabled))

    def remove_account(self, slot: str, *, assume_yes: bool) -> None:
        self.calls.append(("remove_account", slot, assume_yes))


def _snapshot() -> AccountsSnapshot:
    return AccountsSnapshot(
        active_number="1",
        accounts=(
            AccountSnapshot(
                number="1",
                email="one@example.test",
                org_name="",
                org_uuid="",
                is_active=True,
                kind="oauth",
                switchable=True,
                usage=UsageEntry(last_good={"five_hour": {"pct": 10.0}}, age_s=1.0),
            ),
        ),
        taken_at=1.0,
    )


def _wait_for(event: threading.Event) -> None:
    assert event.wait(1), "serialized worker did not complete"


def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("condition did not become true")


def test_make_active_calls_only_direct_switch_to(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    rendered.clear()

    controller.make_active("2")

    _wait_for(rendered)
    assert switcher.calls == [("switch_to", "2", True)]
    controller.stop()


def test_isolated_session_uses_terminal_launcher_not_session_manager(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    launched = threading.Event()
    slots: list[str] = []

    def terminal(slot: str) -> TerminalLaunchResult:
        slots.append(slot)
        launched.set()
        return TerminalLaunchResult(launched=True, slot=slot, command=f"cswap run {slot}")

    controller = MenuBarController(switcher, snapshot_source=source, terminal_launcher=terminal)
    controller.launch_isolated_session("4")

    _wait_for(launched)
    assert slots == ["4"]
    assert switcher.calls == []
    controller.stop()


def test_rotate_uses_json_output_without_implicitly_capturing_a_login(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    rendered.clear()

    controller.rotate("next-available")

    _wait_for(rendered)
    assert ("switch", "next-available", True) in switcher.calls
    controller.stop()


def test_json_switch_noop_message_and_warnings_are_rendered(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    switcher.switch_result = {
        "switched": False,
        "message": "Already on the account with the most remaining quota.",
        "warnings": ["Skipped Account-2 (disabled)"],
    }
    messages: list[tuple[str, str]] = []
    notified = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=_SnapshotSource(_snapshot()))

    def message(title: str, detail: str) -> None:
        messages.append((title, detail))
        notified.set()

    controller.bind_ui(lambda _model, _title: None, message)
    controller.rotate("best")

    _wait_for(notified)
    assert messages == [
        (
            "Account unchanged",
            "Already on the account with the most remaining quota. "
            "Warning: Skipped Account-2 (disabled)",
        )
    ]
    controller.stop()


def test_live_session_warning_is_rendered_after_json_switch(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    live_session_warning = (
        "Account-2 has a live session-mode Claude instance; its credential copy "
        "can go stale."
    )
    switcher.switch_to_result = {
        "switched": True,
        "message": "Switched to Account-2 (two@example.test)",
        "warnings": [live_session_warning],
    }
    messages: list[tuple[str, str]] = []
    notified = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=_SnapshotSource(_snapshot()))

    def message(title: str, detail: str) -> None:
        messages.append((title, detail))
        notified.set()

    controller.bind_ui(lambda _model, _title: None, message)
    controller.make_active("2")

    _wait_for(notified)
    assert messages[0][0] == "Switch warning"
    assert messages[0][1].startswith(live_session_warning)
    assert messages[0][1].endswith("Switch takes effect within about 30 seconds.")
    controller.stop()


def test_auto_switch_event_renders_credential_drift_warning(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    messages: list[tuple[str, str]] = []
    controller = MenuBarController(switcher, snapshot_source=_SnapshotSource(_snapshot()))
    controller.bind_ui(
        lambda _model, _title: None,
        lambda title, detail: messages.append((title, detail)),
    )
    warning = "Live session credential copy can drift."

    controller._handle_engine_event(
        SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "one@example.test"},
            to_ref={"number": 2, "email": "two@example.test"},
            warnings=[warning],
        )
    )

    assert messages[0][0] == "Auto-switch warning"
    assert messages[0][1].startswith(warning)
    controller.stop()


def test_snapshot_refresh_uses_store_only_while_engine_is_running(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    rendered.clear()
    # The controller only needs a tracked engine thread to choose store-only mode.
    engine = object()
    controller._engine_threads[engine] = threading.current_thread()  # type: ignore[index]

    controller.refresh_async(full=True)

    _wait_for(rendered)
    assert source.calls == [(True, True)]
    assert controller.view_model.active_number == "1"
    controller._engine_threads.pop(engine)  # type: ignore[arg-type]
    controller.stop()


def test_external_active_account_change_requests_a_store_governed_refresh(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    controller._worker_succeeded(_snapshot())
    rendered.clear()
    switcher.current_account = ("two@example.test", "")

    controller.detect_external_active_account()

    _wait_for(rendered)
    assert source.calls == [(False, False)]
    controller.stop()


def test_external_unmanaged_login_refreshes_once_without_a_watcher_loop(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    switcher.current_account = ("outside@example.test", "")
    unmanaged = AccountsSnapshot(
        active_number=None,
        accounts=(
            AccountSnapshot(
                number="1",
                email="one@example.test",
                org_name="",
                org_uuid="",
                is_active=False,
                kind="oauth",
                switchable=True,
                usage=UsageEntry(),
            ),
        ),
        taken_at=1.0,
    )
    source = _SnapshotSource(unmanaged)
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    controller._worker_succeeded(unmanaged)
    rendered.clear()

    controller.detect_external_active_account()
    _wait_for(rendered)
    controller.detect_external_active_account()
    threading.Event().wait(0.05)

    assert source.calls == [(False, False)]
    controller.stop()


def test_external_account_probe_stats_before_parsing_and_runs_off_main_thread(
    tmp_path: Path,
) -> None:
    switcher = _Switcher(tmp_path)
    switcher.config_path.write_text('{"oauthAccount": {}}', encoding="utf-8")
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    controller._worker_succeeded(_snapshot())
    rendered.clear()
    main_thread = threading.get_ident()

    controller.detect_external_active_account()
    _wait_until(
        lambda: switcher.current_account_reads == 1
        and not controller._active_probe_in_flight
    )
    controller.detect_external_active_account()
    _wait_until(lambda: not controller._active_probe_in_flight)

    assert switcher.current_account_reads == 1
    assert switcher.probe_threads
    assert all(thread_id != main_thread for thread_id in switcher.probe_threads)
    assert source.calls == []

    switcher.current_account = ("two@example.test", "")
    switcher.config_path.write_text(
        '{"oauthAccount": {"emailAddress": "two@example.test"}}',
        encoding="utf-8",
    )
    controller.detect_external_active_account()

    _wait_for(rendered)
    assert switcher.current_account_reads == 2
    assert source.calls == [(False, False)]
    controller.stop()


def test_explicit_refresh_surfaces_a_snapshot_error(tmp_path: Path) -> None:
    class FailingSnapshotSource:
        def take(self, *, full: bool = False, store_only: bool = False) -> AccountsSnapshot:
            raise RuntimeError("quota endpoint unavailable")

    switcher = _Switcher(tmp_path)
    messages: list[tuple[str, str]] = []
    controller = MenuBarController(switcher, snapshot_source=FailingSnapshotSource())
    controller.bind_ui(lambda _model, _title: None, lambda title, message: messages.append((title, message)))

    controller.refresh_async(full=True)

    for _ in range(20):
        if messages:
            break
        threading.Event().wait(0.05)
    assert messages == [("Couldn't refresh quota data", "quota endpoint unavailable")]
    controller.stop()


def test_engine_restart_waits_for_old_thread_and_quit_waits_for_replacement(
    tmp_path: Path,
) -> None:
    instances: list[ControlledEngine] = []

    class ControlledEngine:
        def __init__(self, *_args, **_kwargs) -> None:
            self.entered = threading.Event()
            self.stop_called = threading.Event()
            self.release = threading.Event()
            instances.append(self)

        def run_loop(self) -> None:
            self.entered.set()
            assert self.release.wait(1), "engine credential operation was not released"

        def stop(self) -> None:
            self.stop_called.set()

    switcher = _Switcher(tmp_path)
    controller = MenuBarController(switcher, snapshot_source=_SnapshotSource(_snapshot()))
    controller.settings.auto_switch_enabled = True

    with patch("claude_swap.menubar_controller.AutoSwitchEngine", ControlledEngine):
        controller._start_engine_worker()
        old = instances[0]
        _wait_for(old.entered)

        controller.settings.auto_switch_enabled = False
        controller._stop_engine_worker()
        assert old.stop_called.is_set()

        controller.settings.auto_switch_enabled = True
        controller._start_engine_worker()
        assert len(instances) == 1

        old.release.set()
        _wait_until(lambda: len(instances) == 2 and instances[1].entered.is_set())
        replacement = instances[1]
        completed = threading.Event()

        controller.stop(completed.set)

        assert replacement.stop_called.is_set()
        assert not completed.is_set()
        replacement.release.set()
        _wait_for(completed)


def test_load_history_renders_updated_history_on_main_dispatch(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    log_path = tmp_path / "claude-swap.log"
    log_path.write_text(
        "2026-07-20 10:00:00 - Switched from account 1 to 2\n", encoding="utf-8"
    )
    dispatched = threading.Event()
    callbacks: list[Callable[[], None]] = []
    rendered_history: list[tuple[str, ...]] = []

    def dispatch_main(callback: Callable[[], None]) -> None:
        callbacks.append(callback)
        dispatched.set()

    controller = MenuBarController(
        switcher,
        snapshot_source=source,
        log_path=log_path,
        dispatch_main=dispatch_main,
    )
    controller.bind_ui(
        lambda _model, _title: rendered_history.append(controller.history),
        lambda _title, _message: None,
    )
    rendered_history.clear()

    controller.load_history_async()

    _wait_for(dispatched)
    assert rendered_history == []
    callbacks.pop()()
    assert controller.history
    assert rendered_history == [controller.history]
    controller.stop()


class _SignInSwitcher(_Switcher):
    """Records the store writes a sign-in may make."""

    def __init__(self, backup_dir: Path) -> None:
        super().__init__(backup_dir)
        self.writes: list[tuple[str, str, str]] = []
        self.cleared: list[list[str]] = []
        self._usage_store = self

    def resolve_account(self, slot: str) -> tuple[str, str, str]:
        return slot, "two@example.test", "org-2"

    def _write_account_credentials(self, number: str, email: str, credentials: str) -> None:
        self.writes.append((number, email, credentials))

    def clear_dead_token(self, nums: list[str], _identities: dict) -> None:
        self.cleared.append(list(nums))


def _signed_in(identity, credentials="new-login", exit_code=0):
    def run(login_dir: Path, _email: str, _on_start) -> int:
        assert login_dir.is_dir()
        return exit_code

    return run, patch.multiple(
        "claude_swap.menubar_controller",
        read_session_identity=lambda _path: identity,
        read_session_credentials=lambda _path: credentials,
    )


def _sign_in(tmp_path: Path, runner) -> tuple[_SignInSwitcher, list[tuple[str, str]]]:
    switcher = _SignInSwitcher(tmp_path)
    messages: list[tuple[str, str]] = []
    finished = threading.Event()

    def message(title: str, text: str) -> None:
        messages.append((title, text))
        finished.set()

    controller = MenuBarController(
        switcher, snapshot_source=_SnapshotSource(_snapshot()), login_runner=runner
    )
    controller.bind_ui(lambda _model, _title: None, message)
    controller.sign_in_and_activate("2")
    _wait_for(finished)
    _wait_until(lambda: not controller._logins)
    controller.stop()
    return switcher, messages


def test_sign_in_stores_the_login_clears_the_strike_and_switches(tmp_path: Path) -> None:
    runner, reads = _signed_in(("Two@Example.test", "org-2"))
    with reads:
        switcher, messages = _sign_in(tmp_path, runner)

    assert switcher.writes == [("2", "two@example.test", "new-login")]
    assert switcher.cleared == [["2"]]
    assert switcher.calls == [("switch_to", "2", True)]
    assert messages[0][0] == "Account switched"
    assert not (tmp_path / "logins" / "2").exists()


def test_sign_in_as_another_account_changes_nothing(tmp_path: Path) -> None:
    runner, reads = _signed_in(("other@example.test", "org-2"))
    with reads:
        switcher, messages = _sign_in(tmp_path, runner)

    assert switcher.writes == []
    assert switcher.calls == []
    assert messages == [
        ("Couldn't sign in", "You signed in as other@example.test, not two@example.test. Try again.")
    ]
    assert not (tmp_path / "logins" / "2").exists()


def test_sign_in_with_another_organization_changes_nothing(tmp_path: Path) -> None:
    runner, reads = _signed_in(("two@example.test", "org-other"))
    with reads:
        switcher, messages = _sign_in(tmp_path, runner)

    assert switcher.writes == []
    assert switcher.calls == []
    assert messages[0][0] == "Couldn't sign in"


def test_abandoned_sign_in_changes_nothing(tmp_path: Path) -> None:
    runner, reads = _signed_in(("two@example.test", "org-2"), exit_code=1)
    with reads:
        switcher, messages = _sign_in(tmp_path, runner)

    assert switcher.writes == []
    assert switcher.calls == []
    assert messages == [("Couldn't sign in", "Sign-in didn't finish. Click the account to try again.")]
    assert not (tmp_path / "logins" / "2").exists()


def test_a_second_click_does_not_start_a_second_sign_in(tmp_path: Path) -> None:
    switcher = _SignInSwitcher(tmp_path)
    started = threading.Event()
    release = threading.Event()
    runs: list[str] = []

    def runner(_login_dir: Path, email: str, _on_start) -> int:
        runs.append(email)
        started.set()
        release.wait(2)
        return 1

    controller = MenuBarController(
        switcher, snapshot_source=_SnapshotSource(_snapshot()), login_runner=runner
    )
    controller.bind_ui(lambda _model, _title: None, lambda _title, _message: None)
    controller.sign_in_and_activate("2")
    _wait_for(started)
    controller.sign_in_and_activate("2")
    release.set()
    _wait_until(lambda: not controller._logins)
    controller.stop()

    assert runs == ["two@example.test"]


def test_quitting_stops_a_waiting_sign_in(tmp_path: Path) -> None:
    switcher = _SignInSwitcher(tmp_path)
    tracked = threading.Event()
    terminated = threading.Event()

    class _Process:
        def terminate(self) -> None:
            terminated.set()

    def runner(_login_dir: Path, _email: str, on_start) -> int:
        on_start(_Process())
        tracked.set()
        terminated.wait(2)
        return 1

    controller = MenuBarController(
        switcher, snapshot_source=_SnapshotSource(_snapshot()), login_runner=runner
    )
    controller.bind_ui(lambda _model, _title: None, lambda _title, _message: None)
    controller.sign_in_and_activate("2")
    _wait_for(tracked)
    controller.stop()

    assert terminated.is_set()
    _wait_until(lambda: not controller._logins)


def test_a_deferred_failure_still_reaches_the_user(tmp_path: Path) -> None:
    """The real app queues callbacks on the main thread, after `except` has ended."""
    switcher = _SignInSwitcher(tmp_path)
    queue: list[Callable[[], None]] = []
    messages: list[tuple[str, str]] = []
    failed = threading.Event()

    def boom(*_args, **_kwargs):
        failed.set()
        raise RuntimeError("no such account")

    switcher.switch_to = boom
    controller = MenuBarController(
        switcher,
        snapshot_source=_SnapshotSource(_snapshot()),
        dispatch_main=queue.append,
    )
    controller.bind_ui(lambda _model, _title: None, lambda title, text: messages.append((title, text)))

    controller.make_active("2")
    _wait_for(failed)
    _wait_until(lambda: queue)
    queue.pop(0)()

    assert messages == [("Couldn't switch account", "no such account")]
    controller.stop()


def test_a_deferred_sign_in_failure_still_reaches_the_user(tmp_path: Path) -> None:
    switcher = _SignInSwitcher(tmp_path)
    queue: list[Callable[[], None]] = []
    messages: list[tuple[str, str]] = []

    def runner(_login_dir: Path, _email: str, _on_start) -> int:
        raise RuntimeError("no browser")

    controller = MenuBarController(
        switcher,
        snapshot_source=_SnapshotSource(_snapshot()),
        dispatch_main=queue.append,
        login_runner=runner,
    )
    controller.bind_ui(lambda _model, _title: None, lambda title, text: messages.append((title, text)))

    controller.sign_in_and_activate("2")
    _wait_until(lambda: queue)
    queue.pop(0)()

    assert messages == [("Couldn't sign in", "no browser")]
    controller.stop()
