# runner-modal

Self-hosted [GitHub Actions](https://docs.github.com/en/actions) runners on [Modal](https://modal.com).

One public type — `Runner` — registers a webhook control plane. Jobs run as Modal Sandboxes with the official Actions runner binary. The API mirrors Modal entities (`Runner` ≈ Volume + Server; `Runner.Job` ≈ Sandbox).

## Requirements

- Python >= 3.12
- A [Modal](https://modal.com) account and CLI
- A GitHub token that can mint JIT runner configs
- A webhook secret for `POST /github`

## Installation

```bash
uv add runner-modal
```

From this repo:

```bash
uv sync
```

## Quick start

### 1. Create a Modal Secret

```bash
modal secret create github-runner \
  GITHUB_TOKEN=ghp_xxx \
  WEBHOOK_SECRET=$(openssl rand -hex 32)
```

| Key | Used by | Purpose |
|-----|---------|---------|
| `GITHUB_TOKEN` | Process that calls `Job.create` (the Server after deploy) | Mint JIT runner configs |
| `WEBHOOK_SECRET` | Server | Verify GitHub HMAC on `/github` |

Job Sandbox `secrets=` do **not** authenticate parent-side JIT mint. Pass the token into the Server (or local) env.

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

Call `Runner.create` **once per App**. The Modal Server class is always `GitHubServer`; the runner `name` selects Dict/Volume/delivery state via `RUNNER_MODAL_NAME`.

### 3. Point GitHub at the webhook

```python
runner = Runner.from_name("acme")
print(runner.url)  # None until the Server is ready
# Webhook URL: {runner.url}/github
```

In the workflow, include every pool label plus a unique label so one runner maps to one job:

```yaml
runs-on:
  - self-hosted
  - modal
  - acme
  - job-${{ github.run_id }}-${{ github.job }}
```

## How it works

```text
GitHub  --workflow_job-->  GitHubServer (FastAPI)
                                |
                         claim delivery ID
                                |
                         mint JIT + Sandbox.create
                                |
                         actions-runner --jitconfig ...
                                |
                         picks up the labeled job
```

| Piece | Modal object | Role |
|-------|--------------|------|
| Pool config | Dict `{name}-runner-meta` | Labels, capacity, app/server names, job defaults |
| Webhook idempotency | Dict `{name}-runner-deliveries` | Claim delivery **before** create |
| Optional scratch | Volume `{name}-cache` → `/cache` | Shared across jobs; **not** `actions/cache` |
| Control plane | Server `GitHubServer` | HMAC, admission, create/cancel |
| Job | Sandbox | Runs `/actions-runner/run.sh` |

Two paths share `Runner.Job.create`:

- **Webhook** — GitHub `queued` / `cancelled` events (production CI)
- **Imperative** — call `Job.create` from your process ([`examples/imperative_create.py`](examples/imperative_create.py))

## Label admission

After each webhook, the Server hydrates Runner meta from the Dict, then admits a job only when:

1. The job labels include `self-hosted`
2. Pool labels are non-empty
3. Every pool label appears on the job (`pool ⊆ job.labels`)

Empty pool labels admit **no** jobs. Extra job labels (for example a unique `job-…` pin) are fine.

## Security notes

Self-hosted runners execute workflow code with access to the runner environment. Treat fork PRs and untrusted workflows as hostile. Prefer private repos or trusted branches, and do not expose a shared org webhook until you understand that blast radius.

Never put `GITHUB_TOKEN`, `WEBHOOK_SECRET`, or JIT strings in Sandbox `env=`. JIT is passed as a Modal Secret (`MODAL_RUNNER_JIT`).

## Imperative jobs

JIT mint still needs `GITHUB_TOKEN` in the **local** process:

```python
job = Runner.Job.create(
    runner,
    repository="acme/api",
    labels=["modal", "acme", "job-1"],
    gpu="t4",
)
job.wait()
```

Docker / VM (no GPU):

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
| `Runner.Job.from_id` / `from_name` / `wait` | `Sandbox.*` | Lookup / block |
| `POST {url}/github` | — | Claim → create → 200 / 204 / 5xx |

Resource knobs on jobs are Modal-flat: `cpu`, `memory`, `gpu`, `experimental_options`.

## Errors vs soft absence

Soft miss → `None` or HTTP 204. Real failures raise.

| Situation | Result |
|-----------|--------|
| Undeployed / no URL yet | `runner.url is None` |
| Ignored webhook (wrong event, labels, …) | HTTP 204 |
| Bad name / repo / missing Job args | `ValueError` |
| Missing named Runner meta | `LookupError` |
| Bad HMAC / missing token | `AuthError` |
| At `max_concurrent` | `ConcurrencyLimitError` |
| Delivery in progress / capacity | HTTP 503 |
| `job.wait(timeout=…)` exceeded | `JobTimeoutError` |

`has_capacity()` is a soft list-then-create check — concurrent creators can overshoot. It is not a linearizable lock.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src/runner_modal tests examples
uv run ruff format --check src/runner_modal tests examples
uv run ty check
```

Layout: `src/runner_modal/` (`runner`, `api`, `server`, `exceptions`); one unit test file per module under `tests/unit/`. Contributor conventions: [`AGENTS.md`](AGENTS.md).

## License

No license file yet — treat as private until one is added.
