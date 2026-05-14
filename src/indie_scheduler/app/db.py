import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    duration_ms INTEGER,
    stdout TEXT,
    stderr TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_job_started
    ON runs (job_name, started_at DESC);

CREATE TABLE IF NOT EXISTS job_settings (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

-- External jobs are registered at runtime via POST /api/register/<name>.
-- Persisted so they survive scheduler restart. The `cron` column is
-- tracker-reported metadata only — framework never fires it.
CREATE TABLE IF NOT EXISTS external_jobs (
    name TEXT PRIMARY KEY,
    tool TEXT,
    cron TEXT,
    timezone TEXT,
    url TEXT,
    owner TEXT,
    description TEXT,
    tracker_version TEXT,
    first_seen_at TEXT NOT NULL,
    last_registered_at TEXT NOT NULL
);
"""


def init_db() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(config.DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
    finally:
        c.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_run(job_name: str, trigger_kind: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs (job_name, trigger_kind, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (job_name, trigger_kind, now_iso()),
        )
        return cur.lastrowid


_MAX_CAPTURE_BYTES = 16 * 1024  # 16KB per stream — enough for debugging,
                                # small enough to keep the DB bounded.


import re as _re

# Conservative patterns. Goal: catch common credentials without losing all
# the surrounding context. False-negatives are preferable to false-positives.
_REDACT_PATTERNS: list[tuple[_re.Pattern, str]] = [
    # Bearer / Basic auth
    (_re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 [REDACTED]"),
    # Authorization header values in dict-like output
    (_re.compile(r"(?i)(['\"]?Authorization['\"]?\s*[:=]\s*['\"])([^'\"]+)(['\"])"), r"\1[REDACTED]\3"),
    # Slack tokens
    (_re.compile(r"\b(xox[abposr]-[A-Za-z0-9-]{10,})"), "[REDACTED-SLACK-TOKEN]"),
    # OpenAI / Anthropic style
    (_re.compile(r"\b(sk-(ant-)?[A-Za-z0-9_\-]{20,})"), "[REDACTED-API-KEY]"),
    # Generic GitHub PAT
    (_re.compile(r"\b(ghp_[A-Za-z0-9]{20,})"), "[REDACTED-GH-PAT]"),
    # AWS access key id
    (_re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED-AWS-KEY]"),
    # Anything that looks like a long key=/token=/secret=/password= value
    (_re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)(\s*[:=]\s*)['\"]?([A-Za-z0-9_\-./+]{16,})['\"]?"),
     lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]"),
]


def _redact(s: str) -> str:
    if not s:
        return s
    for pat, repl in _REDACT_PATTERNS:
        s = pat.sub(repl, s)
    return s


def _truncate(s: str, limit: int = _MAX_CAPTURE_BYTES) -> str:
    """Trim very large captures. We keep the head AND tail so both the
    startup banner and the failure traceback survive."""
    if not s or len(s) <= limit:
        return s
    head = limit // 2 - 50
    tail = limit - head - 50
    return s[:head] + f"\n...[truncated {len(s) - head - tail} bytes]...\n" + s[-tail:]


def finalize_run(
    run_id: int,
    *,
    status: str,
    stdout: str,
    stderr: str,
    error_message: Optional[str] = None,
) -> None:
    with conn() as c:
        cur = c.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        if row is None:
            return
        started = datetime.fromisoformat(row["started_at"])
        ended = datetime.now(timezone.utc)
        duration_ms = int((ended - started).total_seconds() * 1000)
        c.execute(
            "UPDATE runs SET ended_at = ?, status = ?, duration_ms = ?, "
            "stdout = ?, stderr = ?, error_message = ? WHERE id = ?",
            (
                ended.isoformat(timespec="seconds"),
                status,
                duration_ms,
                _truncate(_redact(stdout)),
                _truncate(_redact(stderr)),
                error_message,
                run_id,
            ),
        )


def recent_runs(job_name: str, limit: int = 10) -> list[dict]:
    with conn() as c:
        cur = c.execute(
            "SELECT id, started_at, ended_at, status, duration_ms "
            "FROM runs WHERE job_name = ? ORDER BY started_at DESC LIMIT ?",
            (job_name, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def last_run(job_name: str) -> Optional[dict]:
    runs = recent_runs(job_name, limit=1)
    return runs[0] if runs else None


def purge_old_runs(retention_days: int) -> int:
    """Delete runs older than retention_days. Returns the number deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with conn() as c:
        cur = c.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        return cur.rowcount


def vacuum() -> None:
    """Reclaim disk space after purges. SQLite holds onto pages after
    DELETE; VACUUM rebuilds the file. Run weekly."""
    with conn() as c:
        c.execute("VACUUM")


def list_running_runs() -> list[dict]:
    """Return all rows currently in status='running'. Watchdog uses this
    to detect runs that have exceeded their job's declared timeout."""
    with conn() as c:
        cur = c.execute(
            "SELECT id, job_name, started_at FROM runs WHERE status='running'"
        )
        return [dict(r) for r in cur.fetchall()]


def mark_timed_out(run_id: int, timeout_seconds: int) -> None:
    """Mark a long-running row as timed out. The worker thread continues
    until it returns naturally (Python can't safely kill threads); the
    DB state at least reflects the timeout for the operator."""
    ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn() as c:
        c.execute(
            "UPDATE runs SET status='timeout', ended_at=?, "
            "error_message=? WHERE id=? AND status='running'",
            (ended, f"exceeded timeout_seconds={timeout_seconds}", run_id),
        )


def sweep_interrupted_runs() -> int:
    """Mark any run still in status='running' as 'interrupted'.

    Called at process startup: if PM2 killed the previous scheduler mid-job,
    the runs row was left at status='running' with no ended_at. That row will
    show "currently running" on the dashboard forever and trip the health
    check's stuck-run detector. This sweep is safe to call unconditionally
    because by the time it runs the process is fresh — no real in-flight
    jobs can exist yet.
    """
    ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn() as c:
        cur = c.execute(
            "UPDATE runs SET status='interrupted', ended_at=?, "
            "error_message='process restarted before run completed' "
            "WHERE status='running'",
            (ended,),
        )
        return cur.rowcount


def get_enabled_override(name: str) -> Optional[bool]:
    with conn() as c:
        cur = c.execute("SELECT enabled FROM job_settings WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            return None
        return bool(row["enabled"])


def set_enabled_override(name: str, enabled: bool) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO job_settings (name, enabled, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (name, 1 if enabled else 0, now_iso()),
        )


def clear_enabled_override(name: str) -> None:
    """Drop any DB override so the job reverts to its JOB-dict default."""
    with conn() as c:
        c.execute("DELETE FROM job_settings WHERE name = ?", (name,))


def get_run(run_id: int) -> Optional[dict]:
    with conn() as c:
        cur = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# ── External jobs ──────────────────────────────────────────────────────────
def upsert_external_job(payload: dict) -> None:
    """Register or refresh an external job. Schema is append-only: future
    versions of the framework may add columns, but the existing column
    semantics never change. Unknown payload keys are dropped silently."""
    name = payload.get("name")
    if not name:
        return
    now = now_iso()
    with conn() as c:
        cur = c.execute("SELECT first_seen_at FROM external_jobs WHERE name = ?", (name,))
        existing = cur.fetchone()
        first_seen = existing["first_seen_at"] if existing else now
        c.execute(
            "INSERT INTO external_jobs "
            "(name, tool, cron, timezone, url, owner, description, "
            " tracker_version, first_seen_at, last_registered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  tool = excluded.tool, "
            "  cron = excluded.cron, "
            "  timezone = excluded.timezone, "
            "  url = excluded.url, "
            "  owner = excluded.owner, "
            "  description = excluded.description, "
            "  tracker_version = excluded.tracker_version, "
            "  last_registered_at = excluded.last_registered_at",
            (
                name,
                payload.get("tool"),
                payload.get("cron"),
                payload.get("timezone"),
                payload.get("url"),
                payload.get("owner"),
                payload.get("description"),
                payload.get("tracker_version"),
                first_seen,
                now,
            ),
        )


def list_external_jobs() -> list[dict]:
    with conn() as c:
        cur = c.execute("SELECT * FROM external_jobs ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


def insert_heartbeat_run(
    job_name: str,
    *,
    status: str,
    duration_ms: Optional[int],
    started_at: Optional[str],
    ended_at: Optional[str],
    error_message: Optional[str],
    summary: Optional[str],
) -> int:
    """Insert a finalized run row from an external heartbeat. Unlike
    cron/webhook runs (which insert as 'running' then update), heartbeat
    runs land already-completed — the work happened in the tool, not here."""
    started = started_at or now_iso()
    ended = ended_at or now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs (job_name, trigger_kind, started_at, ended_at, "
            " status, duration_ms, stdout, stderr, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_name,
                "external",
                started,
                ended,
                status,
                duration_ms,
                _truncate(_redact(summary)) if summary else None,
                None,
                _truncate(_redact(error_message)) if error_message else None,
            ),
        )
        return cur.lastrowid
