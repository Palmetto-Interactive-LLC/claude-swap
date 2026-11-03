"""Native AppKit status item and compact account-switching popover.

This module deliberately imports PyObjC lazily.  Importing ``claude_swap`` on
Linux, Windows, or a macOS Python without the menu-bar extra remains safe; only
``run_native_menubar`` requires AppKit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.menubar_layout import (
    BUTTON_HEIGHT,
    POPOVER_MAX_HEIGHT,
    POPOVER_WIDTH,
    Rect,
    button_row,
    plan_main_popover,
    plan_scroll_screen,
)
from claude_swap.menubar_controller import MenuBarController
from claude_swap.menubar_viewmodel import (
    AUTO_THRESHOLD_CHOICES,
    ICON,
    REFRESH_CHOICES,
    TITLE_PCT_CHOICES,
    CapacityState,
    MenuBarPopoverViewModel,
    PopoverAccountViewModel,
    UsageRowViewModel,
    account_at_limit,
    account_can_activate,
    account_initial,
    account_login_expired,
    account_status_text,
    account_tooltip,
    compact_usage_rows,
    usage_time_left,
)

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

try:  # Do not make importing the CLI depend on PyObjC.
    import AppKit
    import Foundation
    import objc
except (ImportError, SystemError):
    # Some unsupported PyObjC/Python combinations raise SystemError while
    # loading the Objective-C bridge. Menu-bar mode remains optional.
    APPKIT_AVAILABLE = False
else:
    APPKIT_AVAILABLE = True


_native_host: tuple[object, object] | None = None

# Slot number picks the badge color, so an account keeps its color and six
# accounts that all start with "m" still read apart at a glance.
_BADGE_COLORS = (
    "systemIndigoColor",
    "systemTealColor",
    "systemPinkColor",
    "systemOrangeColor",
    "systemPurpleColor",
    "systemBlueColor",
    "systemMintColor",
    "systemBrownColor",
)


def require_appkit() -> None:
    """Raise the CLI-facing error when the optional native runtime is absent."""
    if not APPKIT_AVAILABLE:
        raise ClaudeSwitchError(
            "Menu bar mode requires PyObjC/AppKit. Install with: pip install "
            "'claude-swap[menubar]'"
        )


def _draw_status_icon() -> bool:
    """Draw the glyph: the active account's badge in front of the next one's ring."""
    oval = AppKit.NSBezierPath.bezierPathWithOvalInRect_
    ring = oval(AppKit.NSMakeRect(8.2, 6.6, 8.8, 8.8))
    ring.setLineWidth_(1.8)
    AppKit.NSColor.blackColor().setStroke()
    ring.stroke()
    context = AppKit.NSGraphicsContext.currentContext()
    context.setCompositingOperation_(AppKit.NSCompositingOperationClear)
    oval(AppKit.NSMakeRect(0.0, 0.6, 12.8, 12.8)).fill()
    context.setCompositingOperation_(AppKit.NSCompositingOperationSourceOver)
    AppKit.NSColor.blackColor().setFill()
    oval(AppKit.NSMakeRect(1.2, 1.8, 10.4, 10.4)).fill()
    return True


def _status_icon():
    """Template image, so macOS tints it for light, dark, and selected states."""
    image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
        AppKit.NSMakeSize(18.0, 18.0), False, lambda _rect: _draw_status_icon()
    )
    image.setTemplate_(True)
    image.setAccessibilityDescription_("Claude accounts")
    return image


def _title_text(title: str) -> str:
    """The status text beside the icon, without the plain-text fallback glyph."""
    return title.removeprefix(ICON).strip()


def _create_native_menubar(switcher: ClaudeAccountSwitcher):
    """Construct the native status-item app without entering its event loop."""
    global _native_host
    require_appkit()
    assert APPKIT_AVAILABLE  # narrows the optional module for type checkers
    if _native_host is not None:
        return _native_host

    class FlippedView(AppKit.NSView):
        """An AppKit view whose geometry reads from top to bottom."""

        def isFlipped(self) -> bool:
            return True

    class FilledView(FlippedView):
        """A rounded fill. Dynamic colors follow light and dark mode as it draws."""

        def drawRect_(self, _rect) -> None:
            color = getattr(self, "fill_color", None)
            if color is None:
                return
            radius = getattr(self, "corner_radius", 0.0)
            color.setFill()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                self.bounds(), radius, radius
            ).fill()

    class AccountRow(FilledView):
        """One account. Clicking anywhere outside its buttons switches to it."""

        def initWithFrame_(self, frame):
            self = objc.super(AccountRow, self).initWithFrame_(frame)
            if self is None:
                return None
            self.on_activate = None
            self.hover_views = ()
            self.resting_color = None
            self.fill_color = None
            self.corner_radius = 8.0
            self.addTrackingArea_(
                AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                    AppKit.NSZeroRect,
                    AppKit.NSTrackingMouseEnteredAndExited
                    | AppKit.NSTrackingActiveAlways
                    | AppKit.NSTrackingInVisibleRect,
                    self,
                    None,
                )
            )
            return self

        def acceptsFirstMouse_(self, _event) -> bool:
            return True

        def hitTest_(self, point):
            hit = objc.super(AccountRow, self).hitTest_(point)
            if hit is None or isinstance(hit, AppKit.NSButton):
                return hit
            return self

        @objc.python_method
        def _set_fill(self, color) -> None:
            self.fill_color = color
            self.setNeedsDisplay_(True)

        def mouseEntered_(self, _event) -> None:
            if self.on_activate is not None:
                self._set_fill(AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.07))
            for view in self.hover_views:
                view.setHidden_(False)

        def mouseExited_(self, _event) -> None:
            self._set_fill(self.resting_color)
            for view in self.hover_views:
                view.setHidden_(True)

        def mouseDown_(self, _event) -> None:
            if self.on_activate is not None:
                self._set_fill(AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.14))

        def mouseUp_(self, event) -> None:
            self._set_fill(self.resting_color)
            point = self.convertPoint_fromView_(event.locationInWindow(), None)
            if AppKit.NSPointInRect(point, self.bounds()):
                self.activate()

        @objc.python_method
        def activate(self) -> None:
            if self.on_activate is not None:
                self.on_activate()

    class PopoverContentController(AppKit.NSViewController):
        """Compact native renderer for the controller's immutable models."""

        def initWithHost_(self, host):
            self = objc.super(PopoverContentController, self).init()
            if self is None:
                return None
            self.host = host
            self._reset_main_scroll_on_render = False
            self.setView_(
                FlippedView.alloc().initWithFrame_(
                    AppKit.NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT)
                )
            )
            return self

        @objc.python_method
        def _label(
            self,
            text: str,
            frame: Rect,
            *,
            size: float = 11.0,
            weight: float = 0.0,
            color=None,
            align_right: bool = False,
            digits: bool = False,
            keep_ends: bool = False,
        ):
            field = AppKit.NSTextField.labelWithString_(text)
            field.setFrame_(AppKit.NSMakeRect(frame.x, frame.y, frame.width, frame.height))
            field.setFont_(
                AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
                if digits
                else AppKit.NSFont.systemFontOfSize_weight_(size, weight)
            )
            # Accounts often differ only by domain, so names keep both ends.
            field.setLineBreakMode_(
                AppKit.NSLineBreakByTruncatingMiddle
                if keep_ends
                else AppKit.NSLineBreakByTruncatingTail
            )
            if color is not None:
                field.setTextColor_(color)
            if align_right:
                field.setAlignment_(AppKit.NSTextAlignmentRight)
            return field

        @objc.python_method
        def _button(
            self,
            title: str,
            action: str,
            frame: Rect,
            *,
            slot: str | None = None,
            prominent: bool = False,
        ):
            button = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(frame.x, frame.y, frame.width, frame.height)
            )
            button.setTitle_(title)
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setControlSize_(AppKit.NSControlSizeSmall)
            button.setFont_(AppKit.NSFont.systemFontOfSize_(11.0))
            button.setTarget_(self.host)
            button.setAction_(action)
            if prominent:
                button.setBezelColor_(AppKit.NSColor.controlAccentColor())
            if slot is not None:
                button.setRepresentedObject_(slot)
            return button

        @objc.python_method
        def _icon_button(self, symbol: str, label: str, action: str, frame: Rect, *, slot: str | None = None):
            button = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(frame.x, frame.y, frame.width, frame.height)
            )
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, label)
            button.setImage_(image.imageWithSymbolConfiguration_(
                AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                    12.0, AppKit.NSFontWeightRegular
                )
            ))
            button.setTitle_("")
            button.setBordered_(False)
            button.setImagePosition_(AppKit.NSImageOnly)
            button.setContentTintColor_(AppKit.NSColor.secondaryLabelColor())
            button.setToolTip_(label)
            button.setTarget_(self.host)
            button.setAction_(action)
            if slot is not None:
                button.setRepresentedObject_(slot)
            return button

        @objc.python_method
        def _fill(self, color, frame: Rect, *, radius: float = 0.0):
            view = FilledView.alloc().initWithFrame_(
                AppKit.NSMakeRect(frame.x, frame.y, frame.width, frame.height)
            )
            view.fill_color = color
            view.corner_radius = radius
            return view

        @objc.python_method
        def _badge(self, account: PopoverAccountViewModel, frame: Rect, *, muted: bool):
            try:
                index = int(account.number) - 1
            except ValueError:
                index = 0
            color = getattr(AppKit.NSColor, _BADGE_COLORS[index % len(_BADGE_COLORS)])()
            if muted:
                color = color.colorWithAlphaComponent_(0.55)
            badge = self._fill(color, frame, radius=frame.width / 2)
            initial = self._label(
                account_initial(account),
                Rect(0.0, 3.0, frame.width, 14.0),
                size=11.0,
                weight=AppKit.NSFontWeightSemibold,
                color=AppKit.NSColor.whiteColor(),
            )
            initial.setAlignment_(AppKit.NSTextAlignmentCenter)
            badge.addSubview_(initial)
            return badge

        @objc.python_method
        def _usage_color(self, state: CapacityState):
            if state is CapacityState.LIMIT_REACHED:
                return AppKit.NSColor.systemRedColor()
            if state is CapacityState.NEAR_LIMIT:
                return AppKit.NSColor.systemOrangeColor()
            return AppKit.NSColor.systemGreenColor()

        @objc.python_method
        def _usage_line(self, row: UsageRowViewModel, line, parent) -> None:
            """One limit in aligned columns: name, bar, percent, time left, clock."""
            secondary = AppKit.NSColor.secondaryLabelColor()
            color = self._usage_color(row.state)
            parent.addSubview_(self._label(row.label, line.label, size=10.5, color=secondary))
            parent.addSubview_(self._fill(AppKit.NSColor.quaternaryLabelColor(), line.bar, radius=2.5))
            percent = max(0.0, min(100.0, row.used_percent))
            if percent > 0:
                width = max(5.0, line.bar.width * percent / 100.0)
                parent.addSubview_(
                    self._fill(color, Rect(line.bar.x, line.bar.y, width, line.bar.height), radius=2.5)
                )
            parent.addSubview_(
                self._label(
                    f"{row.used_percent:.0f}%",
                    line.percent,
                    size=10.5,
                    weight=AppKit.NSFontWeightMedium,
                    color=color,
                    align_right=True,
                    digits=True,
                )
            )
            if row.amount_text:
                parent.addSubview_(
                    self._label(
                        row.amount_text,
                        Rect(
                            line.time_left.x,
                            line.time_left.y,
                            line.clock.max_x - line.time_left.x,
                            line.time_left.height,
                        ),
                        size=10.5,
                        color=secondary,
                        align_right=True,
                        digits=True,
                    )
                )
                return
            parent.addSubview_(
                self._label(
                    usage_time_left(row),
                    line.time_left,
                    size=10.5,
                    color=secondary,
                    align_right=True,
                    digits=True,
                )
            )
            if row.reset_clock:
                parent.addSubview_(
                    self._label(
                        row.reset_clock,
                        line.clock,
                        size=10.5,
                        color=AppKit.NSColor.tertiaryLabelColor(),
                        align_right=True,
                        digits=True,
                    )
                )

        @objc.python_method
        def _account_name(self, account: PopoverAccountViewModel, frame: Rect, *, expired: bool):
            """The email at full strength with the plan quiet beside it."""
            field = self._label("", frame, keep_ends=True)
            name_color = AppKit.NSColor.secondaryLabelColor() if expired else AppKit.NSColor.labelColor()
            text = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(
                account.alias or account.email,
                {
                    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(
                        12.0, AppKit.NSFontWeightSemibold
                    ),
                    AppKit.NSForegroundColorAttributeName: name_color,
                },
            )
            if account.plan_label:
                text.appendAttributedString_(
                    AppKit.NSAttributedString.alloc().initWithString_attributes_(
                        f"  {account.plan_label}".replace(" ", "\u00a0"),
                        {
                            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(10.0),
                            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.tertiaryLabelColor(),
                        },
                    )
                )
            field.setAttributedStringValue_(text)
            return field

        @objc.python_method
        def _account_row(self, account: PopoverAccountViewModel, layout, document) -> None:
            frame = layout.frame
            row = AccountRow.alloc().initWithFrame_(
                AppKit.NSMakeRect(frame.x, frame.y, frame.width, frame.height)
            )
            document.addSubview_(row)
            expired = account_login_expired(account)
            if account.is_active:
                row.resting_color = AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.08)
                row.fill_color = row.resting_color
            else:
                slot = account.number
                row.on_activate = lambda: self.host.activate_slot(slot)
            row.setToolTip_(account_tooltip(account))
            row.addSubview_(self._badge(account, layout.avatar, muted=expired))
            row.addSubview_(self._account_name(account, layout.name, expired=expired))
            status = account_status_text(account)
            if status:
                if account.is_active:
                    status_color = AppKit.NSColor.systemGreenColor()
                elif expired:
                    status_color = AppKit.NSColor.systemOrangeColor()
                elif account_at_limit(account) or status.startswith("Rejected"):
                    status_color = AppKit.NSColor.systemRedColor()
                else:
                    status_color = AppKit.NSColor.secondaryLabelColor()
                row.addSubview_(
                    self._label(
                        status,
                        layout.status,
                        size=10.5,
                        weight=AppKit.NSFontWeightMedium,
                        color=status_color,
                        align_right=True,
                    )
                )
            usage = compact_usage_rows(account)
            for usage_row, line in zip(usage, layout.usage_lines, strict=True):
                self._usage_line(usage_row, line, row)
            if not usage:
                if expired:
                    detail = "Click to sign in and switch"
                elif account.sentinel:
                    detail = account.sentinel_note or account.sentinel
                else:
                    detail = account.freshness_detail
                row.addSubview_(
                    self._label(detail, layout.detail, size=10.5, color=AppKit.NSColor.tertiaryLabelColor())
                )
            if account.session_available and not expired:
                # Only on hover: a rarely used action shouldn't sit on every row.
                session = self._icon_button(
                    "terminal",
                    "New Terminal session on this account",
                    "isolatedSession:",
                    layout.session_button,
                    slot=account.number,
                )
                session.setHidden_(True)
                row.hover_views = (session,)
                row.addSubview_(session)

        @objc.python_method
        def _scroll_document(self, root, viewport: Rect, document_height: float, *, identifier: str):
            scroll = AppKit.NSScrollView.alloc().initWithFrame_(
                AppKit.NSMakeRect(viewport.x, viewport.y, viewport.width, viewport.height)
            )
            scroll.setIdentifier_(identifier)
            scroll.setHasVerticalScroller_(True)
            scroll.setAutohidesScrollers_(True)
            scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
            scroll.setDrawsBackground_(False)
            document = FlippedView.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, viewport.width, document_height)
            )
            scroll.setDocumentView_(document)
            root.addSubview_(scroll)
            return scroll, document

        @objc.python_method
        def reset_main_scroll_on_next_render(self) -> None:
            """Start the next popover opening at the top of the account list."""
            self._reset_main_scroll_on_render = True

        @objc.python_method
        def _resize_root(self, root, frame: Rect) -> None:
            root.setFrameSize_(AppKit.NSMakeSize(frame.width, frame.height))
            self.host.popover.setContentSize_(AppKit.NSMakeSize(frame.width, frame.height))

        @objc.python_method
        def _main_layout(self, model: MenuBarPopoverViewModel):
            return plan_main_popover(
                tuple(len(compact_usage_rows(account)) for account in model.accounts),
            )

        @objc.python_method
        def _can_rotate(self, model: MenuBarPopoverViewModel) -> bool:
            # Rotation only checks that a slot has stored credentials, so with
            # nothing alive to move to it could land on a dead refresh token.
            return any(account_can_activate(account) for account in model.accounts)

        @objc.python_method
        def _render_main(self, root, model: MenuBarPopoverViewModel, scroll_offset: float, layout) -> None:
            message = self.host.last_message
            if message:
                field = self._label(
                    message,
                    layout.message,
                    size=10.0,
                    color=AppKit.NSColor.systemRedColor()
                    if message.startswith("Couldn't")
                    else AppKit.NSColor.secondaryLabelColor(),
                )
                field.setMaximumNumberOfLines_(2)
                field.cell().setWraps_(True)
                field.setToolTip_(message)
                root.addSubview_(field)
            else:
                root.addSubview_(
                    self._label(
                        "Claude accounts",
                        layout.title,
                        size=13.0,
                        weight=AppKit.NSFontWeightSemibold,
                    )
                )
            for symbol, label, action, frame in (
                ("arrow.clockwise", "Refresh usage", "refreshNow:", layout.refresh),
                ("gearshape", "Settings", "showSettings:", layout.settings),
                ("ellipsis.circle", "Manage accounts", "showOverflow:", layout.more),
            ):
                root.addSubview_(self._icon_button(symbol, label, action, frame))
            scroll, document = self._scroll_document(
                root,
                layout.accounts_viewport,
                layout.accounts_document_height,
                identifier="accounts",
            )
            if model.accounts:
                for account, account_layout in zip(model.accounts, layout.accounts, strict=True):
                    self._account_row(account, account_layout, document)
            else:
                document.addSubview_(
                    self._label(
                        "No accounts yet. Add your login from the ⋯ menu.",
                        Rect(14.0, 16.0, POPOVER_WIDTH - 28.0, 16.0),
                        color=AppKit.NSColor.secondaryLabelColor(),
                    )
                )
            maximum_offset = max(0.0, layout.accounts_document_height - layout.accounts_viewport.height)
            scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0.0, min(scroll_offset, maximum_offset)))
            scroll.reflectScrolledClipView_(scroll.contentView())

        @objc.python_method
        def _secondary_header(self, document, layout, title: str, back_action: str) -> None:
            document.addSubview_(self._icon_button("chevron.left", "Back", back_action, layout.back))
            document.addSubview_(
                self._label(title, layout.title, size=13.0, weight=AppKit.NSFontWeightSemibold)
            )

        @objc.python_method
        def _overflow_heights(self, model: MenuBarPopoverViewModel) -> list[float]:
            return [BUTTON_HEIGHT for _ in model.accounts] + [BUTTON_HEIGHT] * 4

        @objc.python_method
        def _overflow(self, document, model: MenuBarPopoverViewModel) -> None:
            layout = plan_scroll_screen(self._overflow_heights(model))
            self._secondary_header(document, layout, "Manage accounts", "showMain:")
            for account, frame in zip(model.accounts, layout.items, strict=False):
                buttons_width = 140.0
                document.addSubview_(
                    self._label(
                        account.alias or account.email,
                        Rect(frame.x, frame.y + 3.0, frame.width - buttons_width - 6.0, 16.0),
                        keep_ends=True,
                    )
                )
                state, remove = button_row(
                    Rect(frame.max_x - buttons_width, frame.y, buttons_width, BUTTON_HEIGHT), 2
                )
                state_title = "Enable" if account.disabled else "Disable"
                document.addSubview_(self._button(state_title, "toggleDisabled:", state, slot=account.number))
                document.addSubview_(self._button("Remove", "removeAccount:", remove, slot=account.number))
            controls = layout.items[len(model.accounts):]
            can_rotate = self._can_rotate(model)
            for (title, action, tip), frame in zip(
                (
                    ("Most room", "switchBest:", "Switch to the account with the most room"),
                    ("Next with room", "nextAvailable:", "Switch to the next account with room"),
                    ("Next in order", "rotateNext:", "Switch to the next account in order"),
                ),
                button_row(controls[0], 3),
                strict=True,
            ):
                button = self._button(title, action, frame)
                button.setToolTip_(tip)
                button.setEnabled_(can_rotate)
                document.addSubview_(button)
            controls = controls[1:]
            pairs = (
                (("Add current login", "addCurrent:"), ("Add setup token", "addToken:")),
                (("Refresh login", "refreshCredentials:"), ("Switch history", "showHistory:")),
                (("Show log file", "revealLog:"), ("Quit", "quit:")),
            )
            for frame, pair in zip(controls, pairs, strict=True):
                for (title, action), button_frame in zip(pair, button_row(frame, 2), strict=True):
                    document.addSubview_(self._button(title, action, button_frame))

        @objc.python_method
        def _choices(self, document, frame: Rect, label: str, options, action: str) -> None:
            document.addSubview_(
                self._label(
                    label,
                    Rect(frame.x, frame.y, frame.width, 14.0),
                    size=10.0,
                    color=AppKit.NSColor.secondaryLabelColor(),
                )
            )
            row = Rect(frame.x, frame.y + 16.0, frame.width, BUTTON_HEIGHT)
            for (title, value, selected), button_frame in zip(
                options, button_row(row, len(options)), strict=True
            ):
                document.addSubview_(
                    self._button(
                        f"✓ {title}" if selected else title,
                        action,
                        button_frame,
                        slot=value,
                        prominent=selected,
                    )
                )

        @objc.python_method
        def _settings_heights(self) -> list[float]:
            return [BUTTON_HEIGHT, 38.0, 38.0, BUTTON_HEIGHT, 38.0]

        @objc.python_method
        def _settings(self, document) -> None:
            layout = plan_scroll_screen(self._settings_heights())
            self._secondary_header(document, layout, "Settings", "showMain:")
            settings = self.host.controller.settings
            name, scoped = button_row(layout.items[0], 2)
            document.addSubview_(
                self._button(
                    "Hide account name" if settings.show_account_name else "Show account name",
                    "toggleAccountName:",
                    name,
                )
            )
            document.addSubview_(
                self._button(
                    "Hide model limits" if settings.title_scoped else "Show model limits",
                    "toggleScoped:",
                    scoped,
                )
            )
            self._choices(
                document,
                layout.items[1],
                "Usage in menu bar",
                tuple(
                    ({"off": "Off", "5h": "5h", "7d": "7d", "both": "Both"}[mode], mode, settings.title_pct == mode)
                    for mode in TITLE_PCT_CHOICES
                ),
                "setTitlePercent:",
            )
            self._choices(
                document,
                layout.items[2],
                "Refresh usage every",
                tuple(
                    (
                        f"{seconds // 60} min" if seconds >= 60 else f"{seconds} sec",
                        str(seconds),
                        settings.refresh_interval == seconds,
                    )
                    for seconds in REFRESH_CHOICES
                ),
                "setRefreshInterval:",
            )
            document.addSubview_(
                self._button(
                    "Auto-switch: on" if settings.auto_switch_enabled else "Auto-switch: off",
                    "toggleAutoSwitch:",
                    layout.items[3],
                    prominent=settings.auto_switch_enabled,
                )
            )
            threshold = self.host.controller.auto_switch_threshold()
            self._choices(
                document,
                layout.items[4],
                "Auto-switch at",
                tuple((f"{percent}%", str(percent), threshold == percent) for percent in AUTO_THRESHOLD_CHOICES),
                "setThreshold:",
            )

        @objc.python_method
        def _history_heights(self) -> list[float]:
            entries = self.host.controller.history
            heights = [16.0 for _ in entries] if entries else [16.0]
            heights.append(BUTTON_HEIGHT)
            return heights

        @objc.python_method
        def _history(self, document) -> None:
            entries = self.host.controller.history
            layout = plan_scroll_screen(self._history_heights())
            self._secondary_header(document, layout, "Switch history", "showOverflow:")
            if entries:
                for entry, frame in zip(entries, layout.items, strict=False):
                    field = self._label(entry, frame, size=10.0, digits=True)
                    field.setToolTip_(entry)
                    document.addSubview_(field)
            else:
                document.addSubview_(
                    self._label(
                        "No switches yet",
                        layout.items[0],
                        color=AppKit.NSColor.secondaryLabelColor(),
                    )
                )
            document.addSubview_(self._button("Show log file", "revealLog:", layout.items[-1]))

        def renderModel_screen_(self, model, screen: str) -> None:
            root = self.view()
            scroll_offset = 0.0
            if screen == "main" and not self._reset_main_scroll_on_render:
                for child in root.subviews():
                    if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts":
                        scroll_offset = child.documentVisibleRect().origin.y
                        break
            self._reset_main_scroll_on_render = False
            for child in tuple(root.subviews()):
                child.removeFromSuperview()
            if screen == "main":
                layout = self._main_layout(model)
                self._resize_root(root, layout.root)
                self._render_main(root, model, scroll_offset, layout)
                return
            if screen == "overflow":
                item_heights = self._overflow_heights(model)
            elif screen == "settings":
                item_heights = self._settings_heights()
            else:
                item_heights = self._history_heights()
            layout = plan_scroll_screen(item_heights)
            self._resize_root(root, layout.viewport)
            _scroll, document = self._scroll_document(
                root, layout.viewport, layout.document_height, identifier=screen
            )
            if screen == "overflow":
                self._overflow(document, model)
            elif screen == "settings":
                self._settings(document)
            else:
                self._history(document)

    class NativeMenuBarHost(Foundation.NSObject):
        """AppKit target object. It confirms destructive intent, then delegates all work."""

        def initWithSwitcher_(self, initial_switcher):
            self = objc.super(NativeMenuBarHost, self).init()
            if self is None:
                return None
            self.screen = "main"
            self.last_message = ""
            self.controller = MenuBarController(
                initial_switcher,
                dispatch_main=self._dispatch_main,
            )
            self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSVariableStatusItemLength
            )
            button = self.status_item.button()
            button.setImage_(_status_icon())
            button.setImagePosition_(AppKit.NSImageLeading)
            button.setTarget_(self)
            button.setAction_("togglePopover:")
            self.content = PopoverContentController.alloc().initWithHost_(self)
            self.popover = AppKit.NSPopover.alloc().init()
            self.popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
            self.popover.setContentViewController_(self.content)
            self.popover.setContentSize_(AppKit.NSMakeSize(POPOVER_WIDTH, POPOVER_MAX_HEIGHT))
            self.popover.setDelegate_(self)
            self.controller.bind_ui(self._render, self._message)
            self._install_refresh_timer()
            self._install_active_account_watcher()
            self.controller.start()
            return self

        @objc.python_method
        def _dispatch_main(self, callback: Callable[[], None]) -> None:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(callback)

        @objc.python_method
        def _render(self, model: MenuBarPopoverViewModel, title: str) -> None:
            self.status_item.button().setTitle_(_title_text(title))
            timer = getattr(self, "refresh_timer", None)
            if timer is not None and int(round(timer.timeInterval())) != self.controller.settings.refresh_interval:
                self._install_refresh_timer()
            self.content.renderModel_screen_(model, self.screen)

        @objc.python_method
        def _message(self, title: str, message: str) -> None:
            # Keep feedback that arrives while the popover is closed until it
            # has been seen; popoverDidClose_ clears it.
            self.last_message = f"{title}: {message}"
            self.screen = "main"
            self._render(self.controller.view_model, self.controller.title)

        def popoverDidClose_(self, _notification) -> None:
            self.last_message = ""

        @objc.python_method
        def _install_refresh_timer(self, seconds: int | None = None) -> None:
            old_timer = getattr(self, "refresh_timer", None)
            if old_timer is not None:
                old_timer.invalidate()
            interval = seconds if seconds is not None else self.controller.settings.refresh_interval
            self.refresh_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                float(interval), self, "refreshTick:", None, True
            )

        def refreshTick_(self, _timer) -> None:
            self.controller.refresh_async()

        @objc.python_method
        def _install_active_account_watcher(self) -> None:
            self.active_account_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self, "activeAccountTick:", None, True
            )

        def activeAccountTick_(self, _timer) -> None:
            self.controller.detect_external_active_account()

        def togglePopover_(self, _sender) -> None:
            if self.popover.isShown():
                self.popover.performClose_(self)
                return
            self.screen = "main"
            self.content.reset_main_scroll_on_next_render()
            self._render(self.controller.view_model, self.controller.title)
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                self.status_item.button().bounds(), self.status_item.button(), AppKit.NSMinYEdge
            )

        @objc.python_method
        def _confirm(self, title: str, message: str, action: str) -> bool:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            alert.addButtonWithTitle_(action)
            alert.addButtonWithTitle_("Cancel")
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            return alert.runModal() == AppKit.NSAlertFirstButtonReturn

        @objc.python_method
        def activate_slot(self, slot: str) -> None:
            """Switch in one click: routine and reversible, so no confirmation.

            A signed-out account can't take over the live login until it has a
            working credential, so its click signs in first and then switches.

            The render is deferred because this runs inside the clicked row's
            own mouse handler, and rendering replaces that row.
            """
            account = next((item for item in self.controller.view_model.accounts if item.number == slot), None)
            if account is None or account.is_active:
                return
            name = account.alias or account.email
            if account_login_expired(account):
                self.last_message = f"Sign in as {name} in your browser"
                self.controller.sign_in_and_activate(slot)
            else:
                self.last_message = f"Switching to {name}"
                # The controller worker calls only switcher.switch_to(slot, json_output=True).
                self.controller.make_active(slot)
            self._dispatch_main(
                lambda: self._render(self.controller.view_model, self.controller.title)
            )

        def isolatedSession_(self, sender) -> None:
            slot = str(sender.representedObject())
            if self._confirm("Open Terminal session", f"Open Terminal with cswap run {slot}?", "Open"):
                # This deliberately hands the isolated session to the local CLI process.
                self.controller.launch_isolated_session(slot)

        def rotateNext_(self, _sender) -> None:
            self.controller.rotate(None)

        def switchBest_(self, _sender) -> None:
            self.controller.rotate("best")

        def nextAvailable_(self, _sender) -> None:
            self.controller.rotate("next-available")

        def addCurrent_(self, _sender) -> None:
            self.controller.add_current_login()

        def addToken_(self, _sender) -> None:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Add account from setup token")
            alert.setInformativeText_("Enter the account email and setup token.")
            form = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 330, 52))
            email = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 28, 330, 24))
            email.setPlaceholderString_("Email")
            token = AppKit.NSSecureTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 330, 24))
            token.setPlaceholderString_("sk-ant-oat01-…")
            form.addSubview_(email)
            form.addSubview_(token)
            alert.setAccessoryView_(form)
            alert.addButtonWithTitle_("Add")
            alert.addButtonWithTitle_("Cancel")
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            if alert.runModal() == AppKit.NSAlertFirstButtonReturn and email.stringValue().strip() and token.stringValue().strip():
                self.controller.add_setup_token(email.stringValue().strip(), token.stringValue().strip())

        def refreshCredentials_(self, _sender) -> None:
            self.controller.refresh_current_credentials()

        def showOverflow_(self, _sender) -> None:
            self.screen = "overflow"
            self._render(self.controller.view_model, self.controller.title)

        def showSettings_(self, _sender) -> None:
            self.screen = "settings"
            self._render(self.controller.view_model, self.controller.title)

        def showHistory_(self, _sender) -> None:
            self.screen = "history"
            self.controller.load_history_async()
            self._render(self.controller.view_model, self.controller.title)

        def showMain_(self, _sender) -> None:
            self.screen = "main"
            self._render(self.controller.view_model, self.controller.title)

        def revealLog_(self, _sender) -> None:
            self.controller.reveal_log_async()

        def toggleDisabled_(self, sender) -> None:
            slot = str(sender.representedObject())
            account = next((item for item in self.controller.view_model.accounts if item.number == slot), None)
            if account is not None:
                self.controller.set_disabled(slot, not account.disabled)

        def removeAccount_(self, sender) -> None:
            slot = str(sender.representedObject())
            if self._confirm("Remove account", f"Remove account {slot}? This cannot be undone.", "Remove"):
                self.controller.remove_account(slot)

        def toggleAccountName_(self, _sender) -> None:
            self.controller.update_title_preferences(
                show_account_name=not self.controller.settings.show_account_name
            )

        def toggleScoped_(self, _sender) -> None:
            self.controller.update_title_preferences(title_scoped=not self.controller.settings.title_scoped)

        def setTitlePercent_(self, sender) -> None:
            self.controller.update_title_preferences(title_pct=str(sender.representedObject()))

        def setRefreshInterval_(self, sender) -> None:
            seconds = int(str(sender.representedObject()))
            self.controller.set_refresh_interval(seconds)
            self._install_refresh_timer(seconds)

        def toggleAutoSwitch_(self, _sender) -> None:
            self.controller.set_auto_switch_enabled(not self.controller.settings.auto_switch_enabled)

        def setThreshold_(self, sender) -> None:
            self.controller.set_auto_switch_threshold(int(str(sender.representedObject())))

        def refreshNow_(self, _sender) -> None:
            self.controller.refresh_async(full=True)

        def quit_(self, _sender) -> None:
            self._message("Quitting", "Finishing any active account operation before exit.")
            self.controller.stop(
                lambda: AppKit.NSApplication.sharedApplication().terminate_(self)
            )

    application = AppKit.NSApplication.sharedApplication()
    application.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    host = NativeMenuBarHost.alloc().initWithSwitcher_(switcher)
    # Retain the target object for the lifetime of the app; AppKit targets are weak.
    application.setDelegate_(host)
    _native_host = (application, host)
    return _native_host


def run_native_menubar(switcher: ClaudeAccountSwitcher) -> int:
    """Run the native status-item app until the user quits it."""
    application, _host = _create_native_menubar(switcher)
    application.run()
    return 0


__all__ = ["APPKIT_AVAILABLE", "require_appkit", "run_native_menubar"]
