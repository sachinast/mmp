"""Ingest throughput and latency benchmark.

Runs the tracker under uvicorn in a **separate process** and drives it over
loopback TCP. The first version of this script ran the app in-process through
httpx's ASGI transport, which was misleading in a way worth recording: the
client's own JSON encoding and object churn were inside the measurement and
shared an event loop and a heap with the server, so roughly half the reported
"service latency" was the load generator, and every GC pause the client caused
was attributed to the server.

Two processes costs a loopback round trip — about 0.2 ms — and buys a number
that means something.

**What this cannot measure.** It is a Python load generator driving a Python
server on the same cores, and it is the limiting factor, not the server. A
no-op ``/health`` endpoint measured through this harness tops out around 1,300
requests/sec and degrades the same way under concurrency as the ingest endpoint
does — so the throughput figures describe the harness, not the tracker.

It is therefore a **regression gate**, not an SLO check: it compares service
latency at low concurrency against a recorded baseline and fails on a
significant increase. Capacity and the published p99 < 120 ms SLO have to be
measured with ``infra/load/ingest.k6.js`` — a compiled load generator that does
not contend for the GIL — against deployed infrastructure.

    uv run python infra/load/ingest_benchmark.py --requests 1000 --batch 20
    uv run python infra/load/ingest_benchmark.py --update-baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import statistics
import subprocess
import sys
import time
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mmp_core.ids import uuid7
from mmp_crypto.keys import generate_key


async def _seed_link(conn: asyncpg.Connection, org: object, app: object) -> str:
    """A campaign and an active tracking link for the redirect benchmark."""
    campaign, link = uuid7(), uuid7()
    code = "bench" + secrets.token_hex(8)
    await conn.execute(
        """INSERT INTO campaigns (id, organization_id, app_id, name, source, medium, status)
           VALUES ($1, $2, $3, $4, 'bench', 'cpi', 'active')""",
        campaign,
        org,
        app,
        f"Bench {secrets.token_hex(3)}",
    )
    await conn.execute(
        """INSERT INTO tracking_links (id, organization_id, app_id, campaign_id, tracking_code,
                                       name, android_url, ios_url, fallback_url, status)
           VALUES ($1, $2, $3, $4, $5, 'Bench', $6, $7, $8, 'active')""",
        link,
        org,
        app,
        campaign,
        code,
        "https://play.google.com/store/apps/details?id=com.example.bench",
        "https://apps.apple.com/app/id123456789",
        "https://example.com/landing",
    )
    return code


async def _seed(conn: asyncpg.Connection, pepper: str) -> tuple[str, str]:
    org, app, key = uuid7(), uuid7(), uuid7()
    suffix = secrets.token_hex(4)
    await conn.execute(
        "INSERT INTO organizations (id, name, slug, timezone) VALUES ($1, $2, $3, 'UTC')",
        org,
        f"bench-{suffix}",
        f"bench-{suffix}",
    )
    await conn.execute(
        """INSERT INTO apps (id, organization_id, name, platform, android_package_name,
                             status, install_window_days, event_window_days,
                             session_timeout_minutes, timezone)
           VALUES ($1, $2, 'Bench', 'android', 'com.example.bench', 'active',
                   7, 30, 30, 'UTC')""",
        app,
        org,
    )
    generated = generate_key(environment="prod", pepper=pepper)
    await conn.execute(
        """INSERT INTO api_keys (id, organization_id, app_id, name, kind, key_prefix,
                                 key_hash, pepper_version, environment, status)
           VALUES ($1, $2, $3, 'bench', 'sdk', $4, $5, 1, 'prod', 'active')""",
        key,
        org,
        app,
        generated.prefix,
        generated.key_hash,
    )
    return str(app), generated.raw


BASELINE_PATH = Path(__file__).with_name("baseline.json")
# Generous, because a laptop under load is noisy. Tight enough to catch a real
# regression — the kind that comes from adding a synchronous call to the path.
TOLERANCE = 1.6

# Only the medians are gated. The tails are reported but not enforced, because
# on a developer machine they are dominated by garbage collection pauses and by
# CPU contention with the load generator: measured back-to-back with no code
# change, redirect p95 moved from 2.6 ms to 4.7 ms, which would have failed a
# 60% tolerance. A gate that fires on noise is a gate people learn to ignore,
# and then it catches nothing. Tail latency is enforced against the SLO in
# ingest.k6.js, where the measurement is trustworthy enough to enforce.
GATED_METRICS = ("p50_ms", "redirect_p50_ms")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


async def _wait_for_health(url: str, *, timeout: float = 20.0) -> None:
    deadline = time.perf_counter() + timeout
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.perf_counter() < deadline:
            try:
                if (await client.get(f"{url}/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"tracker did not become healthy at {url}")


async def run(
    requests: int, batch: int, concurrency: int, port: int, update_baseline: bool = False
) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.conftest_api import build_api_settings, build_settings_for
    from tests.conftest_db import owner_dsn

    base = build_api_settings()
    settings = build_settings_for("mmp_tracker").model_copy(
        update={"api_key_pepper": base.api_key_pepper, "log_level": "error"}
    )

    conn = await asyncpg.connect(owner_dsn())
    app_id, api_key = await _seed(conn, base.api_key_pepper)
    org_id = await conn.fetchval("SELECT organization_id FROM apps WHERE id = $1::uuid", app_id)
    tracking_code = await _seed_link(conn, org_id, app_id)

    environment = {
        **os.environ,
        "MMP_ENVIRONMENT": "dev",
        "MMP_DATABASE_URL": str(settings.database_url),
        "MMP_REDIS_URL": str(settings.redis_url),
        "MMP_API_KEY_PEPPER": settings.api_key_pepper,
        "MMP_IP_HASH_PEPPER": settings.ip_hash_pepper,
        "MMP_SESSION_SECRET": settings.session_secret,
        "MMP_LOG_LEVEL": "error",
        "MMP_SHUTDOWN_GRACE_SECONDS": "0",
    }
    server = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mmp_tracker.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
            "--no-access-log",
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    base_url = f"http://127.0.0.1:{port}"
    latencies: list[float] = []
    accepted = 0

    try:
        await _wait_for_health(base_url)
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            limits=httpx.Limits(max_connections=concurrency + 8),
        ) as client:
            client.headers["authorization"] = f"Bearer {api_key}"
            semaphore = asyncio.Semaphore(concurrency)

            async def one() -> None:
                nonlocal accepted
                payload = {
                    "events": [
                        {
                            "event_id": str(uuid7()),
                            "event_name": "app_open",
                            "anonymous_id": f"device-{secrets.token_hex(4)}",
                            "platform": "android",
                            "app_version": "1.4.2",
                        }
                        for _ in range(batch)
                    ]
                }
                async with semaphore:
                    started = time.perf_counter()
                    response = await client.post("/v1/events", json=payload)
                    latencies.append((time.perf_counter() - started) * 1000)
                    accepted += response.json().get("accepted", 0)

            # Warm up properly before measuring. The first requests pay costs
            # that happen once per process, not once per request: the API-key
            # cache miss, and the lazy creation of each Redis and Postgres
            # connection in the pools. Including them reports a one-time startup
            # cost as if it were a recurring tail.
            await asyncio.gather(*(one() for _ in range(40)))
            latencies.clear()
            accepted = 0

            # Two phases, because one number cannot answer both questions.
            #
            # Latency is measured at low concurrency, where the figure is actual
            # service time. Measuring it at saturation reports queue depth
            # instead: with N requests in flight against a single event loop,
            # Little's law makes p50 approximately N / throughput regardless of
            # how fast the handler is. That is a capacity number wearing a
            # latency number's clothes.
            semaphore = asyncio.Semaphore(4)
            latency_requests = min(requests, 300)
            await asyncio.gather(*(one() for _ in range(latency_requests)))
            latencies_service = list(latencies)

            # The redirect, measured separately. It is the one endpoint whose
            # latency is visible to an advertiser's *customers* rather than to
            # the advertiser, and the one where slowness costs conversions
            # directly — a person waiting on a store page leaves.
            redirect_latencies: list[float] = []

            async def one_redirect() -> None:
                async with semaphore:
                    started = time.perf_counter()
                    await client.get(
                        f"/c/{tracking_code}",
                        headers={
                            "user-agent": (
                                "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                                "AppleWebKit/537.36 Chrome/120 Mobile"
                            )
                        },
                        follow_redirects=False,
                    )
                    redirect_latencies.append((time.perf_counter() - started) * 1000)

            await asyncio.gather(*(one_redirect() for _ in range(40)))  # warm
            redirect_latencies.clear()
            await asyncio.gather(*(one_redirect() for _ in range(latency_requests)))

            # Throughput is measured at saturation, where it is meaningful.
            latencies.clear()
            accepted = 0
            semaphore = asyncio.Semaphore(concurrency)
            wall_start = time.perf_counter()
            await asyncio.gather(*(one() for _ in range(requests)))
            elapsed = time.perf_counter() - wall_start

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    await conn.execute("DELETE FROM events WHERE app_id = $1::uuid", app_id)
    await conn.execute(
        "DELETE FROM organizations WHERE id = (SELECT organization_id FROM apps "
        "WHERE id = $1::uuid)",
        app_id,
    )
    await conn.close()

    measured = {
        "p50_ms": round(statistics.median(latencies_service), 2),
        "p95_ms": round(_percentile(latencies_service, 0.95), 2),
        "p99_ms": round(_percentile(latencies_service, 0.99), 2),
        "redirect_p50_ms": round(statistics.median(redirect_latencies), 2),
        "redirect_p95_ms": round(_percentile(redirect_latencies, 0.95), 2),
        "events_per_second": round(accepted / elapsed),
    }

    print(f"batch size        {batch} events per request")
    print()
    print(f"-- service latency over loopback (concurrency 4, {len(latencies_service)} requests)")
    print(f"   p50            {measured['p50_ms']:.2f} ms")
    print(f"   p95            {measured['p95_ms']:.2f} ms  (reported, not gated)")
    print(f"   p99            {measured['p99_ms']:.2f} ms  (reported, not gated)")
    print()
    print(f"-- redirect latency over loopback (concurrency 4, {len(redirect_latencies)})")
    print(f"   p50            {measured['redirect_p50_ms']:.2f} ms")
    print(f"   p95            {measured['redirect_p95_ms']:.2f} ms  (reported, not gated)")
    print()
    print(f"-- harness-limited throughput (concurrency {concurrency}, {requests} requests)")
    print(f"   events/sec     {measured['events_per_second']:,}")
    print(f"   requests/sec   {requests / elapsed:,.0f}")
    print("   NOTE: bounded by this Python load generator, not by the tracker.")
    print("         Use infra/load/ingest.k6.js for real capacity figures.")

    if update_baseline:
        BASELINE_PATH.write_text(json.dumps(measured, indent=2) + "\n")
        print(f"\nBaseline written to {BASELINE_PATH.relative_to(Path.cwd())}")
        return 0

    if not BASELINE_PATH.exists():
        print("\nNo baseline recorded. Run with --update-baseline to create one.")
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    regressions = [
        f"{metric}: {measured[metric]:.2f} ms vs baseline {baseline[metric]:.2f} ms"
        for metric in GATED_METRICS
        if metric in baseline and measured[metric] > baseline[metric] * TOLERANCE
    ]
    if regressions:
        print(f"\nFAIL: latency regressed beyond {TOLERANCE:.0%} of baseline")
        for line in regressions:
            print(f"  {line}")
        return 1
    print(
        f"\nPASS: medians within {TOLERANCE:.0%} of baseline "
        f"(ingest p50 {baseline['p50_ms']:.2f} ms, "
        f"redirect p50 {baseline['redirect_p50_ms']:.2f} ms)"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--port", type=int, default=8931)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record the measured latencies as the new regression baseline",
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            run(
                args.requests,
                args.batch,
                args.concurrency,
                args.port,
                args.update_baseline,
            )
        )
    )


if __name__ == "__main__":
    main()
