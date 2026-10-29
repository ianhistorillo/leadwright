"""Retry helpers with exponential backoff."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

# Transient HTTP statuses we retry by default.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def compute_backoff_seconds(
    attempt: int,
    *,
    base_delay: float = 0.5,
    max_delay: float = 60.0,
    retry_after: float | None = None,
) -> float:
    """
    Compute delay before the next retry.

    Prefer an explicit Retry-After value when provided; otherwise use
    exponential backoff with jitter.
    """
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), max_delay)

    delay = min(max_delay, base_delay * (2**attempt))
    jitter = random.uniform(0, delay * 0.25)
    return delay + jitter


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    should_retry: Callable[[BaseException], bool],
    get_retry_after: Callable[[BaseException], float | None] | None = None,
    base_delay: float = 0.5,
    max_delay: float = 60.0,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """
    Execute an async operation with exponential backoff retries.

    `max_retries` is the number of *retries* after the first attempt
    (total attempts = max_retries + 1).
    """
    attempt = 0
    while True:
        try:
            return await operation()
        except BaseException as exc:
            if attempt >= max_retries or not should_retry(exc):
                raise
            retry_after = get_retry_after(exc) if get_retry_after else None
            delay = compute_backoff_seconds(
                attempt,
                base_delay=base_delay,
                max_delay=max_delay,
                retry_after=retry_after,
            )
            if on_retry:
                on_retry(attempt + 1, exc, delay)
            await asyncio.sleep(delay)
            attempt += 1
