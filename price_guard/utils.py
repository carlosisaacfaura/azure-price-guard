"""Shared utilities: logging, waits, retries and parsers.

Everything that more than one module needs lives here so the API client, the
Page Objects and the reporting layer all behave the same way.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

LOGGER_NAME = "price_guard"

_PRICE_RE = re.compile(r"[-+]?\d{1,3}(?:[ , ]\d{3})*(?:[.,]\d+)?|[-+]?\d*[.,]?\d+")


def utc_now_iso() -> str:
    """Timestamp used for every snapshot row. Always UTC, always ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    """Short, sortable identifier for one execution of the framework."""
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def setup_logging(log_path: Path, verbose: bool = True) -> logging.Logger:
    """Configure the framework logger: file + console, one deliverable log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def retry(
    func: Callable[[], T],
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    description: str = "operation",
) -> T:
    """Retry with linear backoff. Used for both HTTP calls and UI waits."""
    log = get_logger("retry")
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except exceptions as exc:  # noqa: PERF203 - explicit retry loop
            last = exc
            if attempt == attempts:
                break
            wait = backoff_seconds * attempt
            log.warning(
                "%s failed (attempt %s/%s): %s - retrying in %.1fs",
                description, attempt, attempts, exc, wait,
            )
            time.sleep(wait)
    assert last is not None
    raise last


def wait_until(
    predicate: Callable[[], bool],
    timeout_seconds: float,
    poll_seconds: float = 0.25,
    description: str = "condition",
) -> bool:
    """Explicit wait. The single place the framework blocks on a condition."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_seconds)
    get_logger("wait").warning("Timed out after %ss waiting for %s", timeout_seconds, description)
    return False


def parse_price(text: str | float | int | None) -> float | None:
    """Turn a UI price string into a float.

    Handles '$0.096/hour', '0,096 EUR', '1 234.56', 'N/A' and None.
    Returns None when no number can be found - callers must not guess.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = text.strip()
    if not cleaned:
        return None
    match = _PRICE_RE.search(cleaned.replace(" ", " "))
    if not match:
        return None
    token = match.group(0).replace(" ", "")
    # 1,234.56 -> 1234.56 ; 0,096 -> 0.096
    if "," in token and "." in token:
        token = token.replace(",", "")
    elif "," in token:
        token = token.replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


def pct_change(old: float, new: float) -> float | None:
    """Relative change in percent. None when the baseline is zero."""
    if old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def render_template(text: str, context: dict[str, str]) -> str:
    """Fill `{placeholders}` in a data-driven search path step value."""
    out = text
    for key, value in context.items():
        out = out.replace("{" + key + "}", str(value))
    return out
