# overmind-scheduler-framework

The scheduler framework that runs on every indie.io team's Overmind box. Replaces n8n.

## What it does

Hosts a FastAPI app + APScheduler instance that:
- Runs cron and webhook jobs from your box's `jobs/` directory
- Records each run (stdout, stderr, status, duration) to a local SQLite DB
- Exposes a web UI at `/` showing job state, recent runs, and per-run details
- Posts to Slack on failure

## Who maintains what

| | Owner |
|---|---|
| This framework repo (app, helpers, packaging) | Ops |
| Each team's `jobs/` directory and box deploy | That team |
| Bug reports against the framework | PR to this repo |
| New jobs, debugging your jobs, n8n migrations | Your team |

## Installation on a team box

The framework is consumed as a pip package. In your team's box repo:

```
# requirements.txt
indie-scheduler @ git+ssh://git@github.com/indie-io-internal-tools/overmind-scheduler-framework.git@v1.0.0
```

Pin to a tag. Use a compatible-release specifier (`~=1.0`) if you want to
auto-pull patch fixes.

Layout your team box expects:

```
overmind-<team>/
  jobs/                ← your jobs go here
    my_cron_job.py
    my_webhook_job.py
  data/                ← SQLite DB written here (auto-created)
  .env                 ← your secrets (gitignored)
  requirements.txt     ← pins indie-scheduler
  ecosystem.config.cjs ← PM2 entry, calls `indie-scheduler-serve`
```

PM2 entry example:

```javascript
module.exports = {
  apps: [{
    name: "<team>-scheduler",
    script: "indie-scheduler-serve",
    cwd: "/opt/apps/overmind-<team>/scheduler",
    interpreter: "/opt/apps/overmind-<team>/scheduler/venv/bin/python",
    env: { SCHEDULER_BASE_PATH: "/<team>/scheduler" },
  }]
}
```

## Writing a job

See `.claude/skills/scheduler-authoring/SKILL.md` for the full author guide —
that skill is what Claude Code uses to write jobs against this framework.

Short version:

```python
# jobs/my_daily_thing.py
JOB = {
    "name": "my_daily_thing",
    "description": "Runs at 9am Pacific every day.",
    "owner": "you@indie.io",
    "trigger": "cron",
    "cron": "0 9 * * *",
    "enabled": True,
}

def run(ctx):
    log = ctx["log"]
    log.info("doing the thing")
    # ...
```

Push to your team's repo, GH Actions deploys, the job appears at
`overmind.indie.io/<team>/scheduler/`.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `SCHEDULER_WORKING_DIR` | `cwd` | Where `jobs/`, `.env`, `data/` live |
| `SCHEDULER_PORT` | `3014` | HTTP port |
| `SCHEDULER_BASE_PATH` | `""` | URL prefix when reverse-proxied |
| `SCHEDULER_DB_PATH` | `<working>/data/runs.db` | Override DB path |
| `SCHEDULER_TIMEZONE` | `America/Los_Angeles` | Cron interpretation |
| `SCHEDULER_RETENTION_DAYS` | `90` | Run-row retention |
| `SCHEDULER_WEBHOOK_SECRET` | unset | For jobs with `auth="shared_secret"` |
| `SLACK_BOT_TOKEN` | unset | Failure notifier |
| `SLACK_FAILURE_CHANNEL` | unset | Channel ID for failures |

## Framework features (all opt-in via JOB dict)

- `misfire_grace_seconds` — override the cadence-based default
- `max_instances` — concurrency cap (default 1)
- `timeout_seconds` — watchdog flips runs to `timeout` if exceeded
- `auth` — for webhook jobs: `"internal" | "shared_secret" | "open"` (required)

## Reporting bugs

Open an issue or PR against this repo. Framework fixes land for all teams on
the next deploy.
