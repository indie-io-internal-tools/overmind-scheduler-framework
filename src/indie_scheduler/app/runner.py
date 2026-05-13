"""Job execution + DB lifecycle.

Captures per-run stdout/stderr without mutating global I/O. Writes a row to the
runs table. On exception, marks the run as 'failed' and notifies Slack (if
configured).

Concurrency model: a ContextVar holds the per-run buffer pair. A tee proxy
on sys.stdout/sys.stderr writes to BOTH the original stream (so PM2 logs
still capture job output) AND the active buffer (for DB storage). Each
run sets the contextvar fresh; concurrent runs in different threads/tasks
get isolated buffers — no cross-contamination.
"""

from __future__ import annotations

import contextvars
import io
import logging
import sys
import traceback
from typing import Any, Optional

from . import db, notifier, registry


# ── Per-run output capture ──────────────────────────────────────────────────
_RUN_BUFFERS: contextvars.ContextVar[Optional[tuple[io.StringIO, io.StringIO]]] = \
    contextvars.ContextVar("scheduler_run_buffers", default=None)


class _TeeStream:
    """Write-through proxy: forwards to the original stream AND, if the
    contextvar is set, also writes to the per-run buffer for this context.
    """

    def __init__(self, original, slot: str):
        self._original = original
        self._slot = slot  # "out" or "err"

    def write(self, s: str) -> int:
        try:
            bufs = _RUN_BUFFERS.get()
            if bufs is not None:
                (bufs[0] if self._slot == "out" else bufs[1]).write(s)
        except Exception:
            pass  # never let logging crash a job
        return self._original.write(s)

    def flush(self) -> None:
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)


_INSTALLED = False


def install_tee_streams() -> None:
    """Install the tee on sys.stdout/sys.stderr once at process startup."""
    global _INSTALLED
    if _INSTALLED:
        return
    sys.stdout = _TeeStream(sys.stdout, "out")
    sys.stderr = _TeeStream(sys.stderr, "err")
    _INSTALLED = True


# ── Framework logger ────────────────────────────────────────────────────────
class _RunContextFilter(logging.Filter):
    """Inject run_id + job_name into every log record from the framework or
    a job. Pulls from contextvars so concurrent runs don't cross-contaminate.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _RUN_CONTEXT.get()
        record.job_name = ctx.get("job_name", "-") if ctx else "-"
        record.run_id = ctx.get("run_id", "-") if ctx else "-"
        return True


_RUN_CONTEXT: contextvars.ContextVar[Optional[dict]] = \
    contextvars.ContextVar("scheduler_run_context", default=None)


def configure_logging() -> None:
    """Install the framework's log format. Idempotent."""
    root = logging.getLogger()
    if any(getattr(h, "_scheduler_marker", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler._scheduler_marker = True
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s job=%(job_name)s run=%(run_id)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ))
    handler.addFilter(_RunContextFilter())
    root.addHandler(handler)
    if root.level == logging.WARNING:  # default
        root.setLevel(logging.INFO)
    # Wire APScheduler's own logger in so missed/coalesced events reach pm2.
    for name in ("apscheduler.scheduler", "apscheduler.executors", "apscheduler.jobstores"):
        logging.getLogger(name).setLevel(logging.INFO)


def _make_job_logger(job_name: str) -> logging.Logger:
    log = logging.getLogger(f"job.{job_name}")
    log.setLevel(logging.INFO)
    return log


# ── Cron execution ──────────────────────────────────────────────────────────
def run_cron_job(job_name: str) -> dict[str, Any]:
    job = registry.get(job_name)
    if job is None:
        return {"ok": False, "error": "unknown job"}
    if not job.enabled:
        return {"ok": False, "error": "job disabled"}
    if not job.is_cron():
        return {"ok": False, "error": "not a cron job"}

    run_id = db.insert_run(job_name, "cron")
    out_buf, err_buf = io.StringIO(), io.StringIO()
    # Set the per-run contextvars. APScheduler executes sync cron jobs on a
    # thread-pool — contextvars are thread-local, so each thread gets its
    # own pair and won't see another run's buffers.
    buf_token = _RUN_BUFFERS.set((out_buf, err_buf))
    ctx_token = _RUN_CONTEXT.set({"job_name": job_name, "run_id": run_id})

    log = _make_job_logger(job_name)
    log.info("started")

    status = "success"
    error_message = None
    try:
        job.run({"job_name": job_name, "run_id": run_id, "log": log})
    except Exception as e:
        status = "failed"
        error_message = f"{type(e).__name__}: {e}"
        err_buf.write(traceback.format_exc())
        log.exception("failed: %s", error_message)
    finally:
        _RUN_BUFFERS.reset(buf_token)
        _RUN_CONTEXT.reset(ctx_token)

    db.finalize_run(
        run_id,
        status=status,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
        error_message=error_message,
    )
    if status == "failed":
        notifier.post_failure(job_name, run_id, error_message or "(no error message)")
    log.info("finished: %s", status)
    return {"ok": status == "success", "run_id": run_id, "status": status}


# ── Webhook execution ───────────────────────────────────────────────────────
async def run_webhook_job(job_name: str, request) -> tuple[Any, dict[str, Any]]:
    """Returns (handler_result, run_meta). Handler result becomes the HTTP body."""
    import hmac as _hmac
    import os as _os

    job = registry.get(job_name)
    if job is None or not job.is_webhook():
        return None, {"ok": False, "error": "unknown webhook job", "status_code": 404}
    if not job.enabled:
        return None, {"ok": False, "error": "job disabled", "status_code": 403}

    # Framework-level auth check. The job MUST declare its auth mode in
    # JOB["auth"]. We refuse to dispatch otherwise — silently-open webhooks
    # have caused outages elsewhere.
    auth_mode = job.webhook_auth
    if auth_mode not in {"internal", "shared_secret", "open"}:
        return None, {
            "ok": False,
            "error": "webhook auth not declared",
            "status_code": 503,
        }
    if auth_mode == "shared_secret":
        expected = _os.environ.get("SCHEDULER_WEBHOOK_SECRET", "").strip()
        if not expected:
            return None, {
                "ok": False,
                "error": "SCHEDULER_WEBHOOK_SECRET not configured",
                "status_code": 503,
            }
        provided = request.headers.get("x-scheduler-secret", "")
        if not _hmac.compare_digest(provided, expected):
            return None, {"ok": False, "error": "invalid secret", "status_code": 403}
    # auth_mode == "internal" or "open": framework defers to the handler.

    run_id = db.insert_run(job_name, "webhook")
    out_buf, err_buf = io.StringIO(), io.StringIO()
    buf_token = _RUN_BUFFERS.set((out_buf, err_buf))
    ctx_token = _RUN_CONTEXT.set({"job_name": job_name, "run_id": run_id})

    log = _make_job_logger(job_name)
    log.info("webhook started")

    status = "success"
    error_message = None
    result: Any = None
    try:
        result = await job.handle_webhook(request)
    except Exception as e:
        status = "failed"
        error_message = f"{type(e).__name__}: {e}"
        err_buf.write(traceback.format_exc())
        log.exception("webhook failed: %s", error_message)
    finally:
        _RUN_BUFFERS.reset(buf_token)
        _RUN_CONTEXT.reset(ctx_token)

    db.finalize_run(
        run_id,
        status=status,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
        error_message=error_message,
    )
    if status == "failed":
        await notifier.post_failure_async(job_name, run_id, error_message or "(no error message)")
    log.info("webhook finished: %s", status)
    return result, {"ok": status == "success", "run_id": run_id, "status": status}
