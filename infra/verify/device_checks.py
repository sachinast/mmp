"""Verify a deployed tracker against the traffic real devices actually send.

The unit tests cover this logic in-process. What they cannot cover is a
*deployment*: a TLS terminator that strips a header, a proxy that rewrites a
path, a load balancer that answers a request the tracker never sees. This script
sends the real shapes at a real host and says what came back.

The SKAdNetwork postbacks below are Apple's own, published with the signatures
their private key produced. That makes this a genuine end-to-end check of
signature verification against a deployed environment — if the deployment
mangles the request body in transit, the signature stops verifying and this
says so.

    uv run python infra/verify/device_checks.py https://track.example.com \\
        --api-key pk_live_... --apple-app-id 525463029

Nothing here writes to your production data beyond what a real device would:
the postbacks carry Apple's own transaction ids, so they are stored once and
deduplicated forever after. Run against staging first.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

# Imported rather than duplicated, so a change to the fixtures cannot leave this
# script checking something the test suite no longer does.
sys.path.insert(0, "tests")

POSTBACK_PATH = "/.well-known/skadnetwork/report-attribution"

MARK_OK = "  pass"
MARK_BAD = "  FAIL"


def _post(url: str, body: dict[str, Any], *, api_key: str | None = None) -> tuple[int, str]:
    # The URL comes from an operator's command line, but it is still checked:
    # urllib will happily open file:// and this script sends an API key.
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing a non-http(s) target: {url!r}")
    request = urllib.request.Request(  # noqa: S310 — scheme checked immediately above
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    if api_key:
        request.add_header("authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except OSError as exc:
        return 0, str(exc)


def check_skan(base: str) -> list[str]:
    """Apple's real postbacks, end to end through whatever sits in front."""
    from test_skadnetwork import V3_LOSER, V3_WINNER, V4_WEB_HIGH

    failures: list[str] = []
    url = base.rstrip("/") + POSTBACK_PATH

    print("SKAdNetwork postbacks (Apple's published vectors)")
    for name, postback in (
        ("4.0 winner", V4_WEB_HIGH),
        ("3.0 winner", V3_WINNER),
        ("3.0 non-winner", V3_LOSER),
    ):
        status, _ = _post(url, postback)
        ok = status == 200
        print(f"{MARK_OK if ok else MARK_BAD}  {name}: HTTP {status}")
        if not ok:
            failures.append(
                f"{name} returned {status}; a genuine Apple postback must be answered 200 "
                f"or the device retries for nine days"
            )

    # The check that matters most, and the one only a deployment can fail: a
    # body altered in transit must stop verifying.
    tampered = dict(V4_WEB_HIGH)
    tampered["app-id"] = 999999999
    status, _ = _post(url, tampered)
    ok = status == 400
    print(f"{MARK_OK if ok else MARK_BAD}  tampered postback refused: HTTP {status}")
    if not ok:
        failures.append(
            f"a tampered postback returned {status} rather than 400 — signature "
            f"verification is not reaching this deployment"
        )

    # An unauthenticated caller must not be able to choose how much we read.
    status, _ = _post(url, {"padding": "x" * 20_000})
    ok = status in (400, 413)
    print(f"{MARK_OK if ok else MARK_BAD}  oversized body refused: HTTP {status}")
    if not ok:
        failures.append(f"an oversized body returned {status}")

    return failures


def check_deferred_deep_link(base: str, api_key: str) -> list[str]:
    """The first-launch handshake, including that it stays silent about
    devices it does not know."""
    print("\nDeferred deep link")
    url = base.rstrip("/") + "/v1/deeplink/resolve"

    status, body = _post(url, {"anonymous_id": "verify-unknown-device"}, api_key=api_key)
    ok = status == 200 and json.loads(body or "{}").get("matched") is False
    print(
        f"{MARK_OK if ok else MARK_BAD}  unknown device answered without confirming: HTTP {status}"
    )
    failures = []
    if not ok:
        failures.append(f"unknown-device resolve returned {status} {body[:120]}")

    status, _ = _post(url, {"anonymous_id": "verify-unknown-device"})
    ok = status == 401
    print(f"{MARK_OK if ok else MARK_BAD}  refused without a key: HTTP {status}")
    if not ok:
        failures.append(f"resolve without a key returned {status}, expected 401")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="e.g. https://track.example.com")
    parser.add_argument("--api-key", help="an app's ingest key, for the SDK endpoints")
    args = parser.parse_args()

    if not args.base_url.startswith("https://") and "localhost" not in args.base_url:
        print("refusing to send an api key over plain http to a remote host")
        return 2

    failures = check_skan(args.base_url)
    if args.api_key:
        failures += check_deferred_deep_link(args.base_url, args.api_key)
    else:
        print("\nDeferred deep link: skipped (no --api-key)")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
