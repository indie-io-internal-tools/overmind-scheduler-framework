# indie-scheduler-tracker (TypeScript)

Lightweight wrapper for tools that keep their own in-process scheduling but
want central inventory + heartbeats in the indie.io scheduler.

## Install

```bash
npm install github:indie-io-internal-tools/overmind-scheduler-framework#v1.3.0
# (the tracker source lives under tracker/typescript/ in that repo)
```

Or vendor the single `index.ts` directly into your project — it's <200 lines
of dependencies-free TypeScript.

## Configure

Add to your tool's `.env`:

```
SCHEDULER_REGISTRY_URL=https://overmind.indie.io/marketing/scheduler
SCHEDULER_HEARTBEAT_SECRET=<same value as your team scheduler's SCHEDULER_WEBHOOK_SECRET>
```

If either is unset, the tracker silently no-ops. Your job still runs.

## Use

```typescript
import { tracked } from "indie-scheduler-tracker";
import cron from "node-cron";

const detectionJob = tracked(
  {
    name: "festival_detection",
    tool: "festival-submitter",
    cron: "30 9 * * 1-5",                // declarative — display only
    timezone: "America/New_York",
    url: "https://overmind.indie.io/marketing/festival-submitter/",
    owner: "you@indie.io",
    description: "HTMAG scrape + form fields + eligibility enqueue",
  },
  async () => {
    // your existing job body, unchanged
  }
);

cron.schedule("30 9 * * 1-5", detectionJob, { timezone: "America/New_York" });
```

The cron field in the `tracked()` config is **declarative metadata only**.
The framework never fires it and never alerts on a missed fire derived from it.
Your tool still owns the schedule.

## Invariants

The wrapper:

1. **Never throws.** Telemetry failure is invisible to the wrapped job.
2. **Never blocks.** Heartbeats fire-and-forget on a background fetch. Zero
   ms added to job latency.
3. **Never has import-time side effects.** First network call is on first
   wrapped invocation, not on import or on `tracked()` construction.
4. **Treats `cron` as metadata.** Framework never fires it.

## Missed-fire alerting

The tracker does NOT provide missed-fire alerting. The central scheduler
doesn't know when your job is *supposed* to fire — only when it does. If
you need missed-fire detection, either:

- Keep alerting in the tool (recommended — you're already there)
- Migrate to the framework's `trigger: "cron"` mode where the framework
  owns the schedule and can alert on missed fires
