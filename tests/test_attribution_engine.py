"""The golden replay suite.

Every case here is a click/install sequence with an exact expected outcome. This
is the regression net for attribution: any future change to the engine has to
declare itself by breaking one of these rather than by quietly moving a
customer's numbers.

The engine is pure — ``now`` and every input are parameters — so these run in
milliseconds with no database, and can afford to be exhaustive about the edges
that matter: window boundaries, precedence conflicts, ordering, and the failure
modes that look like success.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from mmp_attrib.engine import CLICK_INJECTION_THRESHOLD
from mmp_core.ids import uuid7

from mmp_attrib import Click, Install, Method, attribute, parse_referrer, should_supersede

APP = uuid7()
INSTALL_AT = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)
DEVICE = b"\x01" * 16
OTHER_DEVICE = b"\x02" * 16


def click(*, minutes_ago: float = 60, **overrides) -> Click:
    defaults = {
        "click_id": uuid7(),
        "clicked_at": INSTALL_AT - dt.timedelta(minutes=minutes_ago),
        "campaign_id": uuid7(),
        "tracking_link_id": uuid7(),
        "source": "meta",
        "medium": "cpi",
        "device_hash": None,
        "is_bot": False,
    }
    return Click(**{**defaults, **overrides})


def install(**overrides) -> Install:
    defaults = {
        "app_id": APP,
        "anonymous_id": "device-1",
        "installed_at": INSTALL_AT,
        "referrer": None,
        "click_id": None,
        "device_hash": None,
    }
    return Install(**{**defaults, **overrides})


# --- precedence ---------------------------------------------------------
def test_referrer_beats_everything():
    """Play's referrer is the only signal that comes from outside the device."""
    referrer_click = click(minutes_ago=120, device_hash=OTHER_DEVICE)
    device_click = click(minutes_ago=5, device_hash=DEVICE)
    sdk_click = click(minutes_ago=10)

    decision = attribute(
        install(device_hash=DEVICE, click_id=sdk_click.click_id),
        [referrer_click, device_click, sdk_click],
        window_days=7,
        referrer_click_id=referrer_click.click_id,
    )
    assert decision.method is Method.REFERRER
    assert decision.click_id == referrer_click.click_id


def test_sdk_click_id_beats_device_match():
    sdk_click = click(minutes_ago=120)
    device_click = click(minutes_ago=5, device_hash=DEVICE)

    decision = attribute(
        install(device_hash=DEVICE, click_id=sdk_click.click_id),
        [sdk_click, device_click],
        window_days=7,
    )
    assert decision.method is Method.CLICK_ID
    assert decision.click_id == sdk_click.click_id


def test_device_match_used_when_no_click_id():
    matching = click(minutes_ago=30, device_hash=DEVICE)
    decision = attribute(
        install(device_hash=DEVICE), [matching, click(minutes_ago=5)], window_days=7
    )
    assert decision.method is Method.DEVICE_MATCH
    assert decision.click_id == matching.click_id


def test_organic_when_nothing_matches():
    decision = attribute(install(), [click(minutes_ago=30)], window_days=7)
    assert decision.method is Method.ORGANIC
    assert decision.click is None
    assert not decision.is_attributed


def test_organic_with_no_clicks_at_all():
    decision = attribute(install(), [], window_days=7)
    assert decision.method is Method.ORGANIC
    assert "no clicks recorded" in decision.reason


def test_probabilistic_is_never_produced():
    """The engine has no fallback tier.

    A fingerprint match would raise the attribution rate and is exactly what
    Apple's rules prohibit for cross-app attribution — and it produces numbers
    that cannot be defended when an advertiser asks how they were derived.
    """
    for device in (None, DEVICE, OTHER_DEVICE):
        decision = attribute(install(device_hash=device), [click(minutes_ago=30)], window_days=7)
        assert decision.method is not Method.PROBABILISTIC


