"""Static guards on the SDK's native code.

The Kotlin here is never compiled by this repo's tooling and neither half is
ever executed by it, so these are assertions about source text rather than about
behaviour — and they are labelled that way deliberately. They are not a
substitute for running the SDK on a device; they exist because the invariants
below are silent when broken, and a silent break in this particular code means
either collecting an identifier the user refused or giving every opted-out
device on earth the same one.

The iOS half *is* typechecked against the real iOS SDK by `make sdk`, on any
machine with Xcode. The Android half is not compiled anywhere yet, which is
recorded in the SDK README as outstanding work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[1] / "sdks" / "react-native"
SWIFT = SDK / "ios" / "MmpIdentifiers.swift"
KOTLIN = SDK / "android" / "src" / "main" / "java" / "com" / "mmp" / "MmpIdentifiers.kt"

pytestmark = pytest.mark.skipif(not SWIFT.exists(), reason="SDK native sources not present")

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def test_ios_never_reads_the_idfa_without_att_authorisation() -> None:
    """Reading it unauthorised is what App Tracking Transparency exists to
    prevent, and Apple rejects apps for it. The guard must come first — reading
    and then discarding is not the same thing."""
    source = SWIFT.read_text()
    body = source.split("func advertisingIdentifier()")[1].split("\n    }")[0]

    guard = body.index("isTrackingAuthorized()")
    read = body.index("advertisingIdentifier.uuidString")
    assert guard < read, "the IDFA is read before the authorisation is checked"
    assert "guard" in body[:guard], "the authorisation check does not gate the read"


def test_ios_checks_att_before_falling_back_to_the_legacy_switch() -> None:
    source = SWIFT.read_text()
    assert "ATTrackingManager.trackingAuthorizationStatus == .authorized" in source
    assert "isAdvertisingTrackingEnabled" in source, (
        "the pre-iOS-14 switch must still be honoured; assuming allowed there "
        "would read the identifier of someone who turned it off"
    )


@pytest.mark.parametrize("path", [SWIFT, KOTLIN], ids=["ios", "android"])
def test_the_all_zero_identifier_is_treated_as_no_identifier(path: Path) -> None:
    """Both platforms answer an opt-out by returning this rather than failing.
    Treating it as a value gives every opted-out device the same identifier, so
    the server matches them all to each other — the exact opposite of the
    opt-out's intent."""
    source = path.read_text()
    assert ZERO_UUID in source, f"{path.name} does not check for the opted-out identifier"
    assert re.search(r"ignoreCase\s*=\s*true|caseInsensitiveCompare", source), (
        f"{path.name} compares the zero identifier case-sensitively; the two "
        f"platforms disagree on the case they return it in"
    )


def test_android_honours_limit_ad_tracking() -> None:
    """A separate signal from the zero UUID, and older devices set this one
    while still returning a real identifier."""
    source = KOTLIN.read_text()
    assert "isLimitAdTrackingEnabled" in source
    body = source.split("fun advertisingId(")[1]
    limit = body.index("isLimitAdTrackingEnabled")
    returned = body.index("return")
    assert limit < body.index("info.id"), "the opt-out is checked after the id is taken"
    assert returned >= 0


def test_android_does_not_force_play_services_onto_the_host_app() -> None:
    """`implementation` would put our chosen version of Play Services into
    someone else's release and break their build. The classes are reached
    through a try/catch that already handles their absence."""
    gradle = (SDK / "android" / "build.gradle").read_text()
    for library in ("play-services-ads-identifier", "installreferrer"):
        line = next(line for line in gradle.splitlines() if library in line)
        assert line.strip().startswith("compileOnly"), (
            f"{library} must be compileOnly, not a forced dependency"
        )


def test_android_declares_the_ad_id_permission() -> None:
    """Without it, Android 13+ returns zeros — and that failure is
    indistinguishable from the user opting out."""
    manifest = (SDK / "android" / "src" / "main" / "AndroidManifest.xml").read_text()
    assert "com.google.android.gms.permission.AD_ID" in manifest


@pytest.mark.parametrize("path", [SWIFT, KOTLIN], ids=["ios", "android"])
def test_the_native_code_never_logs_an_identifier(path: Path) -> None:
    """An advertising ID in logcat or the device console is readable by other
    software on the device and by anyone with the phone plugged in."""
    source = path.read_text()
    logging = re.findall(r"(?:Log\.[a-z]+|NSLog|print|os_log)\s*\(", source)
    assert not logging, f"{path.name} logs from identifier code: {logging}"


def test_the_sdk_never_presents_the_att_prompt_itself() -> None:
    """It can be shown once per install and the app owns that moment — after
    explaining why, somewhere it makes sense. An SDK that fires it during
    initialize spends the one chance on a cold launch."""
    for source_file in (SDK / "src").glob("*.ts"):
        assert "requestTrackingAuthorization" not in source_file.read_text(), (
            f"{source_file.name} triggers the ATT prompt; only the host app may"
        )


def test_the_referrer_connection_is_always_closed() -> None:
    """The Play Install Referrer service leaks a binding otherwise, and it can
    only be read once per install — a leak means losing that one chance."""
    source = KOTLIN.read_text()
    assert "endConnection()" in source
    assert "compareAndSet" in source, (
        "the referrer listener can fire twice; without a guard the connection "
        "is closed twice and the second close throws"
    )
