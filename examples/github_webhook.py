"""GitHub Actions runners on Modal — Runner.create + deploy.

Setup:
  modal secret create github-runner \\
    GITHUB_TOKEN=ghp_xxx \\
    WEBHOOK_SECRET=$(openssl rand -hex 32)

  # from the repo root (uv project — builds/publishes named job image):
  modal deploy examples/github_webhook.py

Webhook: ``{runner.url}/github``
runs-on: [self-hosted, modal, acme, \"job-${{ github.run_id }}-${{ github.job }}\"]

``secret=`` must be a named Modal Secret with ``GITHUB_TOKEN`` (Job mint) and
``WEBHOOK_SECRET`` (Server HMAC). Job Sandboxes use the named Image
``{name}-job`` published at create (``Image.from_name`` on the webhook path).
"""

from __future__ import annotations

import modal

from runner_modal import Runner

app = modal.App("acme-ci")
gh = modal.Secret.from_name(
    "github-runner",
    required_keys=["GITHUB_TOKEN", "WEBHOOK_SECRET"],
)

runner = Runner.create(
    app=app,
    name="acme",
    secret=gh,
    compute_region="us-east",
    labels=["self-hosted", "modal", "acme"],
    max_concurrent=20,
    cache=True,
    idle_timeout=900,
    env={"RUNNER_MODAL_PROFILE": "1"},
)
