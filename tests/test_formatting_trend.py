"""The statistic-tile helpers: period deltas and sparklines."""

from __future__ import annotations

from mmp_web.formatting import delta, sparkline


def test_delta_is_undefined_without_a_previous_period():
    """ "+100% against zero" looks like growth and means "we started measuring"."""
    assert delta(10, None) is None
    assert delta(10, 0) is None
    assert delta(None, 10) is None


def test_delta_direction_and_display():
    assert delta(150, 100) == {"direction": "up", "display": "+50.0%"}
    assert delta(75, 100) == {"direction": "down", "display": "-25.0%"}
    assert delta(100, 100) == {"direction": "flat", "display": "0.0%"}


def test_sparkline_is_an_svg_with_no_text_in_it():
    markup = sparkline([1, 4, 2, 8])
    assert markup.startswith("<svg")
    assert "<text" not in markup, "nothing inside it may be stretched"
    assert 'preserveAspectRatio="none"' in markup
    assert 'class="line"' in markup and 'class="area"' in markup


def test_sparkline_of_one_value_is_a_flat_line_not_nothing():
    assert sparkline([5]).count(",") >= 2
    assert sparkline([]) == ""


def test_sparkline_peak_touches_the_top_and_zero_the_bottom():
    markup = sparkline([0, 10])
    # The polyline runs from the bottom-left to the top-right.
    assert 'points="0.0,30.0 100.0,2.0"' in markup


def test_volume_chart_shows_one_metric_and_tells_all_three_on_hover():
    from mmp_web.formatting import volume_chart

    series = [
        {"day": "2026-09-01", "installs": 2, "events": 40, "revenue_minor": 1250},
        {"day": "2026-09-02", "installs": 0, "events": 0, "revenue_minor": 0},
    ]
    markup = volume_chart(series, metric="installs")
    assert markup.count('class="bar"') == 2
    assert "height:100.00%" in markup and 'class="fill empty"' in markup
    assert ">2<" in markup, "the scale is in the chosen metric"
    assert "$12.50" in markup, "revenue is in the tooltip, as money"
    assert "<svg" not in markup

    revenue = volume_chart(series, metric="revenue_minor")
    assert ">$12.50<" in revenue, "a revenue scale is money, not minor units"
    assert volume_chart(series, metric="nope").count("Events") >= 1, "unknown metric falls back"


def test_volume_chart_escapes_labels():
    from mmp_web.formatting import volume_chart

    markup = volume_chart([{"day": "<script>x</script>", "events": 1}])
    assert "<script>" not in markup and "&lt;script&gt;" in markup
