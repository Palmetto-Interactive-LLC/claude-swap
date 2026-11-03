"""Pure geometry planning for the native menu-bar popover.

All coordinates use a top-left origin.  Keeping the layout calculation separate
from AppKit makes the popover's fixed chrome, account-only scrolling, and
long-list behavior directly testable without a macOS runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

POPOVER_WIDTH = 340.0
POPOVER_MAX_HEIGHT = 360.0
HEADER_HEIGHT = 36.0
FOOTER_HEIGHT = 38.0
EMPTY_ACCOUNTS_DOCUMENT_HEIGHT = 48.0
INSET = 12.0
ROW_INSET = 6.0
ROW_HEIGHT = 44.0
ROW_GAP = 2.0
LIST_PADDING = 2.0
BUTTON_HEIGHT = 22.0
ICON_BUTTON_SIZE = 22.0
AVATAR_SIZE = 24.0
SESSION_BUTTON_WIDTH = 20.0
MAX_USAGE_CELLS = 3

_ROW_WIDTH = POPOVER_WIDTH - 2 * ROW_INSET
_ROW_PADDING = 8.0
_TEXT_X = _ROW_PADDING + AVATAR_SIZE + 8.0
_STATUS_WIDTH = 72.0
_CELL_GAP = 6.0
_DETAIL_Y = 21.0
_BAR_Y = 34.0
_BAR_HEIGHT = 3.0


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
class UsageCellLayout:
    """One usage meter: a text line above a thin bar, in row coordinates."""

    text: Rect
    bar: Rect


@dataclass(frozen=True)
class AccountRowLayout:
    """Frames for one account row. Children use the row's own coordinates."""

    frame: Rect
    avatar: Rect
    name: Rect
    status: Rect
    detail: Rect
    session_button: Rect
    usage_cells: tuple[UsageCellLayout, ...]


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
    footer: Rect | None
    rotate_next: Rect | None
    switch_best: Rect | None
    next_available: Rect | None
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


def _usage_cells(count: int) -> tuple[UsageCellLayout, ...]:
    if count == 0:
        return ()
    available = (
        _ROW_WIDTH - _TEXT_X - _ROW_PADDING - SESSION_BUTTON_WIDTH - _CELL_GAP
    )
    width = (available - _CELL_GAP * (count - 1)) / count
    cells: list[UsageCellLayout] = []
    for index in range(count):
        x = _TEXT_X + index * (width + _CELL_GAP)
        cells.append(
            UsageCellLayout(
                text=Rect(x, _DETAIL_Y, width, 12.0),
                bar=Rect(x, _BAR_Y, width, _BAR_HEIGHT),
            )
        )
    return tuple(cells)


def plan_main_popover(
    usage_cell_counts: Sequence[int],
    *,
    has_footer: bool = True,
) -> MainPopoverLayout:
    """Plan the fixed main popover and its account-only scroll document.

    ``usage_cell_counts`` holds each account's meter count, at most
    ``MAX_USAGE_CELLS``. Counts outside that range are rejected at this boundary
    rather than producing malformed AppKit frames.
    """
    if any(count < 0 or count > MAX_USAGE_CELLS for count in usage_cell_counts):
        raise ValueError(f"usage cell counts must be 0-{MAX_USAGE_CELLS}")

    rows: list[AccountRowLayout] = []
    y = LIST_PADDING
    for count in usage_cell_counts:
        status_x = _ROW_WIDTH - _ROW_PADDING - _STATUS_WIDTH
        action_right = _ROW_WIDTH - _ROW_PADDING
        rows.append(
            AccountRowLayout(
                frame=Rect(ROW_INSET, y, _ROW_WIDTH, ROW_HEIGHT),
                avatar=Rect(_ROW_PADDING, 10.0, AVATAR_SIZE, AVATAR_SIZE),
                name=Rect(_TEXT_X, 4.0, status_x - _TEXT_X - 4.0, 16.0),
                status=Rect(status_x, 5.0, _STATUS_WIDTH, 14.0),
                detail=Rect(_TEXT_X, _DETAIL_Y + 2.0, action_right - _TEXT_X, 14.0),
                session_button=Rect(
                    action_right - SESSION_BUTTON_WIDTH,
                    21.0,
                    SESSION_BUTTON_WIDTH,
                    18.0,
                ),
                usage_cells=_usage_cells(count),
            )
        )
        y += ROW_HEIGHT + ROW_GAP

    document_height = (
        max(EMPTY_ACCOUNTS_DOCUMENT_HEIGHT, y - ROW_GAP + LIST_PADDING)
        if rows
        else EMPTY_ACCOUNTS_DOCUMENT_HEIGHT
    )
    footer_height = FOOTER_HEIGHT if has_footer else 0.0
    chrome = HEADER_HEIGHT + footer_height
    viewport_height = min(document_height, POPOVER_MAX_HEIGHT - chrome)
    header = Rect(0.0, 0.0, POPOVER_WIDTH, HEADER_HEIGHT)
    icon_y = (HEADER_HEIGHT - ICON_BUTTON_SIZE) / 2
    more = Rect(POPOVER_WIDTH - 8.0 - ICON_BUTTON_SIZE, icon_y, ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
    settings = Rect(more.x - ICON_BUTTON_SIZE, icon_y, ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
    refresh = Rect(settings.x - ICON_BUTTON_SIZE, icon_y, ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
    title = Rect(INSET + 2.0, 10.0, refresh.x - INSET - 6.0, 16.0)
    # Feedback replaces the title, so the list never jumps when a switch reports.
    message = Rect(title.x, 4.0, title.width, 28.0)
    viewport = Rect(0.0, HEADER_HEIGHT, POPOVER_WIDTH, viewport_height)
    footer = None
    rotate_next = switch_best = next_available = None
    if has_footer:
        footer = Rect(0.0, viewport.max_y, POPOVER_WIDTH, FOOTER_HEIGHT)
        switch_best, next_available, rotate_next = button_row(
            Rect(INSET, footer.y + 8.0, POPOVER_WIDTH - 2 * INSET, BUTTON_HEIGHT), 3
        )
    root_height = viewport.max_y + footer_height
    return MainPopoverLayout(
        root=Rect(0.0, 0.0, POPOVER_WIDTH, root_height),
        header=header,
        title=title,
        refresh=refresh,
        settings=settings,
        more=more,
        message=message,
        accounts_viewport=viewport,
        footer=footer,
        rotate_next=rotate_next,
        switch_best=switch_best,
        next_available=next_available,
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
    "MAX_USAGE_CELLS",
    "MainPopoverLayout",
    "POPOVER_MAX_HEIGHT",
    "POPOVER_WIDTH",
    "ROW_HEIGHT",
    "Rect",
    "ScrollScreenLayout",
    "UsageCellLayout",
    "button_row",
    "plan_main_popover",
    "plan_scroll_screen",
]