# --- last click ---------------------------------------------------------
def test_most_recent_qualifying_click_wins():
    old = click(minutes_ago=300, device_hash=DEVICE)
    recent = click(minutes_ago=15, device_hash=DEVICE)
    middle = click(minutes_ago=100, device_hash=DEVICE)

    decision = attribute(install(device_hash=DEVICE), [old, recent, middle], window_days=7)
    assert decision.click_id == recent.click_id


def test_input_order_does_not_change_the_answer():
    """The result must depend on the data, not on how the database returned it."""
    clicks = [
        click(minutes_ago=300, device_hash=DEVICE),
        click(minutes_ago=15, device_hash=DEVICE),
        click(minutes_ago=100, device_hash=DEVICE),
    ]
    expected = attribute(install(device_hash=DEVICE), clicks, window_days=7).click_id
    for permutation in ([clicks[2], clicks[0], clicks[1]], list(reversed(clicks))):
        assert (
            attribute(install(device_hash=DEVICE), permutation, window_days=7).click_id == expected
        )


def test_identical_timestamps_still_resolve_deterministically():
    """Two clicks at the same instant must not make the answer a coin flip."""
    moment = INSTALL_AT - dt.timedelta(minutes=10)
    first = click(clicked_at=moment, device_hash=DEVICE)
    second = click(clicked_at=moment, device_hash=DEVICE)

    a = attribute(install(device_hash=DEVICE), [first, second], window_days=7).click_id
    b = attribute(install(device_hash=DEVICE), [second, first], window_days=7).click_id
    assert a == b


# --- windows ------------------------------------------------------------
@pytest.mark.parametrize(
    ("hours_ago", "window_days", "attributed"),
    [
        (1, 7, True),
        (24 * 6, 7, True),
        (24 * 7 - 1, 7, True),  # just inside
        (24 * 7 + 1, 7, False),  # just outside
        (24 * 8, 7, False),
        (24 * 8, 14, True),  # a wider window catches it
    ],
)
def test_window_boundaries(hours_ago, window_days, attributed):
    candidate = click(minutes_ago=hours_ago * 60, device_hash=DEVICE)
    decision = attribute(install(device_hash=DEVICE), [candidate], window_days=window_days)
    assert decision.is_attributed is attributed


def test_exactly_at_the_boundary_is_inside():
    """An inclusive boundary, stated once, so both ends of the platform agree."""
    candidate = click(minutes_ago=7 * 24 * 60, device_hash=DEVICE)
    decision = attribute(install(device_hash=DEVICE), [candidate], window_days=7)
    assert decision.is_attributed


def test_a_click_after_the_install_never_counts():
    """Either a clock problem or an injection attempt. It did not cause the
    install either way."""
    future = click(minutes_ago=-30, device_hash=DEVICE)
    decision = attribute(install(device_hash=DEVICE), [future], window_days=7)
    assert decision.method is Method.ORGANIC


def test_referrer_click_outside_the_window_does_not_attribute():
    """Even ground truth expires. Otherwise the window means nothing."""
    stale = click(minutes_ago=30 * 24 * 60)
    decision = attribute(install(), [stale], window_days=7, referrer_click_id=stale.click_id)
    assert decision.method is Method.ORGANIC


def test_window_is_recorded_on_the_decision():
    """Copied onto the row, so a historical attribution still explains itself
    after someone changes the app's configuration."""
    decision = attribute(install(device_hash=DEVICE), [click(device_hash=DEVICE)], window_days=14)
    assert decision.window_days == 14


# --- fraud signals ------------------------------------------------------
def test_click_injection_is_flagged_but_still_attributed():
    """The advertiser must not be penalised for their attacker.

    An implausibly short click-to-install gap is the signature of malware firing
    a click as an install begins. It is recorded for the fraud rules to score,
    and the install is still attributed — refusing would mean a compromised
    device silently costs the advertiser their measurement.
    """
    injected = click(minutes_ago=0.05, device_hash=DEVICE)  # 3 seconds
    decision = attribute(install(device_hash=DEVICE), [injected], window_days=7)
    assert decision.is_attributed
    assert decision.suspected_injection
    assert decision.click_to_install < CLICK_INJECTION_THRESHOLD


