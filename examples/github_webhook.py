"""GitHub Actions runners on Modal — Runner.create + deploy.

Setup:
  modal secret create github-runner \\
    GITHUB_TOKEN=ghp_xxx \\
    WEBHOOK_SECRET=$(openssl rand -hex 32)

  # from the repo root (uv project):
  modal deploy examples/github_webhook.py

Webhook: ``{runner.url}/github``
runs-on: [self-hosted, modal, acme, \"job-${{ github.run_id }}-${{ github.job }}\"]
"""

from __future__ import annotations

import modal

from runner_modal import Runner

app = modal.App("acme-ci")
gh = modal.Secret.from_name("github-runner")

runner = Runner.create(
    app=app,
    name="acme",
    image=modal.Image.debian_slim(python_version="3.12").uv_sync(),
    secrets=[gh],
    compute_region="us-east",
    labels=["self-hosted", "modal", "acme"],
    max_concurrent=20,
    cache=True,
)
