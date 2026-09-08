"""SKAdNetwork install-validation postbacks: verification.

Apple's postbacks arrive at a public URL with no credential of any kind. The
signature *is* the authentication — there is nothing else. An unverified
postback is an anonymous HTTP request claiming an install happened, so anyone
who learns the URL could manufacture installs, claim credit for organic ones, or
poison an advertiser's reporting. Everything in this module exists to make that
impossible, and it fails closed in every direction.

**The canonical string.** Apple signs a UTF-8 string built by joining specific
parameter values, in a version-specific order, with U+2063 INVISIBLE SEPARATOR.
The orders below come from Apple's documentation, and — more usefully — the test
suite verifies them against five postbacks that Apple published *with their real
signatures*. That is conformance to Apple's actual signing behaviour rather than
to my reading of their prose, which is the difference that matters: an ordering
that is subtly wrong produces a verifier that rejects every genuine postback,
and the obvious way to "fix" that is to stop verifying.

**Separator injection.** Because fields are joined rather than length-prefixed,
a value that itself contains U+2063 can move the boundary between two fields:
the same canonical string, and therefore the same valid signature, parses into
different values. An attacker who could do that would keep a real signature
while changing which campaign got the credit. Any value containing the separator
is rejected before it is ever joined.

**What is not signed.** Neither ``conversion-value`` nor
``coarse-conversion-value`` is covered by the signature, in any version. They
are attacker-controlled in the strict sense — a valid postback can be replayed
with a different conversion value and the signature still verifies — so they are
stored as reported and must never be treated as trustworthy on their own.

**Old versions.** 1.0 and 2.0 are signed with keys Apple distributes only
through the ad-network registration portal, not the published one below. They
are refused rather than waved through: a postback we cannot verify is one we
cannot count.
"""

from __future__ import annotations

import base64
import binascii
import enum
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

# Apple's NIST P-256 public key for versions 2.1 and later, from
# "Verifying an install-validation postback". Pinned in source deliberately:
# fetching it at runtime would make the thing that authenticates every postback
# depend on a network call that an attacker may be positioned to answer.
APPLE_PUBLIC_KEY_B64 = (
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEWdp8GPcGqmhgzEFj9Z2nSpQVddayaPe4"
    "FMzqM9wib1+aHaaIzoHoLN9zW4K8y4SPykE3YVK3sVqW6Af0lfx3gg=="
)

SEPARATOR = "⁣"  # INVISIBLE SEPARATOR

SIGNATURE_FIELD = "attribution-signature"

# A marker meaning "source-app-id, or source-domain for a web ad, whichever the
# postback carries". Version 4 allows either in the same position.
_SOURCE = "\x00source"

# Per-version field order. Optional fields are simply absent from the postback
# and are then omitted from the string entirely — not included as empty.
FIELD_ORDER: dict[str, tuple[str, ...]] = {
    "2.1": (
        "version",
        "ad-network-id",
        "campaign-id",
        "app-id",
        "transaction-id",
        "redownload",
        "source-app-id",
    ),
    "2.2": (
        "version",
        "ad-network-id",
        "campaign-id",
        "app-id",
        "transaction-id",
        "redownload",
        "source-app-id",
        "fidelity-type",
    ),
    "3.0": (
        "version",
        "ad-network-id",
        "campaign-id",
        "app-id",
        "transaction-id",
        "redownload",
        "source-app-id",
        "fidelity-type",
        "did-win",
    ),
    "4.0": (
        "version",
        "ad-network-id",
        "source-identifier",
        "app-id",
        "transaction-id",
        "redownload",
        _SOURCE,
        "fidelity-type",
        "did-win",
        "postback-sequence-index",
    ),
}

# Signed with keys we do not hold. Named separately from "unknown" so the
# rejection reason can say which it is.
UNSUPPORTED_VERSIONS = frozenset({"1.0", "2.0"})


class Rejection(enum.StrEnum):
    MISSING_VERSION = "missing_version"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNKNOWN_VERSION = "unknown_version"
    MISSING_SIGNATURE = "missing_signature"
    MALFORMED_SIGNATURE = "malformed_signature"
    MISSING_FIELD = "missing_field"
    SEPARATOR_IN_VALUE = "separator_in_value"
    BAD_SIGNATURE = "bad_signature"


@dataclass(frozen=True, slots=True)
class Verified:
    """A postback whose signature is Apple's.

    ``did_win`` is separate from validity: a valid non-winning postback is a
    real message from Apple saying this network did *not* get the attribution.
    Storing it is useful; counting it as an install is not.
    """

    version: str
    ad_network_id: str
    app_id: str
    transaction_id: str
    source_identifier: str | None
    did_win: bool
    redownload: bool
    fidelity_type: int | None
    conversion_value: int | None
    coarse_value: str | None
    postback_sequence_index: int | None
    source_app_id: str | None
    source_domain: str | None


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: Rejection
    detail: str


class SeparatorInValue(ValueError):
    """A field value contains the separator Apple joins fields with."""


