"""Imperative Runner.Job.create — one job Sandbox without the webhook.

Requires a local GITHUB_TOKEN (JIT mint runs in this process). Sandbox
``secrets=[gh]`` alone does not authenticate mint.

  export GITHUB_TOKEN=ghp_...
  # optional: modal secret for the job container if your workflow needs it
  modal secret create github-runner GITHUB_TOKEN=ghp_...

Run (from repo root):
  modal run examples/imperative_create.py --repository owner/repo
"""

from __future__ import annotations

import time

import modal

from runner_modal import Runner

app = modal.App("runner-modal-example")

Runner.objects.create(
    "demo",
    allow_existing=True,
    labels=["modal", "demo"],
    max_concurrent=5,
)


@app.local_entrypoint()
def main(repository: str = "octocat/Hello-World") -> None:
    parent = modal.App.lookup("runner-modal-example", create_if_missing=True)
    runner = Runner.from_name("demo")

    unique = f"manual-{int(time.time())}"
    job = Runner.Job.create(
        runner,
        app=parent,
        repository=repository,
        labels=["modal", "demo", unique],
        cpu=2.0,
        region="us-east",
        timeout=30 * 60,
        name=unique,
    )
    print(f"spawned job object_id={job.object_id}")
    code = job.wait(timeout=30 * 60)
    print(f"exit code={code}")
