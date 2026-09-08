"""The provider adapter framework.

An MMP's long-term cost is its integrations. The failure mode is predictable:
each network's requirement lands as a branch in the delivery path, and within a
year the core cannot be changed without reasoning about six networks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

import pytest
from mmp_providers.base import (
    AuthStyle,
    Capability,
    DeliveryVerdict,
    PreparedRequest,
    Provider,
    ProviderConfig,
    missing_credentials,
    unknown_variables,
)

from mmp_providers import registry


@pytest.fixture(autouse=True)
def builtin_registry():
    registry.load_builtin_once()


CONTEXT = {
    "event_id": "01a0-abc",
    "event_name": "purchase",
    "event_timestamp": "2026-09-08T12:00:00+00:00",
    "click_id": "01a0-click",
    "campaign_id": "01a0-camp",
    "campaign_name": "Meta US",
    "revenue": "19.99",
    "currency": "USD",
    "attribution_method": "referrer",
    "anonymous_id": "device-1",
}


# --- the contract -------------------------------------------------------
def test_every_registered_adapter_satisfies_the_protocol():
    for provider in registry.available():
        assert isinstance(provider, Provider), provider.name


def test_adapters_declare_capabilities_rather_than_being_assumed():
    """So the dashboard can refuse to configure a rule the provider will
    reject — a validation error at save time instead of a delivery failure
    found in a log a week later."""
    for provider in registry.available():
        assert provider.capabilities, f"{provider.name} declares nothing"
        assert all(isinstance(c, Capability) for c in provider.capabilities)


def test_adapters_cannot_send_anything_themselves():
    """The narrowness is the point: a network's quirk must not be able to
    weaken a control that exists for all of them.

    An adapter that could make its own request would sit outside the SSRF guard,
    the delivery claim and the retry policy.
    """
    import inspect

    from mmp_providers.adapters import custom, s2s_json

    for module in (custom, s2s_json):
        source = inspect.getsource(module)
        for forbidden in ("httpx", "requests", "urllib.request", "socket", "asyncpg"):
            assert forbidden not in source, (
                f"{module.__name__} references {forbidden}; adapters describe "
                "requests, they do not send them, and they never touch the database"
            )


def test_registration_refuses_a_duplicate_name():
    """Two adapters for one network by different people, and whichever imported
    last would win — a very confusing way to find out."""
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.get("custom"))


def test_an_unknown_provider_names_what_is_available():
    with pytest.raises(registry.UnknownProvider, match="available"):
        registry.get("a-network-we-do-not-have")


def test_capability_filtering():
    with_refunds = registry.supporting(Capability.REFUNDS)
    assert with_refunds
    assert all(Capability.REFUNDS in p.capabilities for p in with_refunds)


# --- the custom adapter -------------------------------------------------
def test_custom_builds_a_url_from_a_template():
    provider = registry.get("custom")
    config = ProviderConfig(
        settings={"url_template": "https://n.example/c?click={{click_id}}&rev={{revenue}}"}
    )
    prepared = provider.prepare(event_name="purchase", context=CONTEXT, config=config)

    assert prepared is not None
    assert prepared.method == "GET"
    assert "click=01a0-click" in prepared.url
    assert "rev=19.99" in prepared.url


def test_custom_rejects_an_http_template():
    provider = registry.get("custom")
    problems = provider.validate(ProviderConfig(settings={"url_template": "http://n.example/c"}))
    assert any("https" in p for p in problems)


def test_custom_rejects_unknown_variables():
    """An adapter is not a way around the template allowlist."""
    provider = registry.get("custom")
    problems = provider.validate(
        ProviderConfig(settings={"url_template": "https://n.example/c?x={{password}}"})
    )
    assert any("password" in p for p in problems)


def test_custom_does_not_guess_at_a_response_body():
    """A custom endpoint has no contract we know about, and guessing at one
    produces confident wrong answers."""
    provider = registry.get("custom")
    verdict = provider.interpret(status_code=200, body='{"error": "nope"}')
    assert verdict.accepted, "a 2xx from an unknown endpoint is all we can go on"


# --- the JSON adapter ---------------------------------------------------
def _json_config(**settings) -> ProviderConfig:
    base = {"endpoint": "https://n.example/conversions"}
    base.update(settings)
    return ProviderConfig(credentials={"api_token": "tok"}, settings=base)


def test_json_adapter_translates_our_event_names():
    """An event posted under a name the provider has never heard of is
    discarded silently at their end, which looks identical to a delivery
    problem at ours."""
    provider = registry.get("s2s_json")
    prepared = provider.prepare(
        event_name="signup",
        context={**CONTEXT, "event_name": "signup"},
        config=_json_config(),
    )
    assert prepared is not None
    payload = json.loads(prepared.body)
    assert payload["event_name"] == "complete_registration"


def test_json_adapter_skips_unmapped_events():
    """Not an error. An event this integration was not configured to send is
    skipped quietly rather than attempted and failed."""
    provider = registry.get("s2s_json")
    assert (
        provider.prepare(event_name="session_start", context=CONTEXT, config=_json_config()) is None
    )


def test_json_adapter_event_map_can_be_overridden_per_integration():
    provider = registry.get("s2s_json")
    prepared = provider.prepare(
        event_name="purchase",
        context=CONTEXT,
        config=_json_config(event_map={"purchase": "their_purchase_name"}),
    )
    payload = json.loads(prepared.body)
    assert payload["event_name"] == "their_purchase_name"


def test_an_adapters_event_map_cannot_be_mutated_at_runtime():
    """The map is class-level state shared by every integration on that adapter.

    A mutation would therefore leak across tenants — one organisation's edit
    silently changing what another organisation sends. The type says read-only;
    this asserts the runtime agrees, because a type annotation alone stops
    nobody.
    """
    for provider in registry.available():
        with pytest.raises(TypeError):
            provider.event_map["install"] = "hijacked"  # type: ignore[index]


def test_a_configured_event_map_replaces_rather_than_merges():
    """The map is a filter as well as a translation.

    With merge semantics an integration could rename an event but never stop
    sending one, so "only send installs" would be inexpressible — which is a
    thing advertisers ask for constantly. The cost is that renaming one event
    means listing them all; that is visible in a configuration and easy to get
    right, where silently sending an event someone tried to exclude is neither.
    """
    provider = registry.get("s2s_json")
    installs_only = _json_config(event_map={"install": "app_install"})

    assert provider.prepare(event_name="install", context=CONTEXT, config=installs_only) is not None
    assert provider.prepare(event_name="purchase", context=CONTEXT, config=installs_only) is None, (
        "an event left out of a configured map must not be sent"
    )


def test_json_adapter_rejects_a_map_of_events_we_never_emit():
    """A typo that would otherwise sit silently in a configuration for months."""
    provider = registry.get("s2s_json")
    problems = provider.validate(_json_config(event_map={"purchsae": "x"}))
    assert any("purchsae" in p for p in problems)


def test_json_adapter_requires_its_credential():
    provider = registry.get("s2s_json")
    problems = provider.validate(
        ProviderConfig(credentials={}, settings={"endpoint": "https://n.example/c"})
    )
    assert any("api_token" in p for p in problems)


def test_json_adapter_authenticates():
    provider = registry.get("s2s_json")
    prepared = provider.prepare(event_name="purchase", context=CONTEXT, config=_json_config())
    assert prepared.headers["authorization"] == "Bearer tok"


def test_json_adapter_omits_identifiers_the_context_does_not_carry():
    """A denied consent purpose means the field is absent from the context, not
    blanked here — so an adapter cannot reintroduce it."""
    provider = registry.get("s2s_json")
    stripped = {k: v for k, v in CONTEXT.items() if k != "anonymous_id"}
    prepared = provider.prepare(event_name="purchase", context=stripped, config=_json_config())
    payload = json.loads(prepared.body)
    assert "anonymous_id" not in payload


def test_json_adapter_reads_the_body_not_just_the_status():
    """A 200 carrying an error is a rejection. Recording it as delivered is how
    an advertiser spends a month believing conversions are arriving."""
    provider = registry.get("s2s_json")

    rejected = provider.interpret(status_code=200, body='{"error": "unknown campaign"}')
    assert not rejected.accepted
    assert "unknown campaign" in rejected.detail
    assert not rejected.retryable, "the provider understood and refused"

    accepted = provider.interpret(status_code=200, body='{"id": "abc"}')
    assert accepted.accepted


def test_json_adapter_accepts_an_unparseable_success_body():
    """A 2xx with a body we cannot read. The status is the only signal we have,
    and inventing a failure would be worse."""
    provider = registry.get("s2s_json")
    assert provider.interpret(status_code=202, body="<html>ok</html>").accepted


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(500, True), (503, True), (429, True), (408, True), (400, False), (403, False)],
)
def test_json_adapter_retry_advice(status, retryable):
    provider = registry.get("s2s_json")
    verdict = provider.interpret(status_code=status, body="")
    assert not verdict.accepted
    assert verdict.retryable is retryable


# --- helpers ------------------------------------------------------------
def test_missing_credentials_wording_is_shared():
    """A customer comparing two integrations should not have to work out that
    two different messages mean the same thing."""
    problems = missing_credentials(ProviderConfig(credentials={"a": " "}), ("a", "b"))
    assert problems == ["a is required", "b is required"]


def test_unknown_variables_uses_the_same_allowlist_as_postbacks():
    assert unknown_variables("{{click_id}}") == set()
    assert unknown_variables("{{password}}") == {"password"}


# --- the isolation the framework exists for -----------------------------
def test_no_provider_names_leak_into_the_core():
    """The whole point: adding a network is a new adapter file, not a branch in
    the attribution engine or the delivery loop.

    This test is the framework's only real guarantee. Without it the first
    urgent integration puts `if provider == "meta"` somewhere central, and the
    second one makes that a pattern.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    core_paths = [
        root / "packages" / "mmp_attrib",
        root / "packages" / "mmp_ingest",
        root / "packages" / "mmp_core",
        root / "packages" / "mmp_providers" / "src" / "mmp_providers" / "delivery.py",
    ]
    network_names = (
        "meta",
        "facebook",
        "google_ads",
        "adwords",
        "tiktok",
        "snapchat",
        "applovin",
        "unity_ads",
        "ironsource",
    )

    import re

    # Bounded by letters, not by \b.
    #
    # Two failures got this here. Plain substring matching flagged "meta" inside
    # "metadata" — the cloud metadata endpoint the SSRF guard blocks — and
    # reported the security module as a provider leak. Switching to \b fixed
    # that and broke the other direction: "_" is a word character, so
    # \btiktok\b does not match TIKTOK_SPECIAL_CASE, which is exactly the shape
    # a real leak takes. Looking for the name not adjacent to another letter
    # catches both.
    patterns = {
        name: re.compile(rf"(?<![a-z]){re.escape(name)}(?![a-z])") for name in network_names
    }

    offenders = []
    for path in core_paths:
        files = path.rglob("*.py") if path.is_dir() else [path]
        for file in files:
            lowered = file.read_text().lower()
            for name, pattern in patterns.items():
                if pattern.search(lowered):
                    offenders.append(f"{file.relative_to(root)}: {name}")

    assert not offenders, "a specific network is named outside an adapter:\n  " + "\n  ".join(
        offenders
    )


def test_a_new_adapter_needs_only_one_file():
    """The framework's promise, checked rather than asserted in a comment."""
    from mmp_providers.base import DeliveryVerdict as _V
    from mmp_providers.base import PreparedRequest as _R

    class Minimal:
        name = "minimal"
        display_name = "Minimal"
        capabilities = frozenset({Capability.INSTALLS})
        auth_style = AuthStyle.NONE
        event_map: ClassVar[Mapping[str, str]] = MappingProxyType({"install": "install"})

        def validate(self, config: ProviderConfig) -> list[str]:
            return []

        def prepare(self, *, event_name, context, config):
            return _R(method="GET", url="https://example.com/x")

        def interpret(self, *, status_code, body):
            return _V(accepted=status_code == 200)

    assert isinstance(Minimal(), Provider), (
        "the contract must be satisfiable by a small declarative object"
    )
    assert isinstance(
        Minimal().prepare(event_name="install", context={}, config=None), PreparedRequest
    )
    assert isinstance(Minimal().interpret(status_code=200, body=""), DeliveryVerdict)
