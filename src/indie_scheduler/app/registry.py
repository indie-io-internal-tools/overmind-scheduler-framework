"""Discover jobs from the jobs/ directory.

A job is any module under jobs/ that exposes a top-level `JOB` dict and either:
  - run(ctx)               for cron jobs
  - handle_webhook(req)    for webhook jobs

Reload is by process restart — jobs aren't expected to change at runtime.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from cron_descriptor import get_description

from . import config, db


@dataclass
class Job:
    name: str
    description: str
    owner: str
    trigger: str  # "cron" | "webhook"
    cron: Optional[str]
    enabled_default: bool  # from the JOB dict in the .py file
    enabled: bool          # effective state — override if present, else default
    module_path: Path
    run: Optional[Callable]
    handle_webhook: Optional[Callable]
    # Optional knobs from the JOB dict.
    misfire_grace_seconds: Optional[int] = None
    max_instances: int = 1
    timeout_seconds: Optional[int] = None
    # Webhook auth mode: "internal" (handler verifies), "shared_secret"
    # (framework checks X-Scheduler-Secret header), "open" (explicit no-auth).
    # Required for webhook jobs — missing means framework refuses to dispatch.
    webhook_auth: Optional[str] = None

    def is_cron(self) -> bool:
        return self.trigger == "cron"

    def is_webhook(self) -> bool:
        return self.trigger == "webhook"

    def schedule_human(self) -> str:
        """Human-readable schedule for the UI. Webhook jobs say 'on webhook'."""
        if self.is_webhook():
            return "on webhook"
        if not self.cron:
            return "(no schedule)"
        try:
            return get_description(self.cron)
        except Exception:
            return self.cron


_registry: dict[str, Job] = {}


def _import_module(path: Path):
    module_name = f"scheduler_jobs.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load() -> dict[str, Job]:
    _registry.clear()
    if not config.JOBS_DIR.exists():
        return _registry

    for path in sorted(config.JOBS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = _import_module(path)
        except Exception as e:
            print(f"[registry] failed to load {path.name}: {e}")
            continue

        meta = getattr(module, "JOB", None)
        if not isinstance(meta, dict):
            continue
        name = meta.get("name") or path.stem
        trigger = meta.get("trigger", "cron")
        if trigger not in ("cron", "webhook"):
            print(f"[registry] {name}: invalid trigger {trigger!r}, skipping")
            continue

        run_fn = getattr(module, "run", None)
        webhook_fn = getattr(module, "handle_webhook", None)

        if trigger == "cron" and run_fn is None:
            print(f"[registry] {name}: cron job missing run(); skipping")
            continue
        if trigger == "webhook" and webhook_fn is None:
            print(f"[registry] {name}: webhook job missing handle_webhook(); skipping")
            continue

        default_enabled = bool(meta.get("enabled", True))
        override = db.get_enabled_override(name)
        effective = default_enabled if override is None else override

        _registry[name] = Job(
            name=name,
            description=meta.get("description", ""),
            owner=meta.get("owner", "unknown"),
            trigger=trigger,
            cron=meta.get("cron"),
            enabled_default=default_enabled,
            enabled=effective,
            module_path=path,
            run=run_fn,
            handle_webhook=webhook_fn,
            misfire_grace_seconds=meta.get("misfire_grace_seconds"),
            max_instances=int(meta.get("max_instances", 1)),
            timeout_seconds=meta.get("timeout_seconds"),
            webhook_auth=meta.get("auth"),
        )
        if trigger == "webhook":
            mode = meta.get("auth")
            if mode not in {"internal", "shared_secret", "open"}:
                print(
                    f"[registry] {name}: webhook job missing required JOB['auth'] "
                    f"(got {mode!r}). Set to 'internal', 'shared_secret', or 'open'."
                )
    return _registry


def get(name: str) -> Optional[Job]:
    return _registry.get(name)


def all_jobs() -> list[Job]:
    return list(_registry.values())
