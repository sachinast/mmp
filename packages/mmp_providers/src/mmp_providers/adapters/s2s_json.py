"""A JSON server-to-server provider.

The second common shape, and the reason the framework earns its keep: this is
not a URL template with different macros. It POSTs a JSON body, authenticates
with a bearer token, translates our event names into the provider's own
vocabulary, and reads the response body to decide whether the conversion was
actually accepted — because a provider returning ``200`` with
``{"error": "unknown campaign"}`` is a rejection wearing a success code, and an
advertiser can spend weeks believing conversions are arriving.

Deliberately **not named after a real network.** The large networks each have
their own required fields, hashing rules and versioned endpoints, and an adapter
claiming to be one of them without having been tested against it would be worse
than none — someone would configure it and believe it worked. This is the shape;
a real integration is a copy of this file with that network's specifics and a
test against their sandbox.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from mmp_providers.base import (
    AuthStyle,
    Capability,
    DeliveryVerdict,
    PreparedRequest,
    ProviderConfig,
    missing_credentials,
)
from mmp_providers.registry import register
from mmp_providers.templates import render

REQUIRED_CREDENTIALS = ("api_token",)

# Our vocabulary to a conventional one. Anything absent is not sent: an event
# posted under a name the provider has never heard of is discarded silently at
# their end, which looks identical to a delivery problem at ours.
DEFAULT_EVENT_MAP = {
    "install": "app_install",
    "signup": "complete_registration",
    "login": "login",
    "purchase": "purchase",
    "subscription_start": "start_trial",
    "subscription_renew": "subscribe",
    "refund": "refund",
}


class S2SJsonProvider:
    name = "s2s_json"
    display_name = "Server-to-server (JSON)"
    capabilities = frozenset(
        {
            Capability.INSTALLS,
            Capability.IN_APP_EVENTS,
            Capability.REVENUE,
            Capability.SUBSCRIPTIONS,
            Capability.REFUNDS,
            Capability.REQUIRES_ATTRIBUTION,
        }
    )
    auth_style = AuthStyle.BEARER
    event_map: ClassVar[Mapping[str, str]] = MappingProxyType(dict(DEFAULT_EVENT_MAP))

    def validate(self, config: ProviderConfig) -> list[str]:
        problems = missing_credentials(config, REQUIRED_CREDENTIALS)

        endpoint = str(config.settings.get("endpoint", "")).strip()
        if not endpoint:
            problems.append("endpoint is required")
        elif not endpoint.startswith("https://"):
            problems.append("endpoint must be an https URL")

        overrides = config.settings.get("event_map") or {}
        if not isinstance(overrides, dict):
            problems.append("event_map must be an object")
        else:
            # An override for an event we never emit is a typo that would
            # otherwise sit silently in a configuration for months.
            from mmp_ingest.schema import SYSTEM_EVENTS

            unknown = {
                name
                for name in overrides
                if name not in SYSTEM_EVENTS and not name.startswith("custom_")
            }
            if unknown:
                problems.append(
                    "event_map refers to events this platform does not emit: "
                    + ", ".join(sorted(unknown))
                )
        return problems

    def _resolved_map(self, config: ProviderConfig) -> dict[str, str]:
        """A configured map **replaces** the default; it does not merge into it.

        The map is a filter as well as a translation — an event that is not in
        it is not sent — and a filter you can only add to is not a filter. With
        merge semantics an integration could rename an event but never stop
        sending one, so "only send installs and purchases" would be
        inexpressible, which is a thing advertisers ask for constantly.

        The cost is that renaming one event means listing them all. That is
        visible in a configuration and easy to get right; silently sending an
        event someone tried to exclude is neither.
        """
        configured = config.settings.get("event_map")
        return dict(configured) if configured else dict(self.event_map)

    def prepare(
        self, *, event_name: str, context: dict[str, Any], config: ProviderConfig
    ) -> PreparedRequest | None:
        provider_event = self._resolved_map(config).get(event_name)
        if provider_event is None:
            # Not an error. An event this integration was not configured to send
            # is skipped quietly rather than attempted and failed.
            return None

        endpoint = str(config.settings.get("endpoint", ""))
        if not endpoint:
            return None

        payload: dict[str, Any] = {
            "event_name": provider_event,
            "event_time": context.get("event_timestamp"),
            "click_id": context.get("click_id"),
            "campaign_id": context.get("campaign_id"),
            "attribution_method": context.get("attribution_method"),
        }
        if context.get("revenue") is not None:
            payload["value"] = context["revenue"]
            payload["currency"] = context.get("currency")

        # Identifiers are included only when the platform exposed them, which
        # already reflects the device's consent — a denied purpose means the
        # field is absent from the context, not blanked here.
        for key in ("user_id", "anonymous_id"):
            if context.get(key):
                payload[key] = context[key]

        # Optional extra fields, rendered through the same allowlist as any
        # postback template. An adapter is not a way around it.
        for name, template in (config.settings.get("extra_fields") or {}).items():
            payload[name] = render(str(template), context, encode=False)

        return PreparedRequest(
            method="POST",
            url=endpoint,
            headers={
                "authorization": f"Bearer {config.credentials['api_token']}",
                "content-type": "application/json",
            },
            body=json.dumps(payload, separators=(",", ":")).encode(),
            success_codes=(200, 201, 202),
        )

    def interpret(self, *, status_code: int, body: str) -> DeliveryVerdict:
        """Read the body, not just the code.

        The reason this adapter exists as a separate shape: a 200 carrying an
        error is a rejection, and recording it as delivered is how an advertiser
        spends a month believing conversions are arriving.
        """
        if status_code >= 500 or status_code in (408, 429):
            return DeliveryVerdict(accepted=False, detail=body[:200] or None, retryable=True)
        if status_code >= 400:
            return DeliveryVerdict(accepted=False, detail=body[:200] or None, retryable=False)

        try:
            parsed = json.loads(body) if body.strip() else {}
        except ValueError:
            # A 2xx with an unparseable body. Accepted, because the status is
            # the only signal we have and inventing a failure would be worse.
            return DeliveryVerdict(accepted=True)

        if isinstance(parsed, dict) and (error := parsed.get("error")):
            return DeliveryVerdict(
                accepted=False,
                detail=f"provider rejected: {str(error)[:180]}",
                # The provider understood and refused. Sending it again gets the
                # same answer.
                retryable=False,
            )
        return DeliveryVerdict(accepted=True)


register(S2SJsonProvider())
