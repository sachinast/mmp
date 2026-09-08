"""Presentation helpers.

Two rules run through all of this:

* **Money is integer minor units.** It arrives that way and is only turned into
  a decimal string at the last possible moment, in one place. A float that
  reaches a template is a float that eventually reaches a total.
* **Undefined is not zero.** A rate with no denominator, a metric with no data —
  these render as an em dash, never as ``0``. "0% install rate" reads as "nobody
  converted"; the truth is usually "nobody clicked", and those call for very
  different reactions from whoever is looking.
"""

from __future__ import annotations

import datetime as dt
import html
from decimal import Decimal

# Where the symbol is unambiguous and conventional. Anything else renders as a
# code, because "$" on a EUR figure is worse than a currency code nobody minds.
CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥", "INR": "₹"}
EM_DASH = "—"


def money(minor_units: int | None, currency: str = "USD") -> str:
    if minor_units is None:
        return EM_DASH
    # Decimal, not float. Two divisions by 100 in float are enough to make a
    # total that does not match the sum of its rows.
    amount = Decimal(minor_units) / Decimal(100)
    symbol = CURRENCY_SYMBOLS.get(currency, "")
    formatted = f"{amount:,.2f}"
    return f"{symbol}{formatted}" if symbol else f"{formatted} {currency}"


def percentage(rate: float | None, *, places: int = 2) -> str:
    if rate is None:
        return EM_DASH
    return f"{rate * 100:.{places}f}%"


def count(value: int | None) -> str:
    return EM_DASH if value is None else f"{value:,}"


def default_range(days: int = 7) -> tuple[dt.date, dt.date]:
    today = dt.datetime.now(dt.UTC).date()
    return today - dt.timedelta(days=days), today


def parse_date(value: str | None, fallback: dt.date) -> dt.date:
    if not value:
        return fallback
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        # A malformed date in a URL is a typo or a stale bookmark, not an attack
        # and not worth an error page.
        return fallback


def bar_chart(
    series: list[dict[str, object]],
    *,
    value_key: str = "events",
    label_key: str = "day",
) -> str:
    """A daily bar chart as an inline SVG, with its labels in HTML.

    Server-rendered rather than drawn by a charting library, for the same reason
    everything else here is inline: the page loads no third-party code, so the
    Content-Security-Policy can forbid it entirely. It also means the chart is
    in the HTML — reachable by a screen reader, and present in a saved page.

    **No text inside the SVG.** The chart stretches to the width of its
    container via ``preserveAspectRatio="none"``, which scales glyphs along with
    the geometry — the first version put the axis label inside and rendered it as
    unreadable smears. Geometry scales; type does not. So the axis labels are
    ordinary HTML beside the drawing, where they stay the size they were meant to
    be.

    Every value is escaped on the way in. This builds markup by concatenation,
    which is exactly where an unescaped campaign name becomes stored XSS.
    """
    if not series:
        return ""

    def _value(point: dict[str, object]) -> int:
        raw = point.get(value_key, 0)
        return int(raw) if isinstance(raw, int | float | str) and raw != "" else 0

    values = [_value(point) for point in series]
    peak = max(values) if values else 0
    # A flat all-zero series must render as a flat line, not divide by zero.
    scale = peak or 1

    # User units, not percentages: mixing the two inside a viewBox is how the
    # first version ended up with an axis line that did not sit where it looked
    # like it should.
    column = 10.0
    width = column * len(series)
    height = 100.0
    baseline = 96.0

    bars: list[str] = []
    for index, (point, value) in enumerate(zip(series, values, strict=True)):
        bar_height = (value / scale) * (baseline - 4)
        label = html.escape(str(point.get(label_key, "")))
        bars.append(
            f"<g><title>{label}: {value:,}</title>"
            f'<rect x="{index * column + column * 0.15:.2f}" '
            f'y="{baseline - bar_height:.2f}" '
            f'width="{column * 0.7:.2f}" height="{max(bar_height, 0.5):.2f}" '
            f'fill="#4f9cf9" rx="0.6"/></g>'
        )

    first = html.escape(str(series[0].get(label_key, "")))
    last = html.escape(str(series[-1].get(label_key, "")))

    return (
        f'<div class="chart-peak">{peak:,}</div>'
        f'<svg class="chart" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'preserveAspectRatio="none" role="img" '
        f'aria-label="Daily volume, peak {peak:,}">'
        f'<line x1="0" y1="{baseline}" x2="{width:.0f}" y2="{baseline}" '
        f'stroke="#2a323b" stroke-width="0.5"/>'
        f"{''.join(bars)}</svg>"
        f'<div class="chart-axis"><span>{first}</span><span>{last}</span></div>'
    )
