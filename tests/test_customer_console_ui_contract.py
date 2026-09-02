from __future__ import annotations

from wechatrebot_manager.ui_theme import COLORS, GEOMETRY


def test_reference_layout_geometry_is_locked() -> None:
    assert (GEOMETRY.window_width, GEOMETRY.window_height) == (1254, 802)
    assert GEOMETRY.titlebar_height == 56
    assert GEOMETRY.sidebar_width == 212
    assert GEOMETRY.home_upper_height == 405
    assert GEOMETRY.launcher_diameter == 332
    assert (GEOMETRY.start_button_width, GEOMETRY.start_button_height) == (172, 50)


def test_reference_layout_maps_to_125_percent_capture() -> None:
    scale = 1.25
    physical = lambda value: int(value * scale + 0.5)
    assert physical(GEOMETRY.window_width) == 1568
    assert physical(GEOMETRY.window_height) == 1003
    assert physical(GEOMETRY.titlebar_height) == 70
    assert physical(GEOMETRY.sidebar_width) == 265


def test_customer_console_uses_reference_visual_hierarchy() -> None:
    assert COLORS.surface == "#FFFFFF"
    assert COLORS.canvas != COLORS.surface
    assert COLORS.primary != COLORS.ink
    assert COLORS.success != COLORS.warning
