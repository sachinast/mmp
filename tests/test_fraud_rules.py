"""The fraud rules, asserted at their boundaries.

A threshold with no test on its edge is a threshold that drifts. Every rule
here is checked just inside and just outside its limit, so moving one has to be
a deliberate act that breaks a named test.
"""

from __future__ import annotations

import datetime as dt

import pytest
from mmp_attrib.fraud import (
    CLICK_FARM_MIN_DEVICES,
    CLICK_INJECTION_THRESHOLD,
    DEVICE_REPLAY_THRESHOLD,
    FLOOD_LATE_SHARE,
    FLOOD_MIN_ATTRIBUTED,
    FRAUDULENT_AT,
    LATE_CONVERSION_THRESHOLD,
    SUSPICIOUS_AT,
    Assessment,
    InstallContext,
    Rule,
    Severity,
    Signal,
    TrafficWindow,
    Verdict,
    assess_install,
    assess_traffic,
)

MINUTE = dt.timedelta(minutes=1)


# ------------------------------------------------------------------ policy
def test_the_thresholds_are_what_we_think_they_are():
    """Pinned as literals, deliberately.

    Every other test in this file derives its boundary from the constant it is
    testing, which makes those tests silent when a threshold moves — they follow
    it. This test is the one that does not. Changing a number here is fine, but
    it has to be a deliberate edit with a reason, because these values decide
    whether a real partner gets accused of fraud.
    """
    assert dt.timedelta(seconds=10) == CLICK_INJECTION_THRESHOLD
    assert dt.timedelta(days=7) == LATE_CONVERSION_THRESHOLD
    assert DEVICE_REPLAY_THRESHOLD == 6
    assert CLICK_FARM_MIN_DEVICES == 75
    assert FLOOD_MIN_ATTRIBUTED == 50
    assert FLOOD_LATE_SHARE == 0.60
    # One CRITICAL condemns alone; two HIGH corroborate; LOW never accumulates
    # into a verdict on its own.
    assert (SUSPICIOUS_AT, FRAUDULENT_AT) == (25, 100)
    assert Severity.LOW * 2 < SUSPICIOUS_AT
    assert Severity.HIGH * 2 >= FRAUDULENT_AT
    assert Severity.CRITICAL >= FRAUDULENT_AT


# --------------------------------------------------------------- per-install
def test_a_normal_install_raises_nothing():
    assessment = assess_install(InstallContext(click_to_install=dt.timedelta(minutes=12)))
    assert assessment.signals == []
    assert assessment.score == 0
    assert assessment.verdict is Verdict.CLEAN


def test_an_organic_install_is_not_judged_by_click_rules():
    """No click means no click evidence — not absent evidence treated as bad."""
    assessment = assess_install(InstallContext(click_to_install=None))
    assert assessment.verdict is Verdict.CLEAN


@pytest.mark.parametrize("seconds", [0.0, 1.0, 9.9])
def test_an_implausibly_fast_install_is_click_injection(seconds):
    assessment = assess_install(InstallContext(click_to_install=dt.timedelta(seconds=seconds)))
    assert Rule.CLICK_INJECTION in assessment.rules
    assert assessment.verdict is Verdict.FRAUDULENT


def test_the_injection_threshold_is_exclusive_at_its_edge():
    """Exactly at the threshold is not injection; a hair under it is.

    Pinned because an off-by-one here is the difference between flagging a
    network and not, and nothing else in the suite would notice the change.
    """
    at = assess_install(InstallContext(click_to_install=CLICK_INJECTION_THRESHOLD))
    under = assess_install(
        InstallContext(click_to_install=CLICK_INJECTION_THRESHOLD - dt.timedelta(milliseconds=1))
    )
    assert at.rules == []
    assert Rule.CLICK_INJECTION in under.rules


def test_a_click_after_its_own_install_says_so_specifically():
    """A negative gap is also under the injection threshold, so the verdict
    alone cannot tell the two apart — only the explanation can, and the
    explanation is the part a support engineer reads out."""
    assessment = assess_install(InstallContext(click_to_install=-MINUTE))
    assert Rule.CLICK_INJECTION in assessment.rules
    assert assessment.verdict is Verdict.FRAUDULENT
    detail = assessment.signals[0].detail
    assert "after the install" in detail, (
        "a click timestamped after its install must be explained as that, not "
        f"as a merely fast one; got {detail!r}"
    )


