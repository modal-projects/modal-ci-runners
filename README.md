# Named CI runners on Modal

**runner-modal** follows Modal SDK shapes: one public type `Runner` (Volume-like lookup + App Server registration) and `Runner.Job` (Sandbox twin).

| Call | Modal analogue | What |
|------|----------------|------|
| `Runner.create(app, name, …)` | `@app.server` registration | Definition-time control plane (not an eager Sandbox) |
| `Runner.from_name` / `objects` / `ephemeral` | `Volume.from_name` / `objects` / `ephemeral` | Named handle + admin |
| `runner.url` | `Server.get_url()` | `str \| None` until deploy |
| `Runner.Job.create(runner, …)` | `Sandbox.create` | Eager job Sandbox (`sb-…`) |
| `Runner.Job.from_id` / `from_name` | `Sandbox.from_id` / `from_name` | Lookup |
| `POST {url}/github` | — | Claim delivery → job create → 200 / 204 ignored / 5xx |

Job resources are Modal-flat: `cpu=`, `memory=`, `gpu=`, `experimental_options={"vm_runtime": True}` (Docker/VM; GPU unsupported on VM).

## Images

| Surface | Image |
|---------|-------|
| Example control plane (`examples/github_webhook.py`) | `Image.debian_slim(...).uv_sync()` from the repo root |
| Default `Runner.control_plane_image()` | `uv_pip_install("runner-modal")` (published package) |
| Job Sandboxes | `Runner.default_image()` (gVisor) or `Runner.docker_image()` when `vm_runtime` |

Call `Runner.create` once per App — Modal Servers have no parameters, so the
control-plane class is always `GitHubServer`; the runner `name` identifies
Dict/Volume/delivery state via `RUNNER_MODAL_NAME`.

## Errors vs ``None``

Idiomatic Python: soft absence returns ``None``; failures raise.

| Situation | Result |
|-----------|--------|
| Undeployed / no URL yet | `runner.url is None` |
| Ignored webhook | HTTP 204 |
| Bad name / repo / missing Job args | `ValueError` |
| Missing named Runner meta | `LookupError` |
| Bad HMAC / missing token | `AuthError` |
| At `max_concurrent` on `Job.create` | `ConcurrencyLimitError` (soft — see below) |
| Delivery in progress / capacity | HTTP 503 |
| `job.wait(timeout=…)` exceeded | `JobTimeoutError` |

`has_capacity()` is a soft LBYL check (list-then-create; concurrent creators can overshoot). `Job.create` still raises if full at call time (EAFP).

## Install

```bash
uv add runner-modal
# or from this repo:
uv sync
```

## Quick start

```bash
modal secret create github-runner \
  GITHUB_TOKEN=ghp_xxx \
  WEBHOOK_SECRET=$(openssl rand -hex 32)

# deploy from the repo root (uv project → Image.uv_sync in the example)
modal deploy examples/github_webhook.py
```

```python
import modal
from runner_modal import Runner

app = modal.App("acme-ci")

runner = Runner.create(
    app=app,
    name="acme",
    secrets=[modal.Secret.from_name("github-runner")],
    compute_region="us-east",
    labels=["self-hosted", "modal", "acme"],
    max_concurrent=20,
)

# After deploy:
job = Runner.Job.create(
    runner,
    repository="acme/api",
    labels=["modal", "acme", "job-1"],
    gpu="t4",
)
print(job.object_id)
job.wait()

runner = Runner.from_name("acme")
print(runner.url)  # None until deploy
```

## Secrets

| Key | Use |
|-----|-----|
| `GITHUB_TOKEN` | JIT mint in the **calling** process (Server env via Modal Secret, or local env for imperative create) |
| `WEBHOOK_SECRET` | HMAC on `/github` |

Sandbox / Job `secrets=` are injected into the job container only — they do **not** authenticate parent-side JIT mint. Pass `jit_config=` or set `GITHUB_TOKEN` in the process that calls `Job.create`.

## Layout

```
src/runner_modal/
  runner.py      # Runner, Job
  api.py         # App, WorkflowJobEvent, Deliveries
  server.py      # GitHubServer
  exceptions.py
tests/unit/
  test_runner.py
  test_api.py
  test_server.py
  test_exceptions.py
```
