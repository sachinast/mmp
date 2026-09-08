"""Rule-based fraud signals.

Pure functions, on the same terms as ``engine``: no database, no clock, no
network, every input a parameter. Fraud accusations get argued about — with a
network, sometimes with a lawyer — so the assessment that produced one has to be
replayable exactly, months later, from stored inputs.

**Rules, not a model.** Every signal here is a stated threshold with a stated
reason. A trained classifier would very likely catch more, and it is the obvious
next step, but it cannot answer "why did you stop paying us for these 40,000
installs" with anything an advertiser can act on. A rule can, and a network that
knows the rule can stop breaking it — which is the actual goal. Detection that
cannot be explained just moves the fraud somewhere less visible.

**Flag, do not silently discard.** Nothing here changes an attribution. A
suspicious install is still attributed, and the signal is recorded alongside it.
Two reasons: an advertiser whose traffic is being injected should not also lose
the installs they genuinely paid for, and a system that quietly drops data makes
its own errors invisible. Whether a flagged conversion is billed, or is sent
onward to a network, is a commercial decision — this module supplies the
evidence for it and deliberately does not make it.

**False positives are the expensive error.** Wrongly flagging a legitimate
network costs a partnership; missing some fraud costs money that is already
mostly lost. Thresholds are therefore set where a legitimate explanation is
genuinely hard to construct, not where the signal first appears.

**Not here yet.** Clicks from hosting-provider networks are a strong signal and
are deliberately absent: IPs are hashed at the edge for privacy, so the raw
address is gone before this module could see it, and classifying it would have
to happen in the tracker against a maintained CIDR list. That is real work with
a real data dependency, and a rule that can never fire is worse than an absent
one — it reads as coverage that does not exist.

Two entry points, because the two kinds of evidence arrive at different times:

* :func:`assess_install` runs inline at attribution, on one install and the
  click that won it.
* :func:`assess_traffic` runs periodically over a window of aggregates, where
  patterns that are invisible per-install — flooding, click farms — show up.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field

# A real click-to-install gap contains a store page load, a download and a first
# open. Ten seconds is not merely fast; it is not physically possible on a mobile
# network, which is what makes it safe to flag rather than merely suspicious.
CLICK_INJECTION_THRESHOLD = dt.timedelta(seconds=10)

# Above this, a "click" that converted is more plausibly a flood entry that
# happened to sit in front of an organic install than a click anyone made. Set
# at a week rather than a day: long consideration cycles are real, particularly
# for expensive purchases, and one late install is not evidence of anything.
LATE_CONVERSION_THRESHOLD = dt.timedelta(days=7)

# Click flooding, judged on the shape of the distribution rather than volume.
# Genuine traffic converts mostly in the first hour; a flood converts uniformly
# across the window, because its conversions are organic installs it did not
# cause. Requires a minimum sample, or a link with three late installs out of
# four looks identical to an attack.
FLOOD_MIN_ATTRIBUTED = 50
FLOOD_LATE_SHARE = 0.60

# One IP hash behind many distinct devices. Carrier NAT and office wifi do this
# legitimately, so the threshold is set well above what either produces in a day.
CLICK_FARM_MIN_DEVICES = 75

# The same device credited with repeat installs of one app. Reinstalls are
# ordinary; this many in a window is a device being replayed.
DEVICE_REPLAY_THRESHOLD = 6


class Rule(enum.StrEnum):
    CLICK_INJECTION = "click_injection"
    LATE_CONVERSION = "late_conversion"
    BOT_CLICK = "bot_click"
    DEVICE_REPLAY = "device_replay"
    CLICK_FLOODING = "click_flooding"
    CLICK_FARM = "click_farm"


class Severity(enum.IntEnum):
    """Weights, not labels — an assessment's score is their sum.

    Spaced apart deliberately: two LOW signals should not add up to one HIGH,
    because two weak independent hints are not equivalent to one strong finding.
    """

    LOW = 10
    MEDIUM = 25
    HIGH = 50
    CRITICAL = 100


class Verdict(enum.StrEnum):
    CLEAN = "clean"
    SUSPICIOUS = "suspicious"
    FRAUDULENT = "fraudulent"


# One CRITICAL alone is enough to condemn; anything less needs corroboration.
SUSPICIOUS_AT = 25
FRAUDULENT_AT = 100


@dataclass(frozen=True, slots=True)
class Signal:
    rule: Rule
    severity: Severity
    # In words a support engineer can repeat to a customer verbatim. If a signal
    # cannot be explained in one sentence, it is not ready to be acted on.
    detail: str


@dataclass(frozen=True, slots=True)
class Assessment:
    signals: list[Signal] = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum(s.severity for s in self.signals)

    @property
    def verdict(self) -> Verdict:
        score = self.score
        if score >= FRAUDULENT_AT:
            return Verdict.FRAUDULENT
        if score >= SUSPICIOUS_AT:
            return Verdict.SUSPICIOUS
        return Verdict.CLEAN

    @property
    def rules(self) -> list[str]:
        return [s.rule.value for s in self.signals]


@dataclass(frozen=True, slots=True)
class InstallContext:
    """Everything the per-install rules look at.

    Explicit rather than passing the ``Decision`` and ``Click`` through: the
    rules should depend on a small, stable set of facts, so that changing how
    attribution is represented does not silently change what counts as fraud.
    """

    # None for an organic install — there is no click, so no click rule applies.
    click_to_install: dt.timedelta | None = None
    click_is_bot: bool = False


def assess_install(context: InstallContext) -> Assessment:
    """Score one install against the rules that need only this install."""
    signals: list[Signal] = []
    gap = context.click_to_install

    if gap is not None:
        if gap < dt.timedelta(0):
            # The click is recorded after the install it supposedly caused.
            # Attribution excludes these, so reaching here means a clock is
            # wrong or a timestamp was forged; either way it is not evidence
            # of a click that happened.
            signals.append(
                Signal(
                    Rule.CLICK_INJECTION,
                    Severity.CRITICAL,
                    "the click is timestamped after the install it is credited with",
                )
            )
        elif gap < CLICK_INJECTION_THRESHOLD:
            signals.append(
                Signal(
                    Rule.CLICK_INJECTION,
                    Severity.CRITICAL,
                    f"only {gap.total_seconds():.1f}s between click and install, which is "
                    f"less than a store page load and download take",
                )
            )
        elif gap > LATE_CONVERSION_THRESHOLD:
            # On its own this is weak — people do install a week later. It earns
            # its weight by corroborating a flooding finding on the same link.
            signals.append(
                Signal(
                    Rule.LATE_CONVERSION,
                    Severity.LOW,
                    f"{gap.days} days between click and install, near the edge of the window",
                )
            )

    if context.click_is_bot:
        signals.append(
            Signal(
                Rule.BOT_CLICK,
                Severity.HIGH,
                "the click came from a user agent on the known-bot list",
            )
        )

    return Assessment(signals=signals)


@dataclass(frozen=True, slots=True)
class TrafficWindow:
    """Aggregates for one tracking link over one window.

    Counts, not rows: the rules need the shape of the traffic, and shipping
    millions of rows into Python to compute a ratio the database can compute
    is how a fraud job becomes the reason the database is slow.
    """

    attributed_installs: int = 0
    # Of those, the ones whose click-to-install exceeded the late threshold.
    late_installs: int = 0
    # The largest number of distinct devices seen behind any single IP hash.
    max_devices_per_ip: int = 0
    # The most installs any one device was credited with on this link. Here
    # rather than in the per-install rules deliberately: answering "how many
    # installs does this device have" per install would be one indexed query
    # per attribution, and it is a property of a population anyway.
    max_installs_per_device: int = 0


def assess_traffic(window: TrafficWindow) -> Assessment:
    """Score one tracking link against the rules that need a population."""
    signals: list[Signal] = []

    if window.attributed_installs >= FLOOD_MIN_ATTRIBUTED:
        late_share = window.late_installs / window.attributed_installs
        if late_share >= FLOOD_LATE_SHARE:
            signals.append(
                Signal(
                    Rule.CLICK_FLOODING,
                    Severity.CRITICAL,
                    f"{late_share:.0%} of installs on this link converted more than "
                    f"{LATE_CONVERSION_THRESHOLD.days} days after the click, across "
                    f"{window.attributed_installs} installs — genuine traffic converts "
                    f"mostly within the hour",
                )
            )

    if window.max_devices_per_ip >= CLICK_FARM_MIN_DEVICES:
        signals.append(
            Signal(
                Rule.CLICK_FARM,
                Severity.HIGH,
                f"one address produced clicks from {window.max_devices_per_ip} distinct "
                f"devices, more than carrier NAT or an office network explains",
            )
        )

    if window.max_installs_per_device >= DEVICE_REPLAY_THRESHOLD:
        signals.append(
            Signal(
                Rule.DEVICE_REPLAY,
                Severity.MEDIUM,
                f"one device was credited with {window.max_installs_per_device} installs of "
                f"this app on this link, more than reinstalling explains",
            )
        )

    return Assessment(signals=signals)
