"""Deep link destinations, and why they are not taken at face value.

A deep link arrives on the click as a query parameter, which means it is
supplied by whoever wrote the link — an ad network, an affiliate, or anyone who
can get a person to tap a URL. It is then stored, survives the install, and is
handed to the app on first launch, where the app opens it.

That makes it one of the few values in this system that crosses from an
untrusted source into code execution on someone's phone. An app that routes
``/settings/delete-account`` or ``myapp://transfer?to=attacker`` because a link
said so has been used as a weapon, and the link came through us.

So a destination is accepted in exactly two forms:

* **A registered code.** The advertiser pre-registers destinations and the link
  names one. Nothing arbitrary crosses the boundary, and the advertiser gets a
  fallback URL for people who do not install.
* **A relative path**, validated by :func:`clean_path` below. Practical, because
  nobody wants to pre-register a code per product, and safe because a relative
  path cannot name a different app, a different host, or a different scheme.

What is never accepted is a full URL. ``myapp://`` from a query parameter would
let a link address any scheme the device has registered — including another
vendor's app — and ``https://`` would make this an open redirect with our domain
on it. If an advertiser needs a scheme, they register a code.
"""

from __future__ import annotations

import re

# Long enough for a real path with a few query parameters, short enough that
# this cannot be used to pack a payload through the referrer, which Google Play
# truncates anyway.
MAX_DESTINATION = 512

# One leading slash, then path characters. The character class is an allowlist
# rather than a list of things to strip: stripping is how validators get bypassed,
# because the attacker only has to find one character the stripper missed.
_SAFE_PATH = re.compile(r"^/(?!/)[A-Za-z0-9\-._~/%!$&'()*+,;=:@\[\]?#]*$")

# Rejected wherever they appear, including percent-encoded, because the app is
# what finally decodes this and it will decode more than we do.
_TRAVERSAL = re.compile(r"(^|/)\.\.($|/)|%2e%2e|%252e", re.IGNORECASE)


def clean_path(raw: str | None) -> str | None:
    """Return ``raw`` if it is a safe relative path, otherwise ``None``.

    Returning ``None`` rather than raising, and rather than sanitising: a link
    with a destination we will not honour is still a click worth recording, and
    the person tapping it should still reach the store. Silently dropping the
    destination degrades to a normal install. Sanitising would mean guessing
    what the author meant, and a guess that lands somewhere unintended is the
    whole failure this function exists to prevent.
    """
    if not raw:
        return None

    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_DESTINATION:
        return None

    # Control characters, including the CR and LF that would let a destination
    # inject a header when it is echoed into the Location of a later redirect.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        return None

    # Backslashes are rejected outright: several URL parsers treat "\" as "/",
    # so "/\evil.com" is protocol-relative to some readers and a path to others.
    # Anything two parsers disagree about is not something to pass along.
    if "\\" in candidate:
        return None

    if _TRAVERSAL.search(candidate):
        return None

    if not _SAFE_PATH.match(candidate):
        return None

    return candidate


def is_registered_code(raw: str | None) -> bool:
    """Whether ``raw`` looks like a deep link code worth a lookup.

    Checked before touching the database so that a flood of junk codes on the
    redirect path cannot be turned into a flood of queries.
    """
    return bool(raw) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", raw or ""))
