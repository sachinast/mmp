"""The provider adapter contract.

An MMP's long-term cost is its integrations. Meta wants a JSON body with hashed
identifiers; an affiliate network wants a GET with macros in the query string;
another wants a bearer token, a different event vocabulary and its own idea of
what counts as success. The failure mode is predictable and expensive: each of
those requirements lands as a branch somewhere in the delivery path, and within
a year the core cannot be changed without reasoning about six networks.

So the contract here is narrow on purpose. An adapter declares **what it can
receive**, **how our vocabulary maps to its own**, and **how to build one
request**. Everything else — claiming a delivery, retrying, backing off,
recording the outcome, refusing to reach an internal address — belongs to the
delivery engine and is the same for every provider. An adapter cannot change
those, and that is the point: a network's quirk must not be able to weaken a
control that exists for all of them.

Concretely, an adapter may not:

* send the request itself (so the SSRF guard cannot be bypassed),
* decide whether a delivery is retried (so one network cannot cause a retry
  storm against another's outage),
* read anything from the database (so an adapter cannot become a query),
* see any value the template allowlist does not expose.

Adding a network should be one new file and one registry line. If it ever needs
more than that, the contract is wrong and should change here rather than being
worked around there.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from mmp_providers.templates import ALLOWED_VARIABLES


class Capability(enum.StrEnum):
    """What a provider is able to receive.

    Declared rather than assumed, so the dashboard can refuse to configure a
    rule the provider will reject — a validation error at save time instead of a
    delivery failure discovered in a log a week later.
    """

    INSTALLS = "installs"
    IN_APP_EVENTS = "in_app_events"
    REVENUE = "revenue"
    SUBSCRIPTIONS = "subscriptions"
    REFUNDS = "refunds"
    # Some networks accept a conversion only if it can be tied to their click.
    REQUIRES_ATTRIBUTION = "requires_attribution"
    # And some can take a batch, which changes the shape of delivery entirely.
    BATCHING = "batching"


class AuthStyle(enum.StrEnum):
    NONE = "none"
    BEARER = "bearer"
    HEADER = "header"
    QUERY_PARAM = "query_param"


@dataclass(frozen=True)
class PreparedRequest:
    """One outbound request, described but not sent.

    An adapter returns this; the delivery engine sends it. That separation is
    what keeps the SSRF guard, the retry policy and the delivery claim outside
    an adapter's reach.
    """

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    # Which HTTP statuses this provider means by "accepted". Some return 200
    # with an error in the body; an adapter that needs that should say so here
    # and validate in `interpret`.
    success_codes: tuple[int, ...] = (200, 201, 202, 204)


@dataclass(frozen=True)
class DeliveryVerdict:
    """An adapter's reading of a response.

    Exists because "HTTP 200" and "the network accepted this conversion" are not
    the same thing often enough to matter. A provider that returns 200 with
    ``{"error": "unknown campaign"}`` would otherwise be recorded as delivered,
    and the advertiser would spend weeks believing conversions were arriving.
    """

    accepted: bool
    detail: str | None = None
    # False when the provider has told us this will never work — a rejected
    # campaign id, an unmapped event. The engine still owns *whether* to retry;
    # this only reports what the provider said.
    retryable: bool = True


@dataclass(frozen=True)
class ProviderConfig:
    """Per-integration settings, after decryption.

    Credentials arrive already unsealed. An adapter never touches the envelope,
    never sees a key, and cannot reach the store.
    """

    credentials: dict[str, str] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderField:
    """One thing an adapter needs configured, declared so a form can ask for it.

    The adapter already knows what it requires — ``validate()`` refuses without
    it. What it could not do was *say so* in advance, so anything building a
    form had to hardcode the field names, putting adapter knowledge in a second
    place that drifts.

    ``secret`` decides where the value goes: secrets are envelope-encrypted as
    credentials and never returned, everything else is plain configuration. A
    test asserts every declared-required field is one ``validate()`` actually
    rejects the absence of, so the declaration cannot drift from the behaviour.
    """

    name: str
    label: str
    secret: bool = False
    required: bool = True
    hint: str = ""


@runtime_checkable
class Provider(Protocol):
    """What every adapter implements.

    A Protocol rather than a base class: an adapter is a small declarative
    object, and inheritance would invite the shared behaviour this contract
    exists to keep out of adapters.
    """

    name: str
    display_name: str
    capabilities: frozenset[Capability]
    auth_style: AuthStyle
    # Our event vocabulary to theirs. Anything unmapped is not sent, rather than
    # sent under our name and silently ignored by a provider that has never
    # heard of it.
    # ClassVar and Mapping, both deliberately. The map belongs to the adapter
    # class, not to an instance, and it is shared by every integration using
    # that adapter — so a mutation would leak across tenants. Read-only typing
    # says that is not a supported thing to do; the MappingProxyType in each
    # adapter enforces it at runtime.
    event_map: ClassVar[Mapping[str, str]]
    # What this adapter needs configured, so a form can be rendered from the
    # adapter rather than from a second copy of its requirements.
    fields: ClassVar[tuple[ProviderField, ...]]

    def validate(self, config: ProviderConfig) -> list[str]:
        """Return the reasons this configuration cannot work, if any.

        Called when an integration is saved. Returning a list rather than
        raising lets the dashboard show every problem at once instead of one per
        submission.
        """
        ...

    def prepare(
        self, *, event_name: str, context: dict[str, Any], config: ProviderConfig
    ) -> PreparedRequest | None:
        """Describe the request for one conversion, or None to send nothing.

        None is a normal answer: an event this provider does not accept should
        be skipped quietly, not attempted and failed.
        """
        ...

    def interpret(self, *, status_code: int, body: str) -> DeliveryVerdict:
        """Read the provider's response."""
        ...


def maps_event(provider: Provider, event_name: str) -> str | None:
    """The provider's name for one of our events, if it accepts it at all."""
    return provider.event_map.get(event_name)


def missing_credentials(config: ProviderConfig, required: tuple[str, ...]) -> list[str]:
    """Shared validation helper.

    Named here rather than repeated in each adapter so the error wording is the
    same everywhere — a customer comparing two integrations should not have to
    work out that two different messages mean the same thing.
    """
    return [
        f"{name} is required"
        for name in required
        if not (config.credentials.get(name) or "").strip()
    ]


def unknown_variables(template: str) -> set[str]:
    """Template variables an adapter references that the platform does not
    expose. Every value a provider can see passes the same allowlist as a
    postback rule — an adapter is not a way around it."""
    from mmp_providers.templates import PLACEHOLDER

    return {match.group(1) for match in PLACEHOLDER.finditer(template)} - ALLOWED_VARIABLES
