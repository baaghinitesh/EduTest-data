"""
run_budget.py — Enforces a hard wall-clock budget for each generator run.

Prevents infinite or multi-hour runs while allowing full key rotation,
retries, and clean draft saving before the GitHub Actions job timeout.
"""
import os
import time


class RunTimeoutError(Exception):
    """Raised when the run reaches its hard time budget."""
    pass


def _get_max_minutes():
    raw = os.environ.get("MAX_RUN_MINUTES", "").strip()
    if raw:
        try:
            return max(5.0, min(float(raw), 85.0))
        except ValueError:
            pass
    return 75.0  # 1.25 hours default (safely inside a 90m hard job timeout)


_START_TIME = time.monotonic()
MAX_RUN_SECONDS = _get_max_minutes() * 60.0
_RESERVE_FOR_COMMIT_SECS = 15.0


def elapsed_seconds():
    return time.monotonic() - _START_TIME


def remaining_seconds():
    return max(0.0, MAX_RUN_SECONDS - elapsed_seconds())


def is_expired(min_needed=0.0):
    return remaining_seconds() <= (_RESERVE_FOR_COMMIT_SECS + min_needed)


def check_budget(context="run", min_needed=0.0):
    if is_expired(min_needed=min_needed):
        mins = elapsed_seconds() / 60.0
        raise RunTimeoutError(
            f"Hard time budget reached ({mins:.1f}m elapsed) during {context}"
        )


def safe_sleep(seconds, context="sleep"):
    """
    Sleep up to `seconds`, aborting immediately if the run budget is reached
    or would be exceeded by sleeping.
    """
    if seconds <= 0:
        check_budget(context)
        return
    rem = remaining_seconds() - _RESERVE_FOR_COMMIT_SECS
    if rem <= 1.0:
        check_budget(context)
        return
    if seconds > rem:
        time.sleep(min(rem, 1.0))
        raise RunTimeoutError(
            f"Hard time budget reached before {seconds:.0f}s {context} "
            f"(only {rem:.0f}s left)"
        )
    time.sleep(seconds)
    check_budget(context)
