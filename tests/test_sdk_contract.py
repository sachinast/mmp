"""The React Native SDK's contract with this server.

The SDK is TypeScript and the server is Python, so nothing but a test connects
them. Every constant duplicated across that boundary — batch limits, reserved
event names, consent vocabulary, wire field names — is a place where the two can
drift apart silently, and the symptom of drift is a 422 in someone's production
app rather than a failure here.

So these tests read the SDK source and assert it agrees with the values the
server actually enforces. They are deliberately literal about it: they parse the
TypeScript rather than trusting a comment that says the numbers match.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import msgspec
import pytest
from mmp_ingest.consent import Purpose, State
from mmp_ingest.schema import (
    MAX_EVENT_NAME_LENGTH,
    MAX_EVENTS_PER_BATCH,
    MAX_ID_LENGTH,
    MAX_PROPERTIES_BYTES,
    IncomingEvent,
)

SDK = Path(__file__).resolve().parents[1] / "sdks" / "react-native" / "src"

pytestmark = pytest.mark.skipif(not SDK.exists(), reason="SDK sources not present")


def _types() -> str:
    return (SDK / "types.ts").read_text()


def _number(name: str, source: str) -> int:
    """Pull one numeric literal out of the SDK's LIMITS block."""
    match = re.search(rf"{name}:\s*([0-9*\s]+),", source)
    assert match, f"{name} not found in the SDK's LIMITS"
    # Written as `16 * 1024` in the SDK, exactly as it is in Python.
    return int(eval(match.group(1).strip(), {"__builtins__": {}}))  # noqa: S307


def _string_union(name: str, source: str) -> set[str]:
    match = re.search(rf"export type {name} =\s*([^;]+);", source)
    assert match, f"type {name} not found in the SDK"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_the_batch_and_size_limits_match() -> None:
    """The SDK refuses locally what the server would refuse remotely. If these
    drift the SDK either rejects valid events or sends ones that 422."""
    source = _types()
    assert _number("eventName", source) == MAX_EVENT_NAME_LENGTH
    assert _number("id", source) == MAX_ID_LENGTH
    assert _number("propertiesBytes", source) == MAX_PROPERTIES_BYTES
    assert _number("eventsPerBatch", source) == MAX_EVENTS_PER_BATCH


def test_the_consent_vocabulary_matches() -> None:
    """A purpose the SDK reports under a name the server does not recognise is
    dropped silently at ingest — the user's decision would be lost."""
    assert _string_union("Purpose", _types()) == {p.value for p in Purpose}


def test_the_consent_states_the_sdk_can_report_are_all_understood() -> None:
    """UNKNOWN is server-side only: it means "not told yet", which is a state
    the SDK can never be *reporting*."""
    sdk_states = _string_union("ConsentState", _types())
    assert sdk_states <= {s.value for s in State}
    assert State.UNKNOWN.value not in sdk_states


def test_every_event_name_the_sdk_reserves_is_one_the_server_treats_specially() -> None:
    """And every specially-treated name is reserved. A name the server acts on
    but the SDK lets an app send is a way for an app to corrupt its own
    attribution."""
    from mmp_tracker.ingest import CONSENT_EVENT
    from mmp_worker.attribution import IDENTITY_EVENTS, INSTALL_EVENTS

    source = (SDK / "types.ts").read_text()
    match = re.search(r"RESERVED_EVENTS = \[([^\]]+)\]", source)
    assert match, "RESERVED_EVENTS not found in the SDK"
    reserved = set(re.findall(r'"([^"]+)"', match.group(1)))

    # Both sides folded, because both sides now *match* on the folded form.
    # Comparing the literals would fail on "consent_update" versus
    # "consentupdate" while the behaviour was identical.
    from mmp_ingest.schema import canonical_event_name

    reserved = {canonical_event_name(name) for name in reserved}
    special = set(INSTALL_EVENTS) | set(IDENTITY_EVENTS) | {CONSENT_EVENT}
    assert reserved == special, (
        f"the SDK reserves {sorted(reserved)} but the server treats {sorted(special)} specially"
    )


def test_the_sdk_sends_no_field_the_server_does_not_accept() -> None:
    """An unknown field is not merely ignored — msgspec rejects the batch, so
    one stray field in the SDK would stop every event from that version."""
    source = (SDK / "types.ts").read_text()
    block = re.search(r"export interface WireEvent \{(.*?)\n\}", source, re.S)
    assert block, "WireEvent not found in the SDK"
    sdk_fields = set(re.findall(r"^\s*(\w+)\??:", block.group(1), re.M))

    accepted = {f.encode_name for f in msgspec.structs.fields(IncomingEvent)}
    unknown = sdk_fields - accepted
    assert not unknown, f"the SDK sends fields the server will reject: {sorted(unknown)}"


def test_the_sdk_always_sends_what_the_server_requires() -> None:
    required = {
        f.encode_name
        for f in msgspec.structs.fields(IncomingEvent)
        if f.default is msgspec.NODEFAULT and f.default_factory is msgspec.NODEFAULT
    }
    source = (SDK / "client.ts").read_text()
    for field in required:
        assert f"{field}:" in source, (
            f"the server requires {field} but the SDK's event construction never sets it"
        )


def test_the_endpoints_the_sdk_calls_exist_on_the_tracker() -> None:
    """A typo in a path is a total outage for the SDK, and one that no amount of
    retrying recovers from."""
    from mmp_tracker.app import create_app

    from tests.conftest_ingest import build_settings_for

    paths = {
        route.path  # type: ignore[attr-defined]
        for route in create_app(build_settings_for("mmp_tracker")).routes
    }
    called = set(re.findall(r'"(/v1/[a-z0-9/_-]+)"', (SDK / "transport.ts").read_text()))
    assert called, "no endpoints found in the SDK transport"
    assert called <= paths, f"the SDK calls paths the tracker does not serve: {called - paths}"


def test_the_sdk_package_declares_no_runtime_dependencies() -> None:
    """A measurement SDK is a guest in someone else's app. Every runtime
    dependency is a version conflict waiting to happen in an ecosystem where
    dependency resolution is already the hardest part of an upgrade."""
    manifest = json.loads((SDK.parent / "package.json").read_text())
    assert manifest.get("dependencies", {}) == {}
