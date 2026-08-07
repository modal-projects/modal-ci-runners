"""StageClock — env-gated stage timing for profile-driven optimization (not exported)."""

from __future__ import annotations

import os
import time
from typing import Self


class StageClock:
    """Times one named stage when ``RUNNER_MODAL_PROFILE`` is set.

    Usage::

        with StageClock("hydrate"):
            runner.hydrate()

    ``ms`` is set on exit (0 when profiling is off). The harness in
    ``scripts/profile.py`` reuses this entity — do not add a parallel timer.
    """

    ENV = "RUNNER_MODAL_PROFILE"

    def __init__(self, name: str) -> None:
        self.name = name
        self.started: float | None = None
        self.ms = 0.0
        flag = os.environ.get(self.ENV, "").strip().lower()
        self.active = flag not in ("", "0", "false", "no")

    def __enter__(self) -> Self:
        if self.active:
            self.started = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.active and self.started is not None:
            self.ms = (time.perf_counter() - self.started) * 1000
            print(f"{self.ms:8.1f} ms  stage={self.name}", flush=True)
