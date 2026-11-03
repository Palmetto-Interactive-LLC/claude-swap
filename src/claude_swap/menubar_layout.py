"""Pure geometry planning for the native menu-bar popover.

All coordinates use a top-left origin.  Keeping the layout calculation separate
from AppKit makes the popover's fixed chrome, account-only scrolling, and
long-list behavior directly testable without a macOS runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

POPOVER_WIDTH = 376.0
POPOVER_MAX_HEIGHT = 352.0
HEADER_HEIGHT = 36.0
EMPTY_ACCOUNTS_DOCUMENT_HEIGHT = 48.0
INSET = 12.0
ROW_INSET = 6.0
ROW_GAP = 4.0
LIST_PADDING = 2.0
BUTTON_HEIGHT = 22.0
ICON_BUTTON_SIZE = 22.0
AVATAR_SIZE = 20.0
SESSION_BUTTON_SIZE = 18.0
USAGE_LINE_HEIGHT = 16.0
MAX_USAGE_LINES = 5

_ROW_WIDTH = POPOVER_WIDTH - 2 * ROW_INSET
_ROW_PADDING = 10.0
_TEXT_X = _ROW_PADDING + AVATAR_SIZE + 8.0
_RIGHT = _ROW_WIDTH - _ROW_PADDING
_STATUS_WIDTH = 58.0
_FIRST_LINE_Y = 28.0
_ROW_BOTTOM_PADDING = 6.0
_LABEL_WIDTH = 34.0
_PERCENT_WIDTH = 38.0
_TIME_LEFT_WIDTH = 56.0
_CLOCK_WIDTH = 72.0
_COLUMN_GAP = 4.0
_BAR_HEIGHT = 5.0


@dataclass(frozen=True)
class Rect:
    """An immutable top-down rectangle in popover coordinates."""

    x: float
    y: float
    width: float
    height: float

    @property
    def max_x(self) -> float:
        """Return the rectangle's right edge."""
        return self.x + self.width

    @property
    def max_y(self) -> float:
        """Return the rectangle's bottom edge."""
        return self.y + self.height

    def intersects(self, other: Rect) -> bool:
        """Return whether this rectangle overlaps ``other`` with positive area."""
        return (
            self.x < other.max_x
            and other.x < self.max_x
            and self.y < other.max_y
            and other.y < self.max_y
        )


@dataclass(frozen=True)
class UsageLineLayout:
    """One limit's aligned columns, in row coordinates."""

    label: Rect
    bar: Rect
    percent: Rect
    time_left: Rect
    clock: Rect


@dataclass(frozen=True)
class AccountRowLayout:
    """Frames for one account row. Children use the row's own coordinates."""

    frame: Rect
    avatar: Rect
    name: Rect
    status: Rect
    session_button: Rect
    detail: Rect
    usage_lines: tuple[UsageLineLayout, ...]


@dataclass(frozen=True)
class MainPopoverLayout:
    """The fixed main chrome and independently scrolling accounts region."""

    root: Rect
    header: Rect
    title: Rect
    refresh: Rect
    settings: Rect
    more: Rect
    message: Rect
    accounts_viewport: Rect
    accounts: tuple[AccountRowLayout, ...]
    accounts_document_height: float


@dataclass(frozen=True)
class ScrollScreenLayout:
    """Top-down scroll document geometry for secondary screens."""

    viewport: Rect
    back: Rect
    title: Rect
    items: tuple[Rect, ...]
    document_height: float


def button_row(frame: Rect, count: int, gap: float = 6.0) -> tuple[Rect, ...]:
    """Divide ``frame`` into ``count`` equal buttons on one line."""
    width = (frame.width - gap * (count - 1)) / count
    return tuple(
        Rect(frame.x + index * (width + gap), frame.y, width, BUTTON_HEIGHT)
        for index in range(count)
    )


def _usage_line(y: float) -> UsageLineLayout:
    clock_x = _RIGHT - _CLOCK_WIDTH
    time_x = clock_x - _COLUMN_GAP - _TIME_LEFT_WIDTH
    percent_x = time_x - _COLUMN_GAP - _PERCENT_WIDTH
    bar_x = _TEXT_X + _LABEL_WIDTH + _COLUMN_GAP
    bar_y = y + (USAGE_LINE_HEIGHT - _BAR_HEIGHT) / 2
    return UsageLineLayout(
        label=Rect(_TEXT_X, y + 1.0, _LABEL_WIDTH, 14.0),
        bar=Rect(bar_x, bar_y, percent_x - _COLUMN_GAP - bar_x, _BAR_HEIGHT),
        percent=Rect(percent_x, y + 1.0, _PERCENT_WIDTH, 14.0),
        time_left=Rect(time_x, y + 1.0, _TIME_LEFT_WIDTH, 14.0),
        clock=Rect(clock_x, y + 1.0, _CLOCK_WIDTH, 14.0),
    )


