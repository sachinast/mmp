"""The generic URL-template provider.

The first adapter, and the one every other is measured against: if a shape can
be expressed here, it does not need its own file. Most affiliate networks and a
surprising number of large ones are a GET with macros in the query string, which
is exactly what a postback rule already is.

This adapter formalises that rather than replacing it. The value it adds over a
bare rule is the declared capability set and the event map — a rule with an
unmapped trigger event fails at save time instead of delivering a conversion the
network silently discards.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from mmp_providers.base import (
    AuthStyle,
    Capability,
    DeliveryVerdict,
    PreparedRequest,
    ProviderConfig,
    unknown_variables,
)
from mmp_providers.registry import register
from mmp_providers.templates import render


class CustomProvider:
    name = "custom"
    display_name = "Custom postback"
    capabilities = frozenset(
        {
            Capability.INSTALLS,
            Capability.IN_APP_EVENTS,
            Capability.REVENUE,
            Capability.SUBSCRIPTIONS,
            Capability.REFUNDS,
        }
    )
    auth_style = AuthStyle.NONE
    # Identity: a custom integration uses our vocabulary because there is no
    # other party's to translate into.
    event_map: ClassVar[Mapping[str, str]] = MappingProxyType({})

    def validate(self, config: ProviderConfig) -> list[str]:
        problems: list[str] = []
        template = str(config.settings.get("url_template", "")).strip()
        if not template:
            problems.append("url_template is required")
            return problems
        if not template.startswith("https://"):
            problems.append("url_template must be an https URL")

        unknown = unknown_variables(template)
        if unknown:
            problems.append(
                "url_template references variables the platform does not expose: "
                + ", ".join(sorted(unknown))
            )
        return problems

    def prepare(
        self, *, event_name: str, context: dict[str, Any], config: ProviderConfig
    ) -> PreparedRequest | None:
        template = str(config.settings.get("url_template", ""))
        if not template:
            return None

        # An empty event_map means "accept everything under our own names".
        # A populated one is a filter as well as a translation: an event that is
        # not in it is not something this integration was configured to send.
        if self.event_map and event_name not in self.event_map:
            return None

        return PreparedRequest(
            method=str(config.settings.get("method", "GET")).upper(),
            url=render(template, context),
            success_codes=tuple(config.settings.get("success_codes", (200, 201, 202, 204))),
        )

    def interpret(self, *, status_code: int, body: str) -> DeliveryVerdict:
        # No body inspection: a custom endpoint has no contract we know about,
        # and guessing at one would produce confident wrong answers.
        accepted = 200 <= status_code < 300
        return DeliveryVerdict(
            accepted=accepted,
            detail=None if accepted else body[:200] or None,
            retryable=status_code >= 500 or status_code in (408, 429),
        )


register(CustomProvider())
