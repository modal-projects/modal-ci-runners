"""Public exception types — use with None returns idiomatically.

Absence / soft miss → ``None`` (e.g. ``runner.url``, ignored webhook, missing delivery).
Real failures → raise (auth, timeout, concurrency, bad arguments via ``ValueError`` /
``LookupError``).
"""

from __future__ import annotations


class RunnerError(Exception):
    """Base error for runner_modal."""


class AuthError(RunnerError):
    """Missing or invalid GitHub credentials / webhook signature."""


class ConcurrencyLimitError(RunnerError):
    """``max_concurrent`` reached when creating a job."""


class JobTimeoutError(RunnerError):
    """``Runner.Job.wait`` exceeded its timeout."""
