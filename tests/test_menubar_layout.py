"""Pure geometry contracts for the AppKit menu-bar popover."""

from __future__ import annotations

import pytest

from claude_swap.menubar_layout import (
    BUTTON_HEIGHT,
    MAX_USAGE_CELLS,
    MainPopoverLayout,
    POPOVER_MAX_HEIGHT,
    POPOVER_WIDTH,
    ROW_HEIGHT,
    Rect,
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
    if layout.footer is not None:
        assert layout.footer.y == layout.accounts_viewport.max_y
        assert layout.root.height == layout.footer.max_y
    else:
        assert layout.root.height == layout.accounts_viewport.max_y


def _assert_contained(child: Rect, width: float, height: float) -> None:
    assert 0 <= child.x
    assert 0 <= child.y
    assert child.max_x <= width
    assert child.max_y <= height


def test_popover_is_well_under_half_the_original_430_by_620_area() -> None:
    assert POPOVER_WIDTH * POPOVER_MAX_HEIGHT <= 0.5 * 430.0 * 620.0


def test_six_accounts_fit_without_scrolling() -> None:
    layout = plan_main_popover((2,) * 6)

    _assert_main_chrome(layout)
    assert layout.accounts_document_height == layout.accounts_viewport.height
    assert layout.root.height <= POPOVER_MAX_HEIGHT


def test_main_layout_handles_no_accounts_without_a_tall_artificial_document() -> None:
    layout = plan_main_popover(())

    _assert_main_chrome(layout)
    assert layout.accounts == ()
    assert layout.root.height < POPOVER_MAX_HEIGHT
    assert layout.accounts_document_height == layout.accounts_viewport.height == 48.0


@pytest.mark.parametrize("cells", range(MAX_USAGE_CELLS + 1))
def test_row_children_stay_inside_the_row(cells: int) -> None:
    row = plan_main_popover((cells,)).accounts[0]

    assert row.frame.height == ROW_HEIGHT
    assert len(row.usage_cells) == cells
    assert row.frame.width + 2 * row.frame.x == POPOVER_WIDTH
    for child in (row.avatar, row.name, row.status, row.detail, row.session_button, row.login_button):
        _assert_contained(child, row.frame.width, row.frame.height)
    meters = tuple(frame for cell in row.usage_cells for frame in (cell.text, cell.bar))
    for frame in meters:
        _assert_contained(frame, row.frame.width, row.frame.height)
    _assert_disjoint((row.avatar, row.name, row.status))
    _assert_disjoint((*meters, row.avatar, row.session_button, row.name))


def test_rows_do_not_overlap_and_only_the_list_grows_for_long_lists() -> None:
    layout = plan_main_popover((3, 0, 2, 1) * 5)

    _assert_main_chrome(layout)
    assert layout.root.height == POPOVER_MAX_HEIGHT
    assert layout.accounts_document_height > layout.accounts_viewport.height
    _assert_disjoint(tuple(row.frame for row in layout.accounts))
    assert all(row.frame.max_y <= layout.accounts_document_height for row in layout.accounts)


def test_footer_has_one_row_of_three_and_can_be_left_out() -> None:
    with_footer = plan_main_popover((1,))
    frames = (with_footer.switch_best, with_footer.next_available, with_footer.rotate_next)

    assert {frame.y for frame in frames} == {with_footer.footer.y + 8.0}
    _assert_disjoint(frames)
    assert all(frame.max_y <= with_footer.footer.max_y for frame in frames)

    without = plan_main_popover((1,), has_footer=False)
    _assert_main_chrome(without)
    assert without.footer is None
    assert without.switch_best is None
    assert without.root.height < with_footer.root.height


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
        (plan_main_popover, (MAX_USAGE_CELLS + 1,)),
        (plan_scroll_screen, (0.0,)),
    ],
)
def test_planners_reject_invalid_item_sizes(planner, values) -> None:
    with pytest.raises(ValueError):
        planner(values)
