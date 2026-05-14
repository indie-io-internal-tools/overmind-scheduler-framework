"""Scheduler — FastAPI app + APScheduler running cron jobs.

Single PM2 process. Cron schedules come from each job module's JOB dict.
Webhook jobs are routed at /webhooks/<name>. Run history lives in SQLite.
"""

from __future__ import annotations

import hmac as _hmac
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, registry, runner


def humanize_iso(iso_str: Optional[str]) -> str:
    """Convert an ISO timestamp to a relative phrase like '3 min ago'."""
    if not iso_str:
        return "—"
    try:
        ts = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    s = int(delta.total_seconds())
    if s < 0:
        return "just now"
    if s < 45:
        return "just now"
    if s < 3600:
        m = max(1, s // 60)
        return f"{m} min ago"
    if s < 86400:
        h = s // 3600
        return f"{h} hr ago"
    if s < 7 * 86400:
        d = s // 86400
        return f"{d} day{'s' if d != 1 else ''} ago"
    return ts.strftime("%b ") + str(ts.day)


scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)


_UNIX_DOW_TO_NAME = {
    "0": "sun", "1": "mon", "2": "tue", "3": "wed",
    "4": "thu", "5": "fri", "6": "sat", "7": "sun",
}


def _normalize_cron_dow(expr: str) -> str:
    """Translate Unix-cron-style numeric day-of-week to APScheduler day names.

    APScheduler's CronTrigger.from_crontab passes the day-of-week field
    through verbatim, but internally treats 0=Monday — so a numeric
    "2" means Wednesday, not Tuesday like Unix cron. This silently fires
    jobs one day off. Translate numerics in the dow field to day names
    so the behavior matches what authors expect.

    Only the 5th field is touched. Leaves ranges/lists/steps with non-numeric
    tokens alone (e.g. "mon-fri" passes through untouched; "1-5" becomes
    "mon-fri").
    """
    parts = expr.split()
    if len(parts) != 5:
        return expr
    dow = parts[4]
    if dow in ("*", "?"):
        return expr

    def map_token(tok: str) -> str:
        # Handle step: "*/2" stays as-is, "1-5/2" becomes "mon-fri/2"
        if "/" in tok:
            base, step = tok.split("/", 1)
            return f"{map_token(base)}/{step}"
        # Handle range: "1-5" -> "mon-fri"
        if "-" in tok:
            lo, hi = tok.split("-", 1)
            return f"{map_token(lo)}-{map_token(hi)}"
        return _UNIX_DOW_TO_NAME.get(tok, tok)

    new_dow = ",".join(map_token(t) for t in dow.split(","))
    parts[4] = new_dow
    return " ".join(parts)


def _compute_misfire_grace(trigger, configured: Optional[int]) -> int:
    """Pick a misfire grace based on the trigger's fire cadence.

    APScheduler default of 1s drops any missed run. The old framework setting
    of 300s dropped weekly jobs whenever the box was down >5 min, which
    showed up in production as "the job just didn't run this week."

    Cadence-based defaults:
      - sub-hourly:  5 min grace
      - sub-daily:   1 hour grace
      - daily+:      24 hour grace  (catches deploy restarts and overnight
                                     outages on weekly schedules)

    Authors can override via JOB["misfire_grace_seconds"].
    """
    if configured is not None:
        return int(configured)
    now = datetime.now(config.TIMEZONE)
    first = trigger.get_next_fire_time(None, now)
    if first is None:
        return 300
    second = trigger.get_next_fire_time(first, first)
    if second is None:
        return 86400  # one-shot trigger — be generous
    period = (second - first).total_seconds()
    if period <= 3600:
        return 300
    if period <= 86400:
        return 3600
    return 86400


def _schedule_one(job) -> bool:
    """Add a single cron job to APScheduler. Returns True if scheduled."""
    if not job.is_cron() or not job.enabled or not job.cron:
        return False
    cron_expr = _normalize_cron_dow(job.cron)
    if cron_expr != job.cron:
        print(f"[scheduler] {job.name}: normalized dow {job.cron!r} -> {cron_expr!r}")
    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=config.TIMEZONE)
    except ValueError as e:
        print(f"[scheduler] {job.name}: bad cron {job.cron!r}: {e}")
        return False
    grace = _compute_misfire_grace(trigger, job.misfire_grace_seconds)
    scheduler.add_job(
        runner.run_cron_job,
        trigger=trigger,
        args=[job.name],
        id=job.name,
        replace_existing=True,
        misfire_grace_time=grace,
        max_instances=job.max_instances,
        coalesce=True,
    )
    print(f"[scheduler] scheduled {job.name} -> {job.cron} "
          f"(misfire_grace={grace}s, max_instances={job.max_instances})")
    return True


