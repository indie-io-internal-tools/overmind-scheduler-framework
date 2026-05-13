"""Slack failure notifier — fires on any job that ends in status=failed.

Uses chat.postMessage with a bot token so we can post by channel ID and
share the existing indie.io Slack bot. Silently no-ops if SLACK_BOT_TOKEN
or SLACK_FAILURE_CHANNEL aren't set. httpx exceptions are swallowed so the
notifier itself can't crash the scheduler.

Provides both sync and async entry points. Async webhook handlers must use
post_failure_async so they don't block the FastAPI event loop on Slack's
network call.
"""

import asyncio

import httpx

from . import config


def _format(job_name: str, run_id: int, error_message: str) -> str:
    return (
        f":warning: *Scheduler job failed: `{job_name}`*\n"
        f"Run ID: `{run_id}`\n"
        f"Error: `{error_message}`"
    )


def _gate(job_name: str, run_id: int, error_message: str) -> bool:
    """Returns True if we should attempt to post. False = no-op (logged)."""
    if not config.SLACK_BOT_TOKEN or not config.SLACK_FAILURE_CHANNEL:
        print(
            f"[notifier] would notify: {job_name} run {run_id} failed: {error_message} "
            f"(SLACK_BOT_TOKEN and/or SLACK_FAILURE_CHANNEL not set)"
        )
        return False
    return True


def post_failure(job_name: str, run_id: int, error_message: str) -> None:
    """Synchronous post — for sync cron jobs. Blocks for up to 5s on Slack."""
    if not _gate(job_name, run_id, error_message):
        return
    try:
        r = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "channel": config.SLACK_FAILURE_CHANNEL,
                "text": _format(job_name, run_id, error_message),
            },
            timeout=5.0,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not body.get("ok"):
            print(f"[notifier] slack rejected: {body}")
    except Exception as e:
        print(f"[notifier] post failed: {e}")


async def post_failure_async(job_name: str, run_id: int, error_message: str) -> None:
    """Async post — for webhook handlers running in the FastAPI event loop.

    Spawned as a background task so the calling handler doesn't await on
    Slack's network round-trip.
    """
    if not _gate(job_name, run_id, error_message):
        return

    async def _send() -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "channel": config.SLACK_FAILURE_CHANNEL,
                        "text": _format(job_name, run_id, error_message),
                    },
                )
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                if not body.get("ok"):
                    print(f"[notifier] slack rejected: {body}")
        except Exception as e:
            print(f"[notifier] post failed: {e}")

    # Schedule as background task; do not await — return immediately so the
    # webhook handler's response goes out without waiting on Slack.
    asyncio.create_task(_send())
