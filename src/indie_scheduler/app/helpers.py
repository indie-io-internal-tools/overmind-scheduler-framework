"""Helpers exposed to job authors.

Importable from a job module via:
    from app.helpers import retry_with_backoff
"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class RetryError(RuntimeError):
    """Raised when retry_with_backoff exhausts all attempts."""


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    log: Callable[[str], None] = print,
) -> T:
    """Call fn() with exponential backoff + jitter.

    Retries up to `attempts` times when fn raises any exception in `retry_on`.
    Delay is `min(base_seconds * 2**(i-1), max_seconds) * uniform(0.5, 1.5)`.
    Re-raises the final exception wrapped in RetryError on exhaustion.

    Example:
        from app.helpers import retry_with_backoff
        body = retry_with_backoff(
            lambda: httpx.get(url, timeout=30).json(),
            attempts=4,
        )
    """
    last_exc: BaseException | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as e:
            last_exc = e
            if i == attempts:
                break
            delay = min(base_seconds * (2 ** (i - 1)), max_seconds)
            delay *= random.uniform(0.5, 1.5)
            log(f"[retry] attempt {i}/{attempts} failed: {type(e).__name__}: {e}; sleeping {delay:.1f}s")
            time.sleep(delay)
    raise RetryError(f"exhausted {attempts} attempts; last error: {last_exc}") from last_exc