def _on_skip_event(event):
    """Notify Slack when APScheduler drops a fire (overlap or missed grace).

    EVENT_JOB_MAX_INSTANCES: previous run still running when the next fire
    was due — APScheduler dropped this fire. Visible to humans as "the job
    silently stopped firing" unless we alert.

    EVENT_JOB_MISSED: scheduled fire was outside the misfire grace window
    (box was down too long). Also silent without an alert.
    """
    kind = "skipped (still running)" if event.code == EVENT_JOB_MAX_INSTANCES else "missed (outside grace)"
    msg = f"job `{event.job_id}` {kind} at {event.scheduled_run_time}"
    print(f"[scheduler] {msg}")
    try:
        from . import notifier
        notifier.post_failure(event.job_id, 0, f"scheduler {kind}")
    except Exception as e:
        print(f"[scheduler] could not notify on skip: {e}")


def _unschedule_one(job_name: str) -> None:
    try:
        scheduler.remove_job(job_name)
        print(f"[scheduler] unscheduled {job_name}")
    except Exception:
        pass


def _schedule_cron_jobs() -> None:
    for job in registry.all_jobs():
        _schedule_one(job)


def _purge_old_runs() -> None:
    n = db.purge_old_runs(config.RETENTION_DAYS)
    if n:
        print(f"[scheduler] purged {n} run rows older than {config.RETENTION_DAYS} days")


def _watchdog_tick() -> None:
    """Find any in-flight run that has exceeded its job's timeout_seconds,
    mark it timed out, and post a Slack alert. The worker thread continues
    in the background — Python doesn't permit safe thread kill — but the
    operator now sees the timeout and can investigate."""
    try:
        running = db.list_running_runs()
    except Exception as e:
        print(f"[watchdog] DB read failed: {e}")
        return
    if not running:
        return
    now = datetime.now(timezone.utc)
    for row in running:
        job = registry.get(row["job_name"])
        if job is None or job.timeout_seconds is None:
            continue
        started = datetime.fromisoformat(row["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (now - started).total_seconds()
        if elapsed > job.timeout_seconds:
            db.mark_timed_out(row["id"], job.timeout_seconds)
            try:
                from . import notifier
                notifier.post_failure(
                    row["job_name"],
                    row["id"],
                    f"run exceeded timeout_seconds={job.timeout_seconds} (elapsed {int(elapsed)}s)",
                )
            except Exception as e:
                print(f"[watchdog] notify failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    runner.install_tee_streams()
    runner.configure_logging()
    db.init_db()
    swept = db.sweep_interrupted_runs()
    if swept:
        print(f"[scheduler] swept {swept} interrupted run row(s) from previous process")
    registry.load()
    _schedule_cron_jobs()
    # Internal nightly cleanup. Underscore prefix keeps it out of the user-facing list.
    scheduler.add_job(
        _purge_old_runs,
        trigger=CronTrigger(hour=3, minute=0, timezone=config.TIMEZONE),
        id="_internal_cleanup",
        replace_existing=True,
    )
    # Weekly VACUUM to reclaim space after row purges.
    scheduler.add_job(
        db.vacuum,
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=config.TIMEZONE),
        id="_internal_vacuum",
        replace_existing=True,
    )
    # Per-minute watchdog: marks runs exceeding their job's timeout_seconds
    # as 'timeout' and alerts. We can't safely kill the worker thread, but
    # the operator at least sees the failure and a fresh run can start.
    from apscheduler.triggers.interval import IntervalTrigger
    scheduler.add_job(
        _watchdog_tick,
        trigger=IntervalTrigger(seconds=60, timezone=config.TIMEZONE),
        id="_internal_watchdog",
        replace_existing=True,
    )
    scheduler.add_listener(_on_skip_event, EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED)
    scheduler.start()
    print(f"[scheduler] started in {config.TIMEZONE.key}, {len(registry.all_jobs())} user jobs")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Overmind Scheduler",
    lifespan=lifespan,
    root_path=config.BASE_PATH,
)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))
templates.env.filters["humanize"] = humanize_iso
# Inject team context into every template render so the banner/title adapt
# per-box without having to thread these through every endpoint.
templates.env.globals["team_display"] = config.TEAM_NAME.capitalize() if config.TEAM_NAME.islower() else config.TEAM_NAME
templates.env.globals["team_home"] = config.TEAM_HOME_URL


