"""Google Play Install Referrer parsing.

On first launch an Android app can ask the Play Install Referrer API for the
string that was attached to the store link the user came through. That string is
the closest thing to ground truth in mobile attribution: it survives the install,
it comes from Google rather than from us, and it cannot be inferred or spoofed
by a competing network claiming the same install.

The format is a URL query string, and it is *not* ours to control — a network,
an agency, or the advertiser's own marketing team may have appended their own
parameters, or replaced ours entirely. So parsing is deliberately forgiving:
find our click id if it is there, keep the rest for diagnostics, and never raise.
A referrer we cannot parse means an organic install, not an error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote

# Where we put the click id, and the conventional places others put theirs.
# utm_content is checked first because it is what our own redirect writes.
CLICK_ID_KEYS = ("utm_content", "mmp_click_id", "click_id", "clickid", "cid")
DEEP_LINK_KEYS = ("deep_link", "deeplink", "af_dp")

MAX_REFERRER_LENGTH = 2048


@dataclass(frozen=True)
class ParsedReferrer:
    click_id: uuid.UUID | None
    deep_link: str | None
    source: str | None
    medium: str | None
    campaign: str | None
    raw_params: dict[str, str] = field(default_factory=dict)

    @property
    def is_organic(self) -> bool:
        """No click id and no campaign markers: nobody is claiming this install."""
        return self.click_id is None and not any((self.source, self.medium, self.campaign))


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        return None


def parse_referrer(referrer: str | None) -> ParsedReferrer:
    """Extract what we can. Never raises.

    Play may double-encode the string, and some networks encode it again on top
    of that, so one speculative decode is attempted when the value still looks
    encoded. More than one round would risk mangling a legitimate value that
    happens to contain a percent sign.
    """
    empty = ParsedReferrer(click_id=None, deep_link=None, source=None, medium=None, campaign=None)
    if not referrer:
        return empty

    candidate = referrer[:MAX_REFERRER_LENGTH]
    if "%3D" in candidate or "%26" in candidate:
        candidate = unquote(candidate)

    try:
        params = {
            key: values[0]
            for key, values in parse_qs(candidate, keep_blank_values=False).items()
            if values
        }
    except (ValueError, UnicodeDecodeError):
        return empty

    # A network using utm_content for its own creative name is entirely normal,
    # so a value that is not a UUID is not an error — it just is not ours.
    # Logging every one of those would be noise at click volume.
    click_id = next(
        (parsed for key in CLICK_ID_KEYS if (parsed := _as_uuid(params.get(key)))), None
    )

    deep_link = next((params[key] for key in DEEP_LINK_KEYS if params.get(key)), None)

    return ParsedReferrer(
        click_id=click_id,
        deep_link=unquote(deep_link) if deep_link else None,
        source=params.get("utm_source"),
        medium=params.get("utm_medium"),
        campaign=params.get("utm_campaign"),
        raw_params=params,
    )
