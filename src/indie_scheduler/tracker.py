"""Tracker library for power users who keep their own scheduling.

Usage:

    from indie_scheduler.tracker import tracked

    @tracked(
        name="my_job",
        tool="my-tool",
        cron="0 9 * * mon-fri",          # display only; framework never fires it
        timezone="America/New_York",
        url="https://overmind.indie.io/marketing/my-tool/",
    )
    def my_job():
        ...

The decorator:
  - Registers the job (lazily, on first invocation) with the central scheduler
  - Heartbeats per invocation, in a background daemon thread (zero added latency)
  - Captures status + duration_ms + error message
  - Never throws — if telemetry fails, the wrapped function still runs and its
    return/exception behavior is identical to unwrapped

Configuration via env on the tool's side:
  SCHEDULER_REGISTRY_URL       base URL of the team's scheduler (e.g.
                               https://overmind.indie.io/marketing/scheduler)
  SCHEDULER_HEARTBEAT_SECRET   shared secret; must match the scheduler's
                               SCHEDULER_WEBHOOK_SECRET. If unset, the tracker
                               silently no-ops — the wrapped job still runs.

Both sync and async functions are supported.
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
import time
from datetime import datetime
from datetime import timezone as _tz
from typing import Any, Callable, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore


TRACKER_VERSION = "1.0.0"

_registered: set[str] = set()
_register_lock = threading.Lock()


def _config() -> tuple[Optional[str], Optional[str]]:
    url = os.environ.get("SCHEDULER_REGISTRY_URL", "").strip().rstrip("/")
    secret = os.environ.get("SCHEDULER_HEARTBEAT_SECRET", "").strip()
    if not url or not secret or httpx is None:
        return None, None
    return url, secret


def _post_in_background(path: str, payload: dict, timeout: float = 5.0) -> None:
    """Fire-and-forget POST in a daemon thread. Never raises."""
    base, secret = _config()
    if base is None or secret is None:
        return

    def _send() -> None:
        try:
            httpx.post(
                f"{base}{path}",
                headers={
                    "X-Scheduler-Secret": secret,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
        except Exception:
            pass  # never let telemetry crash the host process

    threading.Thread(target=_send, daemon=True, name=f"tracker-{path}").start()


def _register_if_needed(config: dict) -> None:
    name = config["name"]
    with _register_lock:
        if name in _registered:
            return
        _registered.add(name)
    _post_in_background(
        f"/api/register/{name}",
        {**config, "tracker_version": TRACKER_VERSION},
    )


def _heartbeat(name: str, status: str, duration_ms: int, error_message: Optional[str], started: datetime, ended: datetime) -> None:
    _post_in_background(
        f"/api/heartbeat/{name}",
        {
            "status": status,
            "duration_ms": duration_ms,
            "start_ts": started.isoformat(timespec="seconds"),
            "end_ts": ended.isoformat(timespec="seconds"),
            "error_message": error_message,
            "tracker_version": TRACKER_VERSION,
        },
    )


def tracked(
    *,
    name: str,
    tool: str,
    cron: Optional[str] = None,
    timezone: Optional[str] = None,
    url: Optional[str] = None,
    owner: Optional[str] = None,
    description: Optional[str] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator. Wraps a function so each invocation heartbeats to the central
    scheduler. Construction is pure — no network at decoration time. First
    network call happens on the first wrapped invocation.

    The cron field is metadata only; framework never fires it and never alerts
    off a missed fire derived from it. Missed-fire alerting requires using the
    framework's `trigger: "cron"` mode where the framework owns the schedule.
    """
    config = {
        "name": name,
        "tool": tool,
        "cron": cron,
        "timezone": timezone,
        "url": url,
        "owner": owner,
        "description": description,
    }

    def _emit(status: str, t0: float, started: datetime, exc: Optional[BaseException]) -> None:
        try:
            _heartbeat(
                name,
                status,
                int((time.perf_counter() - t0) * 1000),
                f"{type(exc).__name__}: {exc}"[:1000] if exc else None,
                started,
                datetime.now(_tz.utc),
            )
        except Exception:
            pass

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    _register_if_needed(config)
                except Exception:
                    pass
                started = datetime.now(_tz.utc)
                t0 = time.perf_counter()
                try:
                    result = await fn(*args, **kwargs)
                except BaseException as e:
                    _emit("error", t0, started, e)
                    raise
                _emit("success", t0, started, None)
                return result

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                _register_if_needed(config)
            except Exception:
                pass
            started = datetime.now(_tz.utc)
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                _emit("error", t0, started, e)
                raise
            _emit("success", t0, started, None)
            return result

        return sync_wrapper

    return decorator


__all__ = ["tracked", "TRACKER_VERSION"]