_CSRF_COOKIE = "scheduler_csrf"


def _ensure_csrf(request: Request, response_headers: dict) -> str:
    """Return the request's CSRF token, setting a cookie if absent. Used as
    a double-submit token: the value in the cookie must match the value
    submitted with the form. Same-origin enforcement via Cloudflare Access
    + this token blocks cross-site form submissions."""
    token = request.cookies.get(_CSRF_COOKIE)
    if not token:
        token = secrets.token_urlsafe(32)
        response_headers["set-cookie"] = (
            f"{_CSRF_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        )
    return token


def _check_csrf(request: Request, form_token: Optional[str]) -> bool:
    cookie_token = request.cookies.get(_CSRF_COOKIE) or ""
    if not cookie_token or not form_token:
        return False
    return _hmac.compare_digest(cookie_token, form_token)


@app.get("/api/health")
def health():
    """Liveness + dependency check. Returns 503 if any dependency is unhealthy
    so PM2 / Cloudflare / external monitors can detect a wedged scheduler.

    Checks: scheduler thread alive, DB roundtrip succeeds, ≥1 cron job
    scheduled, no run has been stuck in 'running' for more than 1 hour.
    """
    problems: list[str] = []
    try:
        if not scheduler.running:
            problems.append("scheduler not running")
    except Exception as e:
        problems.append(f"scheduler check failed: {e}")
    try:
        with db.conn() as c:
            c.execute("SELECT 1").fetchone()
    except Exception as e:
        problems.append(f"db unreachable: {e}")
    cron_count = sum(1 for j in registry.all_jobs() if j.is_cron() and j.enabled)
    if cron_count == 0 and registry.all_jobs():
        problems.append("no cron jobs scheduled")
    # Stuck runs: any 'running' row older than 1h means a previous process
    # died mid-job and the startup sweep didn't catch it (or the sweep itself
    # is broken).
    try:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        cutoff = (_dt.now(_tz.utc) - _td(hours=1)).isoformat(timespec="seconds")
        with db.conn() as c:
            stuck = c.execute(
                "SELECT COUNT(*) FROM runs WHERE status='running' AND started_at < ?",
                (cutoff,),
            ).fetchone()[0]
        if stuck:
            problems.append(f"{stuck} run(s) stuck in 'running' >1h")
    except Exception:
        pass

    status_code = 200 if not problems else 503
    body = {
        "status": "healthy" if not problems else "unhealthy",
        "service": "operations-scheduler",
        "scheduler_running": getattr(scheduler, "running", False),
        "cron_jobs_scheduled": cron_count,
        "problems": problems,
    }
    return JSONResponse(body, status_code=status_code)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    jobs_with_runs = []
    for job in registry.all_jobs():
        last = db.last_run(job.name)
        recent = db.recent_runs(job.name, limit=10)
        jobs_with_runs.append({"job": job, "last": last, "recent": recent})
    resp_headers: dict = {}
    csrf = _ensure_csrf(request, resp_headers)
    resp = templates.TemplateResponse(
        request,
        "index.html",
        {
            "base": config.BASE_PATH,
            "jobs": jobs_with_runs,
            "csrf_token": csrf,
        },
    )
    for k, v in resp_headers.items():
        resp.headers[k] = v
    return resp


@app.get("/jobs/{job_name}", response_class=HTMLResponse)
def job_detail(request: Request, job_name: str):
    job = registry.get(job_name)
    if job is None:
        return HTMLResponse(f"Unknown job: {job_name}", status_code=404)
    runs = db.recent_runs(job.name, limit=50)
    resp_headers: dict = {}
    csrf = _ensure_csrf(request, resp_headers)
    resp = templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "base": config.BASE_PATH,
            "job": job,
            "runs": runs,
            "csrf_token": csrf,
        },
    )
    for k, v in resp_headers.items():
        resp.headers[k] = v
    return resp