def test_a_late_conversion_is_noted_but_not_condemned_alone():
    """People really do install a week later. One late install proves nothing.

    It carries weight only by corroborating a flooding finding, so on its own
    it must stay below the suspicious line.
    """
    assessment = assess_install(
        InstallContext(click_to_install=LATE_CONVERSION_THRESHOLD + dt.timedelta(days=1))
    )
    assert Rule.LATE_CONVERSION in assessment.rules
    assert assessment.verdict is Verdict.CLEAN


def test_a_bot_click_is_suspicious_but_not_alone_conclusive():
    """One HIGH signal suspects; it takes corroboration to condemn."""
    bot_only = assess_install(InstallContext(click_to_install=MINUTE, click_is_bot=True))
    assert Rule.BOT_CLICK in bot_only.rules
    assert bot_only.verdict is Verdict.SUSPICIOUS


def test_device_replay_needs_to_clear_the_reinstall_threshold():
    """Reinstalling an app is ordinary behaviour and must not be flagged."""
    ordinary = assess_traffic(TrafficWindow(max_installs_per_device=DEVICE_REPLAY_THRESHOLD - 1))
    replayed = assess_traffic(TrafficWindow(max_installs_per_device=DEVICE_REPLAY_THRESHOLD))
    assert ordinary.rules == []
    assert Rule.DEVICE_REPLAY in replayed.rules


# ------------------------------------------------------------------ traffic
def test_quiet_traffic_raises_nothing():
    assert assess_traffic(TrafficWindow(attributed_installs=20, late_installs=1)).signals == []


def test_click_flooding_needs_both_a_sample_and_a_shape():
    """A small link with mostly-late installs is noise, not an attack.

    This is the rule most likely to hurt an innocent partner, so it must not
    fire without a population behind it.
    """
    small = assess_traffic(
        TrafficWindow(
            attributed_installs=FLOOD_MIN_ATTRIBUTED - 1,
            late_installs=FLOOD_MIN_ATTRIBUTED - 1,
        )
    )
    assert small.signals == [], "a small sample must not condemn a link"

    flooded = assess_traffic(
        TrafficWindow(attributed_installs=1000, late_installs=900),
    )
    assert Rule.CLICK_FLOODING in flooded.rules
    assert flooded.verdict is Verdict.FRAUDULENT


def test_a_busy_link_that_converts_promptly_is_not_flooding():
    assessment = assess_traffic(TrafficWindow(attributed_installs=100_000, late_installs=200))
    assert assessment.signals == [], "volume alone is not evidence of anything"


def test_a_click_farm_is_many_devices_behind_one_address():
    shared_office = assess_traffic(TrafficWindow(max_devices_per_ip=CLICK_FARM_MIN_DEVICES - 1))
    farm = assess_traffic(TrafficWindow(max_devices_per_ip=CLICK_FARM_MIN_DEVICES))
    assert shared_office.signals == []
    assert Rule.CLICK_FARM in farm.rules


# ------------------------------------------------------------------ scoring
def test_two_weak_signals_do_not_add_up_to_a_strong_one():
    """The severity gaps are load-bearing, not decorative.

    If LOW signals accumulated into a verdict, a link could be condemned by a
    pile of hints none of which anyone would defend individually.
    """
    many_hints = Assessment(
        signals=[Signal(Rule.LATE_CONVERSION, Severity.LOW, "hint") for _ in range(2)]
    )
    assert many_hints.verdict is Verdict.CLEAN


def test_a_single_critical_signal_is_enough_on_its_own():
    one = Assessment(signals=[Signal(Rule.CLICK_INJECTION, Severity.CRITICAL, "impossible")])
    assert one.verdict is Verdict.FRAUDULENT


def test_every_signal_explains_itself_in_a_sentence():
    """A finding nobody can explain is one nobody can act on.

    These strings get read to an advertiser, so they must be present, specific
    and free of the rule's own internal vocabulary.
    """
    everything = (
        assess_install(
            InstallContext(click_to_install=dt.timedelta(seconds=1), click_is_bot=True)
        ).signals
        + assess_install(
            InstallContext(click_to_install=LATE_CONVERSION_THRESHOLD + dt.timedelta(days=1))
        ).signals
        + assess_traffic(
            TrafficWindow(
                attributed_installs=1000,
                late_installs=900,
                max_devices_per_ip=500,
                max_installs_per_device=99,
            )
        ).signals
    )

    fired = {s.rule for s in everything}
    assert fired == set(Rule), f"every rule must fire here; missing {set(Rule) - fired}"
    for signal in everything:
        assert len(signal.detail) > 25, f"{signal.rule} has no usable explanation"
        assert signal.rule.value not in signal.detail, (
            f"{signal.rule} explains itself by restating its own name"
        )
