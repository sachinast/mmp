"""Attribution: deterministic last-click, as pure functions."""

from mmp_attrib.engine import (
    CLICK_INJECTION_THRESHOLD,
    FIDELITY,
    Click,
    Decision,
    Install,
    Method,
    attribute,
    should_supersede,
)
from mmp_attrib.referrer import ParsedReferrer, parse_referrer

__all__ = [
    "CLICK_INJECTION_THRESHOLD",
    "FIDELITY",
    "Click",
    "Decision",
    "Install",
    "Method",
    "ParsedReferrer",
    "attribute",
    "parse_referrer",
    "should_supersede",
]
