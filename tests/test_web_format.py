"""Tests for the pure display helpers: sparkline, formatting, source health, clock.

Sparkline expected values are worked out by hand.

For closes [10, 20, 15] in a 120 x 32 box with a 2 px pad (inner 116 x 28):
    x spreads over 116 in n-1 = 2 steps -> 2, 60, 118
    y = 2 + 28 * (max - close) / (max - min), max=20, min=10
        close 10 -> 2 + 28 * 10 / 10 = 30
        close 20 -> 2 + 28 *  0 / 10 =  2
        close 15 -> 2 + 28 *  5 / 10 = 16
"""

from decimal import Decimal

from guardrail.web.sparkline import HEIGHT, WIDTH, points, sparkline

D = Decimal


# --- sparkline -----------------------------------------------------------------


def test_sparkline_points_match_the_hand_calculation() -> None:
    assert points([D(10), D(20), D(15)]) == "2.0,30.0 60.0,2.0 118.0,16.0"


def test_a_flat_series_runs_along_the_middle() -> None:
    assert points([D(5), D(5), D(5)]) == "2.0,16.0 60.0,16.0 118.0,16.0"


def test_higher_close_is_higher_on_the_screen() -> None:
    low_y, high_y = (float(p.split(",")[1]) for p in points([D(1), D(2)]).split())

    assert high_y < low_y  # SVG y grows downward


def test_fewer_than_two_closes_draw_nothing() -> None:
    assert points([]) == ""
    assert points([D(1)]) == ""
    assert "sem série" in str(sparkline([D(1)]))


def test_thirty_closes_span_the_whole_width() -> None:
    pts = points([D(i) for i in range(30)]).split()

    assert len(pts) == 30
    assert pts[0].startswith("2.0,") and pts[-1].startswith("118.0,")


def test_sparkline_is_a_single_polyline_without_script() -> None:
    svg = str(sparkline([D(10), D(20), D(15)]))

    assert svg.count("<polyline") == 1
    assert 'points="2.0,30.0 60.0,2.0 118.0,16.0"' in svg
    assert f'viewBox="0 0 {WIDTH} {HEIGHT}"' in svg
    assert "<script" not in svg
    assert "onload" not in svg and "javascript" not in svg