def test_a_normal_gap_is_not_flagged():
    decision = attribute(
        install(device_hash=DEVICE), [click(minutes_ago=5, device_hash=DEVICE)], window_days=7
    )
    assert not decision.suspected_injection


def test_bot_clicks_are_excluded_by_default():
    """A click we already believe was automated must not win an attribution."""
    bot = click(minutes_ago=5, device_hash=DEVICE, is_bot=True)
    human = click(minutes_ago=200, device_hash=DEVICE)

    decision = attribute(install(device_hash=DEVICE), [bot, human], window_days=7)
    assert decision.click_id == human.click_id

    included = attribute(
        install(device_hash=DEVICE), [bot, human], window_days=7, include_bots=True
    )
    assert included.click_id == bot.click_id


# --- device matching ----------------------------------------------------
def test_a_different_device_does_not_match():
    decision = attribute(
        install(device_hash=DEVICE), [click(device_hash=OTHER_DEVICE)], window_days=7
    )
    assert decision.method is Method.ORGANIC


def test_absent_device_hash_never_matches_absent():
    """Two devices that both opted out are not the same device.

    hash_device_id returns None for an opted-out advertising ID, and if None
    matched None every opted-out install would be attributed to whichever
    opted-out click came last — the single worst failure this engine could have.
    """
    no_device_click = click(minutes_ago=5, device_hash=None)
    decision = attribute(install(device_hash=None), [no_device_click], window_days=7)
    assert decision.method is Method.ORGANIC


# --- supersession -------------------------------------------------------
def test_referrer_supersedes_a_device_match():
    """Play's API is queried on first launch and may need a retry if the device
    was offline, so better evidence can arrive after the fact."""
    assert should_supersede(Method.DEVICE_MATCH, Method.REFERRER)


def test_lower_fidelity_never_supersedes():
    """Otherwise the answer would depend on delivery order."""
    assert not should_supersede(Method.REFERRER, Method.DEVICE_MATCH)
    assert not should_supersede(Method.REFERRER, Method.ORGANIC)
    assert not should_supersede(Method.CLICK_ID, Method.CLICK_ID)


def test_organic_can_be_upgraded():
    """An install recorded organic before the referrer arrived is exactly the
    case supersession exists for."""
    assert should_supersede(Method.ORGANIC, Method.REFERRER)
    assert should_supersede(Method.ORGANIC, Method.DEVICE_MATCH)


# --- end-to-end through the referrer parser -----------------------------
def test_referrer_string_to_attribution():
    candidate = click(minutes_ago=30)
    referrer = f"utm_source=google-play&utm_content={candidate.click_id}"
    parsed = parse_referrer(referrer)

    decision = attribute(
        install(referrer=referrer),
        [candidate],
        window_days=7,
        referrer_click_id=parsed.click_id,
    )
    assert decision.method is Method.REFERRER
    assert decision.click_id == candidate.click_id


def test_organic_referrer_yields_organic_install():
    """The most common real referrer: a genuine organic Play Store visit."""
    parsed = parse_referrer("utm_source=google-play&utm_medium=organic")
    decision = attribute(
        install(), [click(minutes_ago=30)], window_days=7, referrer_click_id=parsed.click_id
    )
    assert decision.method is Method.ORGANIC


def test_unknown_click_id_in_referrer_falls_through():
    """A click id from another MMP, or one whose click we never recorded, must
    not attribute — and must not raise."""
    parsed = parse_referrer(f"utm_content={uuid.uuid4()}")
    decision = attribute(
        install(device_hash=DEVICE),
        [click(minutes_ago=10, device_hash=DEVICE)],
        window_days=7,
        referrer_click_id=parsed.click_id,
    )
    assert decision.method is Method.DEVICE_MATCH


def test_every_decision_explains_itself():
    """A support engineer must be able to tell a customer why."""
    cases = [
        attribute(install(), [], window_days=7),
        attribute(install(device_hash=DEVICE), [click(device_hash=DEVICE)], window_days=7),
    ]
    for decision in cases:
        assert decision.reason
        assert len(decision.reason) > 20
