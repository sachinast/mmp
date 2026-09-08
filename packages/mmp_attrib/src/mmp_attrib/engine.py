"""Deterministic last-click attribution.

Pure functions. No database, no clock, no network — every input is a parameter,
including ``now``. That is what makes the golden replay suite possible: a fixed
set of click/install sequences can be replayed against this module and asserted
exactly, so any future change to attribution has to declare itself by breaking a
test rather than by quietly moving a customer's numbers.

**The model.** Last-click, deterministic only, in a strict precedence order.
There is no probabilistic tier: if none of the deterministic signals match, the
install is organic. That is a commercial decision as much as a technical one —
an attribution we cannot explain to an advertiser is one we should not be
charging a network for.

**Precedence**, highest fidelity first:

1. ``REFERRER``   — a click id inside the Play Install Referrer. Ground truth on
                    Android: it comes from Google, survives the install, and
                    cannot be claimed by a competing network.
2. ``CLICK_ID``   — a click id the SDK received explicitly, via a deferred deep
                    link. Trustworthy, but it passed through the device.
3. ``DEVICE_MATCH`` — the same hashed advertising ID present at click and at
                    install. Deterministic, but only available when the network
                    passed the identifier and the user has not opted out.
4. ``ORGANIC``    — nothing matched. Not a failure; the honest answer.

Ties within a tier break to the most recent qualifying click, which is what
"last click" means.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass

# Click injection: malware on the device observes an install beginning and fires
# a click microseconds before it completes, stealing credit from whoever actually
# earned it. A real click-to-install gap includes a store page load and a
# download, so anything under this threshold is implausible.
#
# The install is still attributed — refusing to attribute would penalise the
# advertiser for their attacker — but the gap is recorded so the fraud rules in
# Phase 12 can score it, and so a suspicious network is visible in reporting.
CLICK_INJECTION_THRESHOLD = dt.timedelta(seconds=10)


class Method(enum.StrEnum):
    REFERRER = "referrer"
    CLICK_ID = "click_id"
    DEVICE_MATCH = "device_match"
    ORGANIC = "organic"
    PROBABILISTIC = "probabilistic"  # never produced here; see the build plan


# Ordered by fidelity, for comparison and reporting.
FIDELITY = {
    Method.REFERRER: 4,
    Method.CLICK_ID: 3,
    Method.DEVICE_MATCH: 2,
    Method.PROBABILISTIC: 1,
    Method.ORGANIC: 0,
}


@dataclass(frozen=True)
class Click:
    """A candidate click, as loaded from storage."""

    click_id: uuid.UUID
    clicked_at: dt.datetime
    campaign_id: uuid.UUID | None
    tracking_link_id: uuid.UUID
    source: str | None = None
    medium: str | None = None
    device_hash: bytes | None = None
    is_bot: bool = False
    # The destination this click asked for, already validated at the edge. It
    # rides along so the attribution can carry it — see the deferred deep link
    # migration for why the copy is worth making.
    deep_link: str | None = None


@dataclass(frozen=True)
class Install:
    """The install being attributed."""

    app_id: uuid.UUID
    anonymous_id: str
    installed_at: dt.datetime
    referrer: str | None = None
    click_id: uuid.UUID | None = None
    device_hash: bytes | None = None


@dataclass(frozen=True)
class Decision:
    method: Method
    click: Click | None
    window_days: int
    # Why this decision was reached, in words a support engineer can repeat to a
    # customer. An attribution nobody can explain is one nobody can defend.
    reason: str
    click_to_install: dt.timedelta | None = None
    suspected_injection: bool = False

    @property
    def is_attributed(self) -> bool:
        return self.method is not Method.ORGANIC

    @property
    def campaign_id(self) -> uuid.UUID | None:
        return self.click.campaign_id if self.click else None

    @property
    def click_id(self) -> uuid.UUID | None:
        return self.click.click_id if self.click else None


def _within_window(click: Click, install: Install, window: dt.timedelta) -> bool:
    """A click qualifies if it happened before the install and inside the window.

    The ordering check is not redundant with the window check: a click recorded
    *after* the install is either a clock problem or an injection attempt, and
    in both cases it did not cause the install.
    """
    if click.clicked_at > install.installed_at:
        return False
    return install.installed_at - click.clicked_at <= window


def _latest(clicks: list[Click]) -> Click:
    """Last click wins. Ties on timestamp break to the larger click id, which is
    a UUIDv7 and therefore also the later one — so the rule stays deterministic
    even at identical timestamps."""
    return max(clicks, key=lambda c: (c.clicked_at, c.click_id.int))


def attribute(
    install: Install,
    candidates: list[Click],
    *,
    window_days: int,
    referrer_click_id: uuid.UUID | None = None,
    include_bots: bool = False,
) -> Decision:
    """Resolve one install to at most one click.

    ``candidates`` is every click for this app inside the window; the caller
    supplies it, this function does not fetch. ``referrer_click_id`` is the id
    already extracted from the Install Referrer by ``mmp_attrib.referrer``.
    """
    window = dt.timedelta(days=window_days)
    pool = [c for c in candidates if include_bots or not c.is_bot]
    eligible = [c for c in pool if _within_window(c, install, window)]

    def decide(method: Method, click: Click, reason: str) -> Decision:
        gap = install.installed_at - click.clicked_at
        return Decision(
            method=method,
            click=click,
            window_days=window_days,
            reason=reason,
            click_to_install=gap,
            suspected_injection=gap < CLICK_INJECTION_THRESHOLD,
        )

    # 1. Play Install Referrer. Highest fidelity: Google is telling us.
    if referrer_click_id is not None:
        match = next((c for c in eligible if c.click_id == referrer_click_id), None)
        if match is not None:
            return decide(
                Method.REFERRER,
                match,
                "Play Install Referrer carried this click id, matched within the window",
            )

    # 2. A click id the SDK was handed directly.
    if install.click_id is not None:
        match = next((c for c in eligible if c.click_id == install.click_id), None)
        if match is not None:
            return decide(
                Method.CLICK_ID,
                match,
                "the SDK reported this click id and it matched within the window",
            )

    # 3. Same hashed advertising ID at click and at install.
    if install.device_hash is not None:
        matches = [c for c in eligible if c.device_hash == install.device_hash]
        if matches:
            return decide(
                Method.DEVICE_MATCH,
                _latest(matches),
                "the advertising ID seen at click matched the one seen at install",
            )

    # 4. Nothing deterministic matched.
    #
    # Deliberately not a fallback to probabilistic matching. IP-plus-device-model
    # fingerprinting would raise the match rate and is what Apple's rules
    # prohibit for cross-app attribution; it also produces attributions that
    # cannot be defended when an advertiser asks how a number was derived.
    return Decision(
        method=Method.ORGANIC,
        click=None,
        window_days=window_days,
        reason=(
            "no click id and no device match within the attribution window"
            if pool
            else "no clicks recorded for this app within the attribution window"
        ),
    )


def should_supersede(existing: Method, incoming: Method) -> bool:
    """Whether a later signal outranks the attribution already recorded.

    Referrer data can arrive after an install has already been attributed by
    device match — Play's API is queried on first launch, which may be minutes
    later, and may need a retry if the device was offline. When it does arrive it
    is better evidence, and the record should be corrected.

    Only ever upward. A lower-fidelity signal arriving late must not overwrite a
    higher-fidelity one, or the answer would depend on delivery order.
    """
    return FIDELITY[incoming] > FIDELITY[existing]
