"""Server-side sparkline: closes become one SVG polyline. No library, no script."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from markupsafe import Markup

WIDTH: Final = 120
HEIGHT: Final = 32
PAD: Final = 2


def points(closes: Sequence[Decimal], width: int = WIDTH, height: int = HEIGHT) -> str:
    """The polyline `points` attribute for a series of closes.

    x spreads the values evenly across the width; y maps the minimum to the
    bottom and the maximum to the top (SVG's y grows downward). A flat series is
    drawn along the middle rather than dividing by zero.
    """
    n = len(closes)
    if n < 2:
        return ""
    low, high = min(closes), max(closes)
    span = high - low
    inner_w = Decimal(width - 2 * PAD)
    inner_h = Decimal(height - 2 * PAD)
    out: list[str] = []
    for i, close in enumerate(closes):
        x = Decimal(PAD) + inner_w * i / (n - 1)
        if span == 0:
            y = Decimal(height) / 2
        else:
            y = Decimal(PAD) + inner_h * (high - close) / span
        out.append(f"{x:.1f},{y:.1f}")
    return " ".join(out)


def sparkline(closes: Sequence[Decimal], css_class: str = "spark") -> Markup:
    """An <svg> for the closes, or an empty marker when there are too few.

    The only markup built by hand in the web layer. It is safe because every
    value interpolated is a number this module formatted itself; the class name
    is a constant chosen by the caller, never ingested text.
    """
    path = points(closes)
    if not path:
        return Markup('<span class="spark-empty">sem série</span>')
    return Markup(
        f'<svg class="{css_class}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" aria-label="30 dias">'
        f'<polyline points="{path}" fill="none"/></svg>'
    )
