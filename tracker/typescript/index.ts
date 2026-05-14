/**
 * indie-scheduler-tracker — lightweight wrapper for power-user tools.
 *
 * Tools that keep their own in-process scheduling (node-cron, BullMQ, setInterval,
 * etc.) wrap their callbacks with `tracked()`. The wrapper registers the job
 * with the central scheduler on first invocation and posts a heartbeat after
 * each invocation. The tool keeps owning the schedule; the framework just
 * tracks what fired and when.
 *
 * Configuration via env on the tool's side:
 *   SCHEDULER_REGISTRY_URL       base URL of the team's scheduler
 *   SCHEDULER_HEARTBEAT_SECRET   shared secret; must match the scheduler's
 *                                SCHEDULER_WEBHOOK_SECRET.
 * If either is unset, the tracker silently no-ops. The wrapped function still
 * runs unchanged.
 *
 * Invariants:
 *   - The cron field is metadata only. Framework never fires, never alerts off it.
 *   - Registration is non-fatal at startup (and lazy — first invocation only).
 *   - Heartbeats are fire-and-forget. Zero ms added to wrapped-job latency.
 *   - The wrapper never throws. Telemetry failure is invisible.
 */

export interface TrackedConfig {
  /** Unique within the tool's namespace. */
  name: string;
  /** Tool slug, e.g. "festival-submitter". */
  tool: string;
  /** Declarative cron expression; display-only. */
  cron?: string;
  /** IANA tz name; display-only. */
  timezone?: string;
  /** Public URL the central scheduler links to for "Open tool ↗". */
  url?: string;
  /** Owner email. */
  owner?: string;
  /** One-line description. */
  description?: string;
}

export const TRACKER_VERSION = "1.0.0";

const registered = new Set<string>();

function getConfig(): { url: string; secret: string } | null {
  const url = (process.env.SCHEDULER_REGISTRY_URL ?? "").replace(/\/$/, "").trim();
  const secret = (process.env.SCHEDULER_HEARTBEAT_SECRET ?? "").trim();
  if (!url || !secret) return null;
  return { url, secret };
}

function fireAndForget(path: string, payload: unknown): void {
  const cfg = getConfig();
  if (!cfg) return;

  // Don't await — caller doesn't wait. Errors are swallowed.
  fetch(`${cfg.url}${path}`, {
    method: "POST",
    headers: {
      "X-Scheduler-Secret": cfg.secret,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    // @ts-ignore — AbortSignal.timeout exists on Node 18+
    signal: AbortSignal.timeout(5000),
  }).catch(() => {
    // never throw; never log noisily — telemetry failure must be invisible
  });
}

function registerIfNeeded(cfg: TrackedConfig): void {
  if (registered.has(cfg.name)) return;
  registered.add(cfg.name);
  fireAndForget(`/api/register/${cfg.name}`, { ...cfg, tracker_version: TRACKER_VERSION });
}

function heartbeat(
  name: string,
  status: "success" | "error",
  durationMs: number,
  errorMessage: string | null,
  startedIso: string,
  endedIso: string
): void {
  fireAndForget(`/api/heartbeat/${name}`, {
    status,
    duration_ms: durationMs,
    start_ts: startedIso,
    end_ts: endedIso,
    error_message: errorMessage,
    tracker_version: TRACKER_VERSION,
  });
}

/**
 * Wrap a function so each invocation reports to the central scheduler.
 *
 * Construction is pure — no network at wrap time. First call triggers
 * registration; every call posts a heartbeat. Both happen on a fire-and-forget
 * basis in the background.
 */
export function tracked<TArgs extends unknown[], TReturn>(
  config: TrackedConfig,
  fn: (...args: TArgs) => TReturn | Promise<TReturn>
): (...args: TArgs) => TReturn | Promise<TReturn> {
  return function wrapped(...args: TArgs): TReturn | Promise<TReturn> {
    try {
      registerIfNeeded(config);
    } catch {
      /* never throw */
    }
    const started = new Date();
    const t0 = performance.now();

    const emit = (status: "success" | "error", err: unknown): void => {
      try {
        const durationMs = Math.round(performance.now() - t0);
        const errorMessage =
          err instanceof Error
            ? `${err.name}: ${err.message}`.slice(0, 1000)
            : err
            ? String(err).slice(0, 1000)
            : null;
        heartbeat(config.name, status, durationMs, errorMessage, started.toISOString(), new Date().toISOString());
      } catch {
        /* never throw */
      }
    };

    let result: TReturn | Promise<TReturn>;
    try {
      result = fn(...args);
    } catch (err) {
      emit("error", err);
      throw err;
    }

    if (result && typeof (result as Promise<TReturn>).then === "function") {
      return (result as Promise<TReturn>).then(
        (value) => {
          emit("success", null);
          return value;
        },
        (err) => {
          emit("error", err);
          throw err;
        }
      );
    }

    emit("success", null);
    return result;
  };
}
