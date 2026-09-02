"""OPS-14: is_running telemetry adapter for the HindsightEmbedded daemon probe.

Installs a behavior-equivalent replacement for DaemonEmbedManager.is_running()
on a manager instance. The replacement performs the same httpx /health probe
and records structured telemetry on every failure without altering the boolean
return contract or any upstream restart logic.
"""
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_ADAPTER_MODULE = __name__


def _make_probe(manager):
    def is_running(profile):
        url = manager.get_url(profile)
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=2) as client:
                resp = client.get(f"{url}/health")
            elapsed = time.monotonic() - t0
            if resp.status_code == 200:
                return True
            logger.warning(
                "hindsight is_running probe failed: elapsed_seconds=%r http_status=%d",
                elapsed,
                resp.status_code,
                extra={
                    "event": "is_running_probe_failed",
                    "elapsed_seconds": elapsed,
                    "http_status": resp.status_code,
                },
            )
            return False
        except Exception as exc:
            elapsed = time.monotonic() - t0
            exc_mod = type(exc).__module__
            if exc_mod == _ADAPTER_MODULE:
                exc_class = type(exc).__qualname__
            else:
                exc_class = f"{exc_mod}.{type(exc).__qualname__}"
            logger.warning(
                "hindsight is_running probe failed: elapsed_seconds=%r exception_class=%s",
                elapsed,
                exc_class,
                extra={
                    "event": "is_running_probe_failed",
                    "elapsed_seconds": elapsed,
                    "exception_class": exc_class,
                },
            )
            return False

    return is_running


def install(manager):
    """Replace *manager*.is_running with the telemetry adapter."""
    manager.is_running = _make_probe(manager)
