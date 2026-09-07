"""Liveness and readiness.

The distinction matters operationally and is routinely collapsed:

* ``/health`` answers "is this process alive". It touches nothing external, so a
  Redis outage does not cause the orchestrator to kill every healthy pod.
* ``/ready`` answers "should this instance receive traffic". It runs the
  registered dependency probes, and fails the instance out of the load balancer
  while leaving it running.

A draining instance reports not-ready immediately but keeps serving in-flight
requests, which is what makes a rolling deploy lossless.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import JSONResponse

Probe = Callable[[], Awaitable[None]]


@dataclass
class HealthRegistry:
    service: str
    version: str
    probes: dict[str, Probe] = field(default_factory=dict)
    draining: bool = False

    def register(self, name: str, probe: Probe) -> None:
        self.probes[name] = probe

    def start_draining(self) -> None:
        self.draining = True

    async def check(self, *, probe_timeout: float = 2.0) -> tuple[bool, dict[str, str]]:
        # Per-probe rather than per-call: one slow dependency should be reported
        # as slow, not mask the status of everything checked alongside it.
        results: dict[str, str] = {}
        healthy = True

        async def run(name: str, probe: Probe) -> None:
            nonlocal healthy
            try:
                await asyncio.wait_for(probe(), timeout=probe_timeout)
                results[name] = "ok"
            except TimeoutError:
                results[name] = "timeout"
                healthy = False
            except Exception as exc:
                results[name] = f"error: {type(exc).__name__}"
                healthy = False

        async with asyncio.TaskGroup() as tg:
            for name, probe in self.probes.items():
                tg.create_task(run(name, probe))
        return healthy, results


def health_routes(
    registry: HealthRegistry,
) -> list[tuple[str, Callable[[Request], Awaitable[JSONResponse]]]]:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "service": registry.service, "version": registry.version}
        )

    async def ready(_request: Request) -> JSONResponse:
        if registry.draining:
            return JSONResponse({"status": "draining"}, status_code=503)
        healthy, checks = await registry.check()
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "checks": checks},
            status_code=200 if healthy else 503,
        )

    return [("/health", health), ("/ready", ready)]
