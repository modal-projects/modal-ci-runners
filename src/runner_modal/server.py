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
        from runner_modal.api import WebhookApp

        try:
            name = os.environ["RUNNER_MODAL_NAME"]
        except KeyError as e:
            raise RuntimeError("RUNNER_MODAL_NAME is required") from e
        try:
            webhook_secret = os.environ["WEBHOOK_SECRET"]
        except KeyError as e:
            raise RuntimeError(
                "WEBHOOK_SECRET is required (modal.Secret on Runner.create)"
            ) from e

        fastapi_app = WebhookApp.for_runner(name, webhook_secret=webhook_secret)
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
