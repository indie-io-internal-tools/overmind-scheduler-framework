---
name: scheduler-authoring
description: Authoring guide for jobs running on the indie.io scheduler framework. Use when writing, modifying, debugging, or deploying any file in a team box's `jobs/` directory; when the user asks to "build me a job that...", "schedule something to run weekly", "migrate this n8n workflow", "fix my scheduler job", or anything similar. Covers the JOB dict contract, cron and webhook patterns, the ctx parameter, retry helpers, local testing, deploy flow, and common pitfalls.
---

# Scheduler Authoring

You are about to write or modify a job for the indie.io scheduler. This file is your reference — everything you need to write a correct, deployed job is here.

## The mental model

- One job = one Python file in `jobs/<snake_case>.py`.
- The file declares a top-level `JOB` dict (config) and either a `run(ctx)` function (cron) or an `async handle_webhook(request)` function (webhook).
- The framework discovers files in `jobs/` at startup, schedules cron jobs, and routes incoming webhook POSTs to `/<team>/scheduler/webhooks/<job_name>`.
- All run state (stdout, stderr, status, duration) is captured to a SQLite DB and viewable in the `/jobs/<name>` UI.

## File layout in a team box

```
overmind-<team>/scheduler/
  jobs/                  ← you add files here
    my_daily_thing.py
    my_webhook_handler.py
  data/                  ← SQLite DB, auto-managed
  .env                   ← secrets (gitignored, never commit)
  requirements.txt       ← pins indie-scheduler version
  ecosystem.config.cjs   ← PM2 entry
```

## The JOB dict

Required keys for every job:

```python
JOB = {
    "name": "snake_case_name",          # must match filename stem
    "description": "what this does",     # one line
    "owner": "you@indie.io",
    "trigger": "cron",                   # "cron" or "webhook"
    "enabled": True,
}
```

Cron jobs add:
```python
    "cron": "0 8 * * tue",   # five-field cron expression
```

Webhook jobs add:
```python
    "auth": "internal",       # required: "internal" | "shared_secret" | "open"
```

### Optional knobs

```python
    "misfire_grace_seconds": 3600,  # default is auto-derived from cadence
    "max_instances": 1,             # concurrent runs cap; default 1
    "timeout_seconds": 600,         # watchdog flips runs to "timeout" if exceeded
```

## Cron expressions — use day NAMES

**Important:** The framework normalizes Unix-style numeric day-of-week to APScheduler's day-name notation (because APScheduler's `from_crontab` does NOT remap and `2` would silently mean Wednesday instead of Tuesday). The framework handles this for you, but the safest habit is to use names:

```python
"cron": "0 8 * * tue"          # Tuesday at 8am — clear
"cron": "0 9 * * mon-fri"      # weekdays at 9am
"cron": "*/5 * * * *"          # every 5 minutes
"cron": "0 0 1 * *"            # 1st of month, midnight
```

All cron expressions are interpreted in `America/Los_Angeles` (configurable via `SCHEDULER_TIMEZONE`). DST is handled — write times in local time and they'll fire correctly across spring-forward / fall-back.

## Writing a cron job

Minimum viable:

```python
"""Daily sync of X from Y to Z."""

JOB = {
    "name": "daily_x_sync",
    "description": "Pulls X from Y at 9am and updates Z.",
    "owner": "you@indie.io",
    "trigger": "cron",
    "cron": "0 9 * * *",
    "enabled": True,
}

def run(ctx):
    log = ctx["log"]
    log.info("starting daily_x_sync")
    # ... do the thing ...
    log.info("done")
```

### The ctx parameter

`ctx` is a dict with:
- `ctx["log"]` — a `logging.Logger` scoped to this job. Use `log.info(...)`, `log.warning(...)`, `log.exception("...")`. Logs flow to both pm2 stdout AND the run-detail page in the UI.
- `ctx["run_id"]` — integer ID of this run, useful for correlation
- `ctx["job_name"]` — the job's name

