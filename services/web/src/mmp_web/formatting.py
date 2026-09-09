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
    """A daily volume chart, laid out by CSS rather than drawn as SVG.

    The previous version was an inline SVG stretched to its container with
    ``preserveAspectRatio="none"``. That distorts everything: with a single
    day's data the ten-unit viewBox was blown up to the full width of the page
    and one bar became a slab; with thirty days the bars became slivers, and the
    rounded corners and the baseline stroke were stretched with them. It also
    forced the axis labels out of the drawing, because glyphs scaled too.

    Bars in normal flow have none of those problems. A bar is a div whose height
    is a percentage, so it is exact at any container width, the width is capped
    so one day looks like one bar rather than a wall, and the labels are text
    that was never scaled in the first place.

    Still no JavaScript and no charting library: the page loads no third-party
    code, which is what lets its content-security policy forbid it outright.

    Every value is escaped. This builds markup by concatenation, which is
    exactly where an unescaped campaign name becomes stored XSS.
    """
    if not series:
        return ""

    def _value(point: dict[str, object]) -> int:
        raw = point.get(value_key, 0)
        return int(raw) if isinstance(raw, int | float | str) and raw != "" else 0

    values = [_value(point) for point in series]
    peak = max(values) if values else 0
    # A flat all-zero series renders as a flat line rather than dividing by zero.
    scale = peak or 1

    bars: list[str] = []
    for point, value in zip(series, values, strict=True):
        label = html.escape(str(point.get(label_key, "")))
        # A day with no volume still gets a visible sliver, so the gap in the
        # series reads as "nothing happened" rather than as missing data.
        height = (value / scale) * 100 if value else 0
        bars.append(
            f'<div class="bar" title="{label}: {value:,}">'
            f'<div class="fill{" empty" if not value else ""}" '
            f'style="height:{height:.2f}%"></div>'
            f"</div>"
        )

    first = html.escape(str(series[0].get(label_key, "")))
    last = html.escape(str(series[-1].get(label_key, "")))
    midpoint = f"{peak // 2:,}" if peak else "0"

    return (
        f'<div class="chart" role="img" aria-label="Daily volume, peak {peak:,}">'
        # The scale sits beside the plot rather than inside it, so the numbers
        # are real text at a real size.
        f'<div class="chart-scale">'
        f"<span>{peak:,}</span><span>{midpoint}</span><span>0</span>"
        f"</div>"
        f'<div class="chart-plot">'
        # Two gridlines, at the peak and the midpoint. Any more is decoration
        # competing with the data.
        f'<div class="gridline" style="top:0"></div>'
        f'<div class="gridline" style="top:50%"></div>'
        f'<div class="bars">{"".join(bars)}</div>'
        f"</div>"
        f"</div>"
        f'<div class="chart-axis"><span>{first}</span><span>{last}</span></div>'
    )