@app.get("/jobs/{job_name}/source", response_class=HTMLResponse)
def job_source(request: Request, job_name: str):
    job = registry.get(job_name)
    if job is None:
        return HTMLResponse(f"Unknown job: {job_name}", status_code=404)
    try:
        source = job.module_path.read_text(encoding="utf-8")
    except OSError as e:
        source = f"(could not read source: {e})"
    return templates.TemplateResponse(
        request,
        "job_source.html",
        {
            "base": config.BASE_PATH,
            "job": job,
            "source": source,
        },
    )


@app.get("/jobs/{job_name}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, job_name: str, run_id: int):
    job = registry.get(job_name)
    run = db.get_run(run_id)
    if job is None or run is None or run["job_name"] != job_name:
        return HTMLResponse("Run not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "base": config.BASE_PATH,
            "job": job,
            "run": run,
        },
    )


from fastapi import Form


@app.post("/jobs/{job_name}/toggle")
def toggle(job_name: str, request: Request, csrf_token: str = Form("")):
    if not _check_csrf(request, csrf_token):
        return JSONResponse({"error": "invalid csrf token"}, status_code=403)
    job = registry.get(job_name)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    new_enabled = not job.enabled
    db.set_enabled_override(job_name, new_enabled)
    job.enabled = new_enabled
    if job.is_cron():
        if new_enabled:
            _schedule_one(job)
        else:
            _unschedule_one(job_name)
    referer = request.headers.get("referer")
    target = referer if referer else f"{config.BASE_PATH}/"
    return RedirectResponse(url=target, status_code=303)


@app.post("/jobs/{job_name}/reset-enabled")
def reset_enabled(job_name: str, request: Request, csrf_token: str = Form("")):
    """Drop the DB enabled-override; the job reverts to its JOB-dict default."""
    if not _check_csrf(request, csrf_token):
        return JSONResponse({"error": "invalid csrf token"}, status_code=403)
    job = registry.get(job_name)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    db.clear_enabled_override(job_name)
    job.enabled = job.enabled_default
    if job.is_cron():
        if job.enabled:
            _schedule_one(job)
        else:
            _unschedule_one(job_name)
    target = request.headers.get("referer") or f"{config.BASE_PATH}/"
    return RedirectResponse(url=target, status_code=303)


@app.post("/jobs/{job_name}/run")
def trigger_run(job_name: str, request: Request, csrf_token: str = Form("")):
    if not _check_csrf(request, csrf_token):
        return JSONResponse({"error": "invalid csrf token"}, status_code=403)
    job = registry.get(job_name)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    if not job.is_cron():
        return JSONResponse({"error": "manual run only supported for cron jobs"}, status_code=400)
    # Dispatch through APScheduler so the run shares the same executor +
    # max_instances rules as a normal cron fire — and so the HTTP response
    # returns immediately. The unique id avoids colliding with the recurring
    # job's id.
    from datetime import datetime as _dt
    import uuid as _uuid
    scheduler.add_job(
        runner.run_cron_job,
        args=[job_name],
        id=f"_manual_{job_name}_{_uuid.uuid4().hex[:8]}",
        next_run_time=_dt.now(config.TIMEZONE),
        misfire_grace_time=60,
    )
    return RedirectResponse(
        url=f"{config.BASE_PATH}/jobs/{job_name}",
        status_code=303,
    )


@app.api_route("/webhooks/{job_name}", methods=["GET", "POST", "PUT", "HEAD"])
async def webhook(job_name: str, request: Request):
    from starlette.responses import Response as StarletteResponse

    result, meta = await runner.run_webhook_job(job_name, request)
    if not meta.get("ok") and "status_code" in meta:
        return JSONResponse({"error": meta.get("error")}, status_code=meta["status_code"])
    # Handlers can return a Response object directly (e.g. to set custom
    # response headers like Wrike's X-Hook-Secret handshake echo).
    if isinstance(result, StarletteResponse):
        return result
    if isinstance(result, (dict, list)):
        return JSONResponse(result)
    if isinstance(result, str):
        return HTMLResponse(result)
    return JSONResponse({"ok": True, "run_id": meta.get("run_id")})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=config.PORT, reload=False)
