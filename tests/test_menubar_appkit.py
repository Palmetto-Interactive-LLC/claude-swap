"""Native AppKit layout tests, skipped when the optional framework is absent."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from claude_swap import menubar_appkit
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.menubar_layout import POPOVER_MAX_HEIGHT, POPOVER_WIDTH
from claude_swap.menubar_viewmodel import (
    CapacityState,
    FreshnessState,
    MenuBarPopoverViewModel,
    MenuBarSettings,
    PopoverAccountViewModel,
    UsageRowViewModel,
    UsageScope,
)


def _model(account_count: int, rows_per_account: int = 2) -> MenuBarPopoverViewModel:
    row = UsageRowViewModel(
        label="5h",
        scope=UsageScope.FIVE_HOUR,
        used_percent=40.0,
        available_percent=60.0,
        reset_text="2h 0m",
        state=CapacityState.AVAILABLE,
        state_label="Available",
    )
    accounts = tuple(
        PopoverAccountViewModel(
            number=str(index + 1),
            email=f"account-{index + 1}@example.test",
            alias="",
            display_name=f"account-{index + 1}",
            is_active=index == 0,
            disabled=False,
            freshness=FreshnessState.FRESH,
            freshness_detail="Updated just now",
            sentinel=None,
            sentinel_note=None,
            has_last_good=True,
            capacity_summary="60% left",
            rows=(row,) * rows_per_account,
            session_available=index != 0,
        )
        for index in range(account_count)
    )
    return MenuBarPopoverViewModel(accounts, "1" if accounts else None)


def _intersects(first, second) -> bool:
    return (
        first.origin.x < second.origin.x + second.size.width
        and second.origin.x < first.origin.x + first.size.width
        and first.origin.y < second.origin.y + second.size.height
        and second.origin.y < first.origin.y + first.size.height
    )


@unittest.skipUnless(menubar_appkit.APPKIT_AVAILABLE, "requires macOS PyObjC/AppKit")
class NativeMenuBarAppKitSmokeTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        cached = getattr(cls, "_native_host", None)
        if cached is None:
            return
        application, host = cached
        host.refresh_timer.invalidate()
        import AppKit

        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(host.status_item)
        application.setDelegate_(None)

    def _host(self, model: MenuBarPopoverViewModel):
        class FakeController:
            def __init__(self, *_args, **_kwargs) -> None:
                self.settings = MenuBarSettings()
                self.view_model = model
                self.title = "⇄"
                self.history = []

            def bind_ui(self, renderer, _message) -> None:
                renderer(self.view_model, self.title)

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def auto_switch_threshold(self) -> int:
                return 90

        cached = getattr(self.__class__, "_native_host", None)
        if cached is not None:
            return cached
        with patch.object(menubar_appkit, "MenuBarController", FakeController):
            cached = menubar_appkit._create_native_menubar(object())
        self.__class__._native_host = cached
        return cached

    def _button_titles(self, view) -> list[str]:
        import AppKit

        titles: list[str] = []
        for child in view.subviews():
            if isinstance(child, AppKit.NSButton):
                titles.append(str(child.title()))
            titles.extend(self._button_titles(child))
        return titles

    def test_native_status_item_can_be_created_and_removed(self) -> None:
        import AppKit

        application = AppKit.NSApplication.sharedApplication()
        status_bar = AppKit.NSStatusBar.systemStatusBar()
        item = status_bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        try:
            item.button().setTitle_("⇄")
            self.assertEqual(item.button().title(), "⇄")
        finally:
            status_bar.removeStatusItem_(item)
        self.assertIsNotNone(application)

    def _subviews(self, view) -> list:
        found = []
        for child in view.subviews():
            found.append(child)
            found.extend(self._subviews(child))
        return found

    def _accounts_document(self, host):
        import AppKit

        return next(
            child
            for child in host.content.view().subviews()
            if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
        ).documentView()

    def _titles(self, host) -> list[str]:
        return self._button_titles(host.content.view())

    def test_status_item_uses_a_template_image_not_a_text_glyph(self) -> None:
        _application, host = self._host(_model(1))
        host._render(_model(1), "⇄ matt 5h 10%")

        button = host.status_item.button()
        self.assertTrue(button.image().isTemplate())
        self.assertEqual(str(button.title()), "matt 5h 10%")
        host._render(_model(1), "⇄")
        self.assertEqual(str(button.title()), "")

    def test_clicking_a_row_switches_in_one_step_with_no_alert(self) -> None:
        model = _model(2)
        _application, host = self._host(model)
        host.controller.view_model = model
        host.content.renderModel_screen_(model, "main")
        active_row, other_row = self._accounts_document(host).subviews()

        with (
            patch.object(host, "_confirm", side_effect=AssertionError("must not confirm")),
            patch.object(host.controller, "make_active", create=True) as make_active,
        ):
            active_row.activate()
            make_active.assert_not_called()
            other_row.activate()
        make_active.assert_called_once_with("2")
        self.assertEqual(host.last_message, "Switching to account-2@example.test")
        host.last_message = ""

    def test_row_keeps_clicks_except_on_its_buttons(self) -> None:
        import AppKit

        model = _model(2)
        _application, host = self._host(model)
        host.content.renderModel_screen_(model, "main")
        _active_row, other_row = self._accounts_document(host).subviews()

        origin = other_row.frame().origin
        label_point = AppKit.NSMakePoint(origin.x + 50.0, origin.y + 12.0)
        self.assertEqual(other_row.hitTest_(label_point), other_row)
        session = next(view for view in other_row.subviews() if isinstance(view, AppKit.NSButton))
        session_frame = session.frame()
        button_point = AppKit.NSMakePoint(
            origin.x + session_frame.origin.x + 4.0, origin.y + session_frame.origin.y + 4.0
        )
        self.assertEqual(other_row.hitTest_(button_point), session)

    def test_expired_account_offers_log_in_and_cannot_be_activated(self) -> None:
        """One-click switching must not put a dead refresh token in the live login."""
        base = _model(3)
        dead = replace(base.accounts[1], sentinel=USAGE_RELOGIN_REQUIRED)
        model = replace(base, accounts=(base.accounts[0], dead, base.accounts[2]))
        _application, host = self._host(model)
        host.controller.view_model = model
        host.content.renderModel_screen_(model, "main")

        dead_row = self._accounts_document(host).subviews()[1]
        with patch.object(host.controller, "make_active", create=True) as make_active:
            dead_row.activate()
            host.activate_slot("2")
        make_active.assert_not_called()
        self.assertEqual(self._button_titles(dead_row), ["Log in"])

    def test_rotation_buttons_are_hidden_with_nothing_alive_to_move_to(self) -> None:
        base = _model(2)
        dead = replace(base.accounts[1], sentinel=USAGE_RELOGIN_REQUIRED)
        model = replace(base, accounts=(base.accounts[0], dead))
        _application, host = self._host(model)

        host.content.renderModel_screen_(model, "main")
        for title in ("Best", "Next", "Rotate"):
            self.assertNotIn(title, self._titles(host))

        host.content.renderModel_screen_(base, "main")
        for title in ("Best", "Next", "Rotate"):
            self.assertIn(title, self._titles(host))

    def test_message_replaces_the_title_without_moving_the_list(self) -> None:
        import AppKit

        model = _model(2)
        _application, host = self._host(model)
        host.content.renderModel_screen_(model, "main")
        before = host.content.view().frame().size.height
        host.controller.view_model = model

        host._message("Couldn't launch isolated session", "Terminal access was denied.")
        fields = [
            str(child.stringValue())
            for child in host.content.view().subviews()
            if isinstance(child, AppKit.NSTextField)
        ]
        self.assertTrue(any(text.startswith("Couldn't launch isolated session:") for text in fields))
        self.assertNotIn("Claude accounts", fields)
        self.assertEqual(host.content.view().frame().size.height, before)

        host.popoverDidClose_(None)
        self.assertEqual(host.last_message, "")

    def test_native_popover_constructs_fixed_chrome_and_flipped_accounts_document(self) -> None:
        """The main screen keeps only account rows inside its scroll view."""

        import AppKit

        model = _model(2, rows_per_account=2)
        _application, host = self._host(model)
        host.content.renderModel_screen_(model, "main")
        root = host.content.view()
        self.assertLess(root.frame().size.height, POPOVER_MAX_HEIGHT)
        self.assertEqual(host.popover.contentSize().height, root.frame().size.height)
        scrolls = [child for child in root.subviews() if isinstance(child, AppKit.NSScrollView)]
        self.assertEqual(len(scrolls), 1)
        accounts_scroll = scrolls[0]
        self.assertEqual(accounts_scroll.identifier(), "accounts")
        document = accounts_scroll.documentView()
        self.assertTrue(document.isFlipped())

        root_buttons = [child for child in root.subviews() if isinstance(child, AppKit.NSButton)]
        self.assertEqual(
            {str(button.action()) for button in root_buttons},
            {
                "refreshNow:",
                "showSettings:",
                "showOverflow:",
                "switchBest:",
                "nextAvailable:",
                "rotateNext:",
            },
        )
        self.assertTrue(all(not _intersects(accounts_scroll.frame(), button.frame()) for button in root_buttons))

        rows = tuple(document.subviews())
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.isFlipped() for row in rows))
        self.assertFalse(_intersects(rows[0].frame(), rows[1].frame()))

    def test_active_row_has_no_session_button_and_others_do(self) -> None:
        model = _model(2)
        _application, host = self._host(model)
        host.content.renderModel_screen_(model, "main")
        active_row, other_row = self._accounts_document(host).subviews()

        self.assertEqual(self._button_titles(active_row), [])
        self.assertEqual(self._button_titles(other_row), [""])

    def test_every_secondary_screen_renders_within_the_popover_width(self) -> None:
        import AppKit

        model = _model(3)
        _application, host = self._host(model)
        host.controller.history = ["a to b"]
        for screen in ("overflow", "settings", "history"):
            host.content.renderModel_screen_(model, screen)
            document = next(
                child for child in host.content.view().subviews() if isinstance(child, AppKit.NSScrollView)
            ).documentView()
            for child in document.subviews():
                frame = child.frame()
                self.assertGreaterEqual(frame.origin.x, 0.0)
                self.assertLessEqual(frame.origin.x + frame.size.width, POPOVER_WIDTH)

    def test_long_account_list_scrolls_only_the_flipped_account_document(self) -> None:
        import AppKit

        application, host = self._host(_model(12, rows_per_account=4))
        host.content.renderModel_screen_(_model(12, rows_per_account=4), "main")
        try:
            root = host.content.view()
            self.assertEqual(root.frame().size.height, POPOVER_MAX_HEIGHT)
            self.assertEqual(host.popover.contentSize().height, POPOVER_MAX_HEIGHT)
            accounts_scroll = next(
                child
                for child in root.subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            )
            document = accounts_scroll.documentView()
            self.assertGreater(document.frame().size.height, accounts_scroll.frame().size.height)
            self.assertEqual(len([child for child in root.subviews() if isinstance(child, AppKit.NSScrollView)]), 1)
            self.assertTrue(all(group.frame().origin.y >= 0.0 for group in document.subviews()))
            self.assertTrue(
                all(
                    group.frame().origin.y + group.frame().size.height <= document.frame().size.height
                    for group in document.subviews()
                )
            )
            for first, second in zip(document.subviews(), document.subviews()[1:], strict=False):
                self.assertFalse(_intersects(first.frame(), second.frame()))
        finally:
            pass

    def test_refresh_keeps_account_scroll_but_popover_open_resets_it(self) -> None:
        import AppKit

        model = _model(12, rows_per_account=4)
        application, host = self._host(model)
        try:
            host.content.renderModel_screen_(model, "main")
            scroll = next(
                child
                for child in host.content.view().subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            )
            scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0.0, 100.0))
            scroll.reflectScrolledClipView_(scroll.contentView())
            host.content.renderModel_screen_(model, "main")
            refreshed_scroll = next(
                child
                for child in host.content.view().subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            )
            self.assertEqual(refreshed_scroll.documentVisibleRect().origin.y, 100.0)

            host.content.reset_main_scroll_on_next_render()
            host.content.renderModel_screen_(model, "main")
            reopened_scroll = next(
                child
                for child in host.content.view().subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            )
            self.assertEqual(reopened_scroll.documentVisibleRect().origin.y, 0.0)
        finally:
            pass

    def test_secondary_screens_use_flipped_top_down_scroll_documents(self) -> None:
        import AppKit

        model = _model(10, rows_per_account=1)
        application, host = self._host(model)
        try:
            host.content.renderModel_screen_(model, "overflow")
            scroll = next(child for child in host.content.view().subviews() if isinstance(child, AppKit.NSScrollView))
            document = scroll.documentView()
            self.assertTrue(document.isFlipped())
            self.assertGreaterEqual(document.frame().size.height, scroll.frame().size.height)
            self.assertTrue(all(child.frame().origin.y >= 0.0 for child in document.subviews()))

            host.controller.history = [f"{index} → {index + 1}" for index in range(40)]
            host.content.renderModel_screen_(model, "history")
            history_scroll = next(
                child for child in host.content.view().subviews() if isinstance(child, AppKit.NSScrollView)
            )
            self.assertTrue(history_scroll.documentView().isFlipped())
            self.assertGreater(
                history_scroll.documentView().frame().size.height, history_scroll.frame().size.height
            )
        finally:
            pass