You can also use `print()` — it's tee'd to both pm2 and the DB. But `log.info(...)` is preferred because it includes structured `job_name` + `run_id` in the line format.

### Exceptions = failed runs

Raise (or don't catch) any exception → the run is marked `failed`, the traceback goes into the run's stderr, and a Slack alert fires to the failure channel. No try/except wrapping needed unless you want to handle a specific error.

## Writing a webhook job

Webhook jobs must declare auth. Pick one:

### `auth: "shared_secret"` (default choice for new webhooks)

Framework verifies header `X-Scheduler-Secret` against env var `SCHEDULER_WEBHOOK_SECRET`. If mismatch → 403 before your handler runs.

```python
JOB = {
    "name": "stripe_webhook",
    "description": "Receives Stripe events.",
    "owner": "you@indie.io",
    "trigger": "webhook",
    "auth": "shared_secret",
    "enabled": True,
}

async def handle_webhook(request):
    body = await request.json()
    # Framework already verified the secret — just do the work.
    return {"ok": True}
```

Caller side (the system POSTing to you) sends `X-Scheduler-Secret: <your secret>` as a header. Tell the integration partner to add that header; rotate the secret by changing `.env` and PM2 reload.

### `auth: "internal"` (when the integration brings its own auth)

For services like Wrike or Slack that do HMAC signing — you verify yourself, framework defers. Use this only when the external service demands its own auth scheme.

```python
JOB = {
    "name": "wrike_thing",
    "trigger": "webhook",
    "auth": "internal",   # I'm verifying inside the handler
    ...
}

async def handle_webhook(request):
    raw = await request.body()
    sig = request.headers.get("x-hook-signature", "")
    if not _verify_hmac(raw, sig, os.environ["WRIKE_WEBHOOK_SECRET"]):
        raise HTTPException(403, "invalid signature")
    # ...
```

### `auth: "open"` (rare — public endpoint)

For genuinely public stuff (a status pingback, etc.). Use sparingly.

If you forget to declare `auth`, the framework returns 503 and refuses to dispatch. There is no silent default — auth is required.

## Helpers

### Retry with backoff

For flaky external APIs:

```python
from indie_scheduler.helpers import retry_with_backoff

def run(ctx):
    body = retry_with_backoff(
        lambda: httpx.get("https://api.example.com/things", timeout=30).json(),
        attempts=4,
        base_seconds=2,
    )
```

Exponential delay with jitter, capped at `max_seconds`. Raises `RetryError` after exhausting attempts.

### Secrets — use `.env`

Never hardcode tokens. Read from environment:

```python
import os
WRIKE_TOKEN = os.environ["WRIKE_API_TOKEN"].strip()  # KeyError fails the run loudly
```

Don't print tokens. The framework redacts common patterns (Bearer, `sk-`, `xoxb-`, `AKIA…`, `ghp_`, generic `key=…`) before storing run output, but the safest move is to not log them in the first place.

## Testing locally

Before deploying, run the job in isolation:

```bash
# From your team box's scheduler/ directory
source venv/bin/activate
python -c "
import sys; sys.path.insert(0, '.')
from indie_scheduler.app import registry
registry.load()
job = registry.get('my_daily_thing')
job.run({'job_name': 'my_daily_thing', 'run_id': 0, 'log': __import__('logging').getLogger('test')})
"
```

For webhook jobs, you can hit the local endpoint directly:

```bash
curl -X POST http://127.0.0.1:3014/webhooks/my_webhook \
     -H "X-Scheduler-Secret: $SCHEDULER_WEBHOOK_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"test": "payload"}'
```

## Deploying

1. Commit and push to your team's box repo (`overmind-<team>`).
2. GH Actions auto-deploys via rsync + PM2 reload (~30 seconds).
3. Visit `https://overmind.indie.io/<team>/scheduler/` — your new job is listed.
4. Click into it to see the schedule, click "Run now" to fire a manual test run.
5. Click into the run to see stdout, stderr, status, duration.

## Debugging

Order of operations when a job misbehaves:

1. **`/jobs/<name>` page** — shows last 50 runs with status. Find the failed one.
2. **Run detail page** — full stdout, stderr, error message.
3. **`pm2 logs <team>-scheduler` on the box** — framework-level logs (scheduling, watchdog firings, APScheduler events). Job logs also flow here (tee'd).
4. **`/api/health`** — returns 503 with `problems[]` if scheduler is unhealthy.

Common issues and fixes:

| Symptom | Likely cause |
|---|---|
| Job fires on the wrong day | You used a numeric day-of-week. Framework remaps to day names; the log line `[scheduler] X: normalized dow '...' -> '...'` confirms. Always use day names anyway. |
| "scheduled job missed" Slack alert | Box was down longer than the misfire grace. Default grace is cadence-aware (24h for weekly, 5min for sub-hourly). Override via `misfire_grace_seconds` if needed. |
| Job appears to "stop running" | Likely concurrent runs got skipped due to `max_instances=1`. Check the missed/skipped alerts. Either increase `max_instances` or speed up the job. |
| Run stuck in `running` forever | The worker thread is wedged. Set `timeout_seconds` on the job — the watchdog will mark it `timeout` and alert. Worker thread keeps running (Python can't safely kill threads); the next scheduled fire will start cleanly. |
| `503 webhook auth not declared` | Add `"auth": "..."` to the JOB dict. |
| Secrets visible in run details | Framework redacts common patterns but isn't exhaustive. Stop printing the secret; rotate it if it was committed to git. |

## Before you commit checklist

- [ ] JOB `name` matches filename stem (snake_case)
- [ ] `owner` is your real email
- [ ] Cron expression uses day **names** (`tue`, not `2`) if it has a day-of-week component
- [ ] Webhook jobs declare `auth`
- [ ] Long-running jobs declare `timeout_seconds`
- [ ] Job reads secrets from `os.environ[...]`, doesn't hardcode them
- [ ] Job uses `ctx["log"]` (or `print()`) — both flow to the run detail page
- [ ] Job has been run locally at least once and didn't explode
- [ ] If migrating from n8n: the original n8n workflow is disabled before this ships, not both running in parallel

## What to NOT do

- **Don't write a job that calls `subprocess` to run another Python script.** Just put the logic in the job module.
- **Don't add `try/except: pass`** to swallow errors. Let them surface — failed runs alert Slack.
- **Don't share state between job runs via module-level globals.** Each run should be self-contained. Modules are loaded once at scheduler startup, not per-run.
- **Don't import from another team's `jobs/` directory.** Each team's jobs are isolated.
- **Don't bypass the framework's auth check** with `auth: "open"` unless the endpoint is genuinely public.
- **Don't `time.sleep()` for hours** inside a job — that holds a thread pool slot. If you need a delay, schedule a separate job.

## When asked to migrate an n8n workflow

1. Read the n8n nodes top to bottom. Map each one to Python.
2. Schedule nodes → JOB dict `cron` field. Webhook nodes → `trigger: "webhook"`.
3. HTTP request nodes → `httpx.get/post`.
4. Sheets / Slack / Wrike nodes → use the API directly via `httpx` (the framework doesn't yet ship shared client libraries — each job does its own HTTP).
5. Function/Code nodes → inline Python.
6. Wire up the secrets in `.env`.
7. Test locally before disabling the n8n workflow.
8. Only after the new job runs successfully on the box for at least one full cycle, disable (don't delete) the n8n workflow.

## When something in this skill is wrong or missing

The skill lives in `overmind-scheduler-framework/.claude/skills/scheduler-authoring/SKILL.md`. Open a PR. Framework maintainers will review.
