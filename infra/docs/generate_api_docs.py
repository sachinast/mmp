"""Regenerate the API reference from the running service.

Hand-maintained endpoint lists drift, and a stale API document is worse than no
document because it is trusted. This reads the real FastAPI schema, so the
reference cannot disagree with the service it documents.

    uv run python infra/docs/generate_api_docs.py
"""

from __future__ import annotations

import collections
import json
import pathlib

OUT = pathlib.Path("docs/api")

# The tracker is Starlette and publishes no OpenAPI schema, so its endpoints are
# listed here by hand. It is a small, deliberately stable surface — but it is
# the one part of this file that can go stale, so a test asserts these paths
# exist on the real app (tests/test_sdk_contract.py).
TRACKER = [
    ("POST", "/v1/events", "Ingest events from an SDK", "App key (Bearer)"),
    ("POST", "/v1/s2s/events", "Ingest events server-to-server", "App key + HMAC signature"),
    ("GET", "/c/{tracking_code}", "Click redirect", "None (public)"),
    ("POST", "/v1/deeplink/resolve", "Deferred deep link handshake", "App key (Bearer)"),
    (
        "GET",
        "/v1/skan/conversion-values",
        "Conversion value mapping for the SDK",
        "App key (Bearer)",
    ),
    (
        "POST",
        "/.well-known/skadnetwork/report-attribution",
        "Apple SKAdNetwork postback",
        "Apple's ECDSA signature",
    ),
]

TITLES = {
    "auth": "Authentication",
    "organizations": "Organisations & members",
    "apps": "Apps",
    "api-keys": "API keys",
    "campaigns": "Campaigns & tracking links",
    "attribution": "Attribution",
    "analytics": "Analytics",
    "postbacks": "Postbacks",
    "webhooks": "Webhooks",
    "integrations": "Provider integrations",
    "privacy": "Privacy",
    "fraud": "Fraud",
    "deeplinks": "Deep links",
    "exports": "Data export",
    "skadnetwork": "SKAdNetwork",
    "ops": "Operations",
}
ORDER = list(TITLES)


def main() -> None:
    from mmp_api.app import create_app

    schema = create_app().openapi()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "openapi.json").write_text(json.dumps(schema, indent=2) + "\n")

    groups: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            tag = (operation.get("tags") or ["other"])[0]
            summary = (
                operation.get("summary")
                or ((operation.get("description") or "").strip().split("\n")[0])
            )
            groups[tag].append((method.upper(), path, summary))

    total = sum(len(v) for v in groups.values())
    print(f"{len(schema['paths'])} paths, {total} operations -> {OUT}/openapi.json")
    print("Reference prose in docs/api/README.md is hand-written around this data;")
    print("update it if the grouping changed.")


if __name__ == "__main__":
    main()
