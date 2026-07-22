# runner-modal

Self-hosted [GitHub Actions](https://docs.github.com/en/actions) runners on [Modal](https://modal.com) — one public type, Modal-shaped DX.

Register a webhook control plane with `Runner.create`, deploy it, point GitHub at `{url}/github`, and jobs spawn as Modal Sandboxes. No custom webhook Functions as the primary API.

## Features

- **Modal twin API** — `Runner` ≈ Volume + Server; `Runner.Job` ≈ Sandbox (`create` / `from_name` / `from_id` / `wait`)
- **GitHub webhook** — HMAC verify, label admission, delivery claim, cancel terminate → 200 / 204 / 5xx
- **Flat Sandbox resources** — `cpu`, `memory`, `gpu`, `experimental_options={"vm_runtime": True}`
- **Shared `/cache` Volume** — optional mount across jobs (`cache=True`); not the GitHub Actions cache service
- **uv-native images** — example control plane via `Image.uv_sync()`; published install via `uv_pip_install("runner-modal")`

## Requirements

- Python >= 3.12
- A [Modal](https://modal.com) account and CLI (`modal`)
- A GitHub token that can mint JIT runner configs, plus a webhook secret

## Installation

```bash
uv add runner-modal
# or from this repo:
uv sync
```

## Usage

### 1. Create a Modal Secret

```bash
modal secret create github-runner \
  GITHUB_TOKEN=ghp_xxx \
  WEBHOOK_SECRET=$(openssl rand -hex 32)
```

`GITHUB_TOKEN` is used for JIT mint in the **process that calls** `Job.create` (the Server, after deploy). `WEBHOOK_SECRET` verifies `POST /github`.

### 2. Deploy the control plane

From the repo root (so `uv_sync()` sees the project):

```bash
modal deploy examples/github_webhook.py
```

```python
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
    idle_timeout=900,
)
```

Call `Runner.create` **once per App**. The Modal Server class is always `GitHubServer`; the runner `name` identifies Dict/Volume/delivery state via `RUNNER_MODAL_NAME`.

Pool `labels` are an admission filter: the webhook only creates a Job when every pool label appears on the GitHub job. Empty pool labels admit all jobs.

`cache=True` mounts a shared Modal Volume at `/cache` on job Sandboxes for optional scratch/tooling reuse. It does **not** implement GitHub's `actions/cache` service.

### 3. Wire GitHub

After deploy:

```python
runner = Runner.from_name("acme")
print(runner.url)  # None until the Server is ready
# Webhook: {runner.url}/github
```

In the workflow:

```yaml
runs-on: [self-hosted, modal, acme, "job-${{ github.run_id }}-${{ github.job }}"]
```

### Imperative job (no webhook)

See [`examples/imperative_create.py`](examples/imperative_create.py). JIT mint still needs `GITHUB_TOKEN` in the **local** process — Job `secrets=` do not authenticate parent-side mint.

```python
job = Runner.Job.create(
    runner,
    repository="acme/api",
    labels=["modal", "acme", "job-1"],
    gpu="t4",
)
job.wait()
```

Docker/VM jobs:

```python
Runner.Job.create(
    runner,
    repository="acme/api",
    experimental_options={"vm_runtime": True},
)
```

## API map

| Call | Modal analogue | What |
|------|----------------|------|
| `Runner.create(app, name, …)` | `@app.server` | Definition-time control plane |
| `Runner.from_name` / `objects` / `ephemeral` | `Volume.*` | Named handle + admin |
| `runner.url` | `Server.get_url()` | `str \| None` until ready |
| `Runner.Job.create(runner, …)` | `Sandbox.create` | Eager job Sandbox |
| `Runner.Job.from_id` / `from_name` | `Sandbox.from_*` | Lookup |
| `POST {url}/github` | — | Claim delivery → create → 200 / 204 / 5xx |

## Errors vs `None`

Soft absence returns `None` (or HTTP 204); failures raise.

| Situation | Result |
|-----------|--------|
| Undeployed / no URL yet | `runner.url is None` |
| Ignored webhook | HTTP 204 |
| Bad name / repo / missing Job args | `ValueError` |
| Missing named Runner meta | `LookupError` |
| Bad HMAC / missing token | `AuthError` |
| At `max_concurrent` | `ConcurrencyLimitError` (soft — see below) |
| Delivery in progress / capacity | HTTP 503 |
| `job.wait(timeout=…)` exceeded | `JobTimeoutError` |

`has_capacity()` is a soft list-then-create check — concurrent creators can overshoot. It is not a linearizable lock.

## Secrets

| Key | Where | Use |
|-----|--------|-----|
| `GITHUB_TOKEN` | Calling process env (Server Secret → env after deploy) | JIT mint |
| `WEBHOOK_SECRET` | Server env | HMAC on `/github` |
| JIT config | `modal.Secret` on the Job Sandbox | Runner binary (`MODAL_RUNNER_JIT`) — never put tokens/JIT in Sandbox `env=` |

## Development

```bash
uv sync
uv run pytest
uv run ruff check src/runner_modal tests examples
uv run ruff format --check src/runner_modal tests examples
uv run ty check
```

Package layout: `src/runner_modal/` (`runner`, `api`, `server`, `exceptions`); one unit test file per module under `tests/unit/`. Agent/contributor conventions: [`AGENTS.md`](AGENTS.md).

## License

No license file yet — treat as private until one is added.