def _render(value: Any) -> str:
    """One parameter, as Apple renders it in the signed string.

    Booleans are the literal words. Everything else is its plain string form —
    which is why a numeric field arriving as a quoted string still verifies, and
    a subtly different rendering (a space, a "+") does not.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def canonical_string(payload: dict[str, Any]) -> str:
    """Rebuild the exact string Apple signed.

    Raises :class:`SeparatorInValue` if any value contains the separator, which
    would let one signature cover more than one reading of the fields.
    """
    version = str(payload.get("version", ""))
    order = FIELD_ORDER[version]

    parts: list[str] = []
    for field in order:
        if field is _SOURCE:
            # Whichever of the two this postback carries; a web ad has the
            # domain, an app-to-app ad has the app id, and neither is required.
            for candidate in ("source-app-id", "source-domain"):
                if candidate in payload:
                    parts.append(_render(payload[candidate]))
            continue
        if field not in payload:
            # Optional and absent — omitted, not blank. An empty string here
            # would add a separator Apple did not sign over.
            continue
        parts.append(_render(payload[field]))

    for part in parts:
        if SEPARATOR in part:
            raise SeparatorInValue(
                "a postback value contains the field separator, which would let "
                "one signature cover more than one set of values"
            )
    return SEPARATOR.join(parts)


def _load_key(key_b64: str) -> ec.EllipticCurvePublicKey:
    key = serialization.load_der_public_key(base64.b64decode(key_b64))
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise TypeError("Apple's postback key must be an elliptic curve key")
    return key


_APPLE_KEY = _load_key(APPLE_PUBLIC_KEY_B64)


def verify(
    payload: dict[str, Any], *, key: ec.EllipticCurvePublicKey | None = None
) -> Verified | Rejected:
    """Verify one postback. Never raises; every failure is a :class:`Rejected`.

    ``key`` is injectable so tests can sign their own postbacks, and so an ad
    network holding a version-1 key from Apple's portal can supply it. It
    defaults to Apple's published key.
    """
    version = payload.get("version")
    if not version:
        # Version 1 postbacks genuinely have no version field. They are also
        # signed with a key we do not have, so this is the right answer anyway.
        return Rejected(Rejection.MISSING_VERSION, "postback carries no version")
    version = str(version)

    if version in UNSUPPORTED_VERSIONS:
        return Rejected(
            Rejection.UNSUPPORTED_VERSION,
            f"version {version} is signed with a key issued through Apple's "
            f"registration portal, which this deployment does not hold",
        )
    if version not in FIELD_ORDER:
        return Rejected(Rejection.UNKNOWN_VERSION, f"unrecognised version {version!r}")

    raw_signature = payload.get(SIGNATURE_FIELD)
    if not raw_signature or not isinstance(raw_signature, str):
        return Rejected(Rejection.MISSING_SIGNATURE, "no attribution-signature")
    try:
        signature = base64.b64decode(raw_signature, validate=True)
    except (binascii.Error, ValueError):
        return Rejected(Rejection.MALFORMED_SIGNATURE, "signature is not valid base64")

    # Every field the version requires must be present, except the ones Apple
    # documents as optional. A postback missing a required field cannot be
    # rebuilt, and guessing at a value would be inventing evidence.
    required = {
        field
        for field in FIELD_ORDER[version]
        if field is not _SOURCE and field not in {"source-app-id"}
    }
    missing = sorted(required - payload.keys())
    if missing:
        return Rejected(Rejection.MISSING_FIELD, f"missing {', '.join(missing)}")

    try:
        message = canonical_string(payload).encode("utf-8")
    except SeparatorInValue as exc:
        return Rejected(Rejection.SEPARATOR_IN_VALUE, str(exc))

    try:
        (key or _APPLE_KEY).verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return Rejected(Rejection.BAD_SIGNATURE, "signature is not Apple's")

    return Verified(
        version=version,
        ad_network_id=str(payload["ad-network-id"]),
        app_id=str(payload["app-id"]),
        transaction_id=str(payload["transaction-id"]),
        # Renamed in version 4; the same thing under both names.
        source_identifier=(
            str(payload["source-identifier"])
            if "source-identifier" in payload
            else str(payload["campaign-id"])
            if "campaign-id" in payload
            else None
        ),
        # Absent before version 3, where every postback was a winner.
        did_win=bool(payload.get("did-win", True)),
        redownload=bool(payload.get("redownload", False)),
        fidelity_type=_as_int(payload.get("fidelity-type")),
        # Not covered by the signature — see the module docstring.
        conversion_value=_as_int(payload.get("conversion-value")),
        coarse_value=(
            str(payload["coarse-conversion-value"])
            if payload.get("coarse-conversion-value") is not None
            else None
        ),
        postback_sequence_index=_as_int(payload.get("postback-sequence-index")),
        source_app_id=(
            str(payload["source-app-id"]) if payload.get("source-app-id") is not None else None
        ),
        source_domain=(
            str(payload["source-domain"]) if payload.get("source-domain") is not None else None
        ),
    )


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
