"""Pure geometry contracts for the AppKit menu-bar popover."""

from __future__ import annotations

import pytest

from claude_swap.menubar_layout import (
    BUTTON_HEIGHT,
    COLOR_BAD,
    COLOR_GOOD,
    COLOR_WARN,
    MAX_USAGE_LINES,
    MainPopoverLayout,
    POPOVER_MAX_HEIGHT,
    POPOVER_WIDTH,
    Rect,
    USAGE_LINE_HEIGHT,
    button_row,
    plan_main_popover,
    plan_scroll_screen,
)


def _assert_disjoint(frames: tuple[Rect, ...]) -> None:
    for index, frame in enumerate(frames):
        for other in frames[index + 1 :]:
            assert not frame.intersects(other)


def _assert_main_chrome(layout: MainPopoverLayout) -> None:
    assert layout.accounts_viewport.y == layout.header.max_y
    assert layout.accounts_document_height >= layout.accounts_viewport.height
    assert layout.root.height == layout.accounts_viewport.max_y


def _assert_contained(child: Rect, width: float, height: float) -> None:
    assert 0 <= child.x
    assert 0 <= child.y
    assert child.max_x <= width
    assert child.max_y <= height


def test_popover_is_well_under_half_the_original_430_by_620_area() -> None:
    assert POPOVER_WIDTH * POPOVER_MAX_HEIGHT <= 0.5 * 430.0 * 620.0


def test_the_email_and_plan_get_room_on_the_first_line() -> None:
    row = plan_main_popover((2,)).accounts[0]

    assert row.name.width >= 234.0


def test_the_columns_fit_a_full_percent_and_a_full_reset_clock() -> None:
    line = plan_main_popover((1,)).accounts[0].usage_lines[0]

    assert line.percent.width >= 38.0
    assert line.clock.width >= 72.0
    assert line.bar.width >= 80.0


def test_two_accounts_with_three_limits_stay_compact() -> None:
    layout = plan_main_popover((3, 3))

    _assert_main_chrome(layout)
    assert layout.root.height < 0.7 * POPOVER_MAX_HEIGHT
    assert layout.accounts_document_height == layout.accounts_viewport.height


def test_main_layout_handles_no_accounts_without_a_tall_artificial_document() -> None:
    layout = plan_main_popover(())

    _assert_main_chrome(layout)
    assert layout.accounts == ()
    assert layout.root.height < POPOVER_MAX_HEIGHT
    assert layout.accounts_document_height == layout.accounts_viewport.height == 48.0


@pytest.mark.parametrize("lines", range(MAX_USAGE_LINES + 1))
def test_row_children_stay_inside_the_row(lines: int) -> None:
    row = plan_main_popover((lines,)).accounts[0]

    assert len(row.usage_lines) == lines
    assert row.frame.width + 2 * row.frame.x == POPOVER_WIDTH
    for child in (row.avatar, row.name, row.status, row.session_button, row.detail):
        _assert_contained(child, row.frame.width, row.frame.height)
    columns = tuple(
        frame
        for line in row.usage_lines
        for frame in (line.label, line.bar, line.percent, line.time_left, line.clock)
    )
    for frame in columns:
        _assert_contained(frame, row.frame.width, row.frame.height)
    _assert_disjoint((row.avatar, row.name, row.session_button, row.status))
    _assert_disjoint((*columns, row.avatar, row.name, row.status))


def test_a_row_grows_by_one_line_per_limit() -> None:
    one, three = plan_main_popover((1, 3)).accounts

    assert three.frame.height - one.frame.height == 2 * USAGE_LINE_HEIGHT


def test_a_row_with_no_limits_still_has_a_line_for_its_detail() -> None:
    none, one = plan_main_popover((0, 1)).accounts

    assert none.frame.height == one.frame.height


def test_rows_do_not_overlap_and_only_the_list_grows_for_long_lists() -> None:
    layout = plan_main_popover((3, 0, 2, 1) * 5)

    _assert_main_chrome(layout)
    assert layout.root.height == POPOVER_MAX_HEIGHT
    assert layout.accounts_document_height > layout.accounts_viewport.height
    _assert_disjoint(tuple(row.frame for row in layout.accounts))
    assert all(row.frame.max_y <= layout.accounts_document_height for row in layout.accounts)


def test_header_controls_do_not_overlap_the_title_or_message() -> None:
    layout = plan_main_popover((1,))

    _assert_disjoint((layout.title, layout.refresh, layout.settings, layout.more))
    _assert_disjoint((layout.message, layout.refresh, layout.settings, layout.more))
    assert layout.message.max_y <= layout.header.max_y


def test_button_row_divides_a_frame_evenly() -> None:
    first, second = button_row(Rect(10.0, 20.0, 106.0, BUTTON_HEIGHT), 2)

    assert first.width == second.width == 50.0
    assert second.max_x == 116.0
    assert first.y == second.y == 20.0


def test_secondary_screen_fits_short_content_and_scrolls_long_content() -> None:
    short = plan_scroll_screen((22.0,))
    assert short.viewport.height == short.document_height < POPOVER_MAX_HEIGHT

    long = plan_scroll_screen((22.0,) * 50)
    assert long.items[0].y >= long.title.max_y
    assert all(item.y >= 0 for item in long.items)
    assert long.viewport.height == POPOVER_MAX_HEIGHT
    assert long.document_height > long.viewport.height


@pytest.mark.parametrize(
    "planner, values",
    [
        (plan_main_popover, (-1,)),
        (plan_main_popover, (MAX_USAGE_LINES + 1,)),
        (plan_scroll_screen, (0.0,)),
    ],
)
def test_planners_reject_invalid_item_sizes(planner, values) -> None:
    with pytest.raises(ValueError):
        planner(values)


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])


def _contrast(hex_color: str, background: tuple[float, float, float]) -> float:
    rgb = tuple(int(hex_color[index : index + 2], 16) for index in (1, 3, 5))
    high, low = sorted((_luminance(rgb), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# Measured from a screenshot of the backed popover over a mid-tone desktop.
_MEASURED_ROW = (47.0, 51.0, 62.0)
_MEASURED_ACTIVE_ROW = (65.0, 69.0, 76.0)


@pytest.mark.parametrize("pair", [COLOR_GOOD, COLOR_WARN, COLOR_BAD])
def test_status_colors_are_readable_on_the_dark_panel(pair) -> None:
    assert _contrast(pair[0], _MEASURED_ROW) >= 4.5
    assert _contrast(pair[0], _MEASURED_ACTIVE_ROW) >= 3.5


@pytest.mark.parametrize("pair", [COLOR_GOOD, COLOR_WARN, COLOR_BAD])
def test_status_colors_are_readable_in_light_mode(pair) -> None:
    assert _contrast(pair[1], (236.0, 236.0, 238.0)) >= 4.5
