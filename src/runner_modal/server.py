"""Modal Server for the Runner webhook control plane (not exported)."""

from __future__ import annotations

import os
import threading
import time

import modal
import uvicorn

# Modal identifies Servers by the class ``__name__`` (no parameters allowed).
# One ``Runner.create`` per App — the runner name is passed via ``RUNNER_MODAL_NAME``.
SERVER_CLASS_NAME = "GitHubServer"


class GitHubServer:
    """HTTP control plane started via ``@app.server``."""

    @modal.enter()
    def start(self) -> None:
        from runner_modal.api import App

        name = os.environ.get("RUNNER_MODAL_NAME", "")
        if not name:
            raise RuntimeError("RUNNER_MODAL_NAME is not set")

        fastapi_app = App.for_runner(name)
        config = uvicorn.Config(
            fastapi_app, host="0.0.0.0", port=8000, log_level="info"
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            daemon=True,
            name=f"runner-uvicorn-{name}",
        )
        thread.start()
        deadline = time.time() + 30
        while not server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start within 30s")
            time.sleep(0.05)
        self.http_server = server
        self.http_thread = thread
