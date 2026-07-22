"""Tests for runner_modal.exceptions."""

from __future__ import annotations

from runner_modal.exceptions import (
    AuthError,
    ConcurrencyLimitError,
    JobTimeoutError,
    RunnerError,
)


def test_exception_hierarchy() -> None:
    assert issubclass(AuthError, RunnerError)
    assert issubclass(ConcurrencyLimitError, RunnerError)
    assert issubclass(JobTimeoutError, RunnerError)