def plan_main_popover(usage_line_counts: Sequence[int]) -> MainPopoverLayout:
    """Plan the fixed main popover and its account-only scroll document.

    ``usage_line_counts`` holds each account's limit count, at most
    ``MAX_USAGE_LINES``. An account with none gets one detail line. Counts
    outside that range are rejected at this boundary rather than producing
    malformed AppKit frames.
    """
    if any(count < 0 or count > MAX_USAGE_LINES for count in usage_line_counts):
        raise ValueError(f"usage line counts must be 0-{MAX_USAGE_LINES}")

    rows: list[AccountRowLayout] = []
    y = LIST_PADDING
    for count in usage_line_counts:
        height = _FIRST_LINE_Y + max(count, 1) * USAGE_LINE_HEIGHT + _ROW_BOTTOM_PADDING
        status_x = _RIGHT - _STATUS_WIDTH
        session_x = status_x - 2.0 - SESSION_BUTTON_SIZE
        rows.append(
            AccountRowLayout(
                frame=Rect(ROW_INSET, y, _ROW_WIDTH, height),
                avatar=Rect(_ROW_PADDING, 6.0, AVATAR_SIZE, AVATAR_SIZE),
                name=Rect(_TEXT_X, 7.0, session_x - 2.0 - _TEXT_X, 17.0),
                status=Rect(status_x, 9.0, _STATUS_WIDTH, 14.0),
                session_button=Rect(session_x, 7.0, SESSION_BUTTON_SIZE, SESSION_BUTTON_SIZE),
                detail=Rect(_TEXT_X, _FIRST_LINE_Y + 1.0, _RIGHT - _TEXT_X, 14.0),
                usage_lines=tuple(
                    _usage_line(_FIRST_LINE_Y + index * USAGE_LINE_HEIGHT)
                    for index in range(count)
                ),
            )
        )
        y += height + ROW_GAP

    document_height = (
        max(EMPTY_ACCOUNTS_DOCUMENT_HEIGHT, y - ROW_GAP + LIST_PADDING + 4.0)
        if rows
        else EMPTY_ACCOUNTS_DOCUMENT_HEIGHT
    )
    viewport_height = min(document_height, POPOVER_MAX_HEIGHT - HEADER_HEIGHT)
    header = Rect(0.0, 0.0, POPOVER_WIDTH, HEADER_HEIGHT)
    icon_y = (HEADER_HEIGHT - ICON_BUTTON_SIZE) / 2
    more = Rect(POPOVER_WIDTH - 8.0 - ICON_BUTTON_SIZE, icon_y, ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
    settings = Rect(more.x - ICON_BUTTON_SIZE, icon_y, ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
    refresh = Rect(settings.x - ICON_BUTTON_SIZE, icon_y, ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
    title = Rect(INSET + 2.0, 10.0, refresh.x - INSET - 6.0, 16.0)
    # Feedback replaces the title, so the list never jumps when a switch reports.
    message = Rect(title.x, 4.0, title.width, 28.0)
    viewport = Rect(0.0, HEADER_HEIGHT, POPOVER_WIDTH, viewport_height)
    return MainPopoverLayout(
        root=Rect(0.0, 0.0, POPOVER_WIDTH, viewport.max_y),
        header=header,
        title=title,
        refresh=refresh,
        settings=settings,
        more=more,
        message=message,
        accounts_viewport=viewport,
        accounts=tuple(rows),
        accounts_document_height=document_height,
    )


def plan_scroll_screen(item_heights: Sequence[float]) -> ScrollScreenLayout:
    """Plan a secondary scroll document from ordered item heights."""
    if any(height <= 0 for height in item_heights):
        raise ValueError("scroll screen item heights must be positive")

    y = HEADER_HEIGHT
    items: list[Rect] = []
    for height in item_heights:
        items.append(Rect(INSET, y, POPOVER_WIDTH - 2 * INSET, height))
        y += height + 6.0
    document_height = max(HEADER_HEIGHT + INSET, y - 6.0 + INSET)
    viewport = Rect(0.0, 0.0, POPOVER_WIDTH, min(document_height, POPOVER_MAX_HEIGHT))
    back = Rect(8.0, (HEADER_HEIGHT - ICON_BUTTON_SIZE) / 2, ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
    title = Rect(back.max_x + 4.0, 10.0, POPOVER_WIDTH - back.max_x - 4.0 - INSET, 16.0)
    return ScrollScreenLayout(viewport, back, title, tuple(items), document_height)


__all__ = [
    "AVATAR_SIZE",
    "AccountRowLayout",
    "BUTTON_HEIGHT",
    "INSET",
    "MAX_USAGE_LINES",
    "MainPopoverLayout",
    "POPOVER_MAX_HEIGHT",
    "POPOVER_WIDTH",
    "Rect",
    "ScrollScreenLayout",
    "USAGE_LINE_HEIGHT",
    "UsageLineLayout",
    "button_row",
    "plan_main_popover",
    "plan_scroll_screen",
]
