"""OPS-14: is_running telemetry adapter for the HindsightEmbedded daemon probe.

Installs a behavior-equivalent replacement for DaemonEmbedManager.is_running()
on a manager instance. The replacement delegates to health_contract.classify(),
which implements the full healthy/degraded/dead probe contract, the
per-profile restart circuit breaker, and one-event-per-sequence telemetry.

While upstream's own per-profile startup lock is held, the replacement falls
back to a single lightweight probe identical to the original upstream
is_running(), so the internal startup poll loop is neither slowed down nor
logged as a health decision.
"""
from __future__ import annotations

from . import health_contract


def install(manager) -> None:
    """Replace *manager*.is_running with the OPS-14 health contract adapter."""
    manager.is_running = health_contract.make_is_running(manager)
