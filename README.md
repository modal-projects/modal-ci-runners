# runner-modal

Self-hosted [GitHub Actions](https://docs.github.com/en/actions) runners on [Modal](https://modal.com).

One public type — `Runner` — registers a webhook control plane. Jobs run as Modal Sandboxes via `python -m runner_modal.job`. The API mirrors Modal entities (`Runner` ≈ Volume + Server; `Runner.Job` ≈ Sandbox).

## Requirements

- Python >= 3.12
- A [Modal](https://modal.com) account and CLI
- A named Modal Secret with `GITHUB_TOKEN` and `WEBHOOK_SECRET`

## Installation

From this repo (uv project — deploy and `modal run` from the repo root):

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

| Key | Where | Purpose |
|-----|--------|---------|
| `GITHUB_TOKEN` | Job Sandbox (`Job.create(secret=…)`) | Mint JIT inside the job |
| `WEBHOOK_SECRET` | Server (`Runner.create(secret=…)`) | HMAC on `POST /github` |

`secret=` must be a **named** Secret (`modal.Secret.from_name`). There are no credential env fallbacks.

### 2. Deploy the control plane

From the repo root so `uv_sync()` / `add_local_python_source("runner_modal")` see the project:

```bash
modal deploy examples/github_webhook.py
```

```python
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
    idle_timeout=900,
)
```

`Runner.create` builds and publishes the job Sandbox image as a named Image (`"{name}-job"`) via `Image.build` + `publish`, and persists `job_image_name` next to `secret_name`. The Server later uses `Image.from_name` — it does not rebuild local mounts.

Call `Runner.create` **once per App**. The Server class is always `GitHubServer`; identity is `RUNNER_MODAL_NAME` + persisted meta.

### 3. Point GitHub at the webhook

```python
runner = Runner.from_name("acme")
print(runner.url)  # None until the Server is ready
# Webhook: {runner.url}/github
```

Workflow labels must include every pool label plus a unique pin:

```yaml
runs-on:
  - self-hosted
  - modal
  - acme
  - job-${{ github.run_id }}-${{ github.job }}
```

## How it works

```text
GitHub  --workflow_job-->  GitHubServer
                              │
                       claim delivery ID
                              │
                       Job.create(secret=from_name(secret_name),
                                  image=from_name(job_image_name))
                              │
                       python -m runner_modal.job
                              │
                       mint JIT from GITHUB_TOKEN
                              │
                       ./run.sh --jitconfig …
```

| Piece | Modal object | Role |
|-------|--------------|------|
| Meta | Dict `{name}-runner-meta` | Labels, capacity, `secret_name`, `job_image_name`, defaults |
| Deliveries | Dict `{name}-runner-deliveries` | Claim **before** create |
| Cache | Volume `{name}-cache` → `/cache` | Optional scratch — **not** `actions/cache` |
| Control plane | Server `GitHubServer` | HMAC, admission, create/cancel |
| Credentials | Named `modal.Secret` | Required on `Runner.create` and `Job.create` |
| Job | Sandbox | `python -m runner_modal.job` |

Paths: webhook ([`examples/github_webhook.py`](examples/github_webhook.py)) and imperative ([`examples/imperative_create.py`](examples/imperative_create.py)).

## Performance

People evaluating CI runners usually care about a small set of metrics — not package install times or pytest duration (those are the job’s problem).

| Metric | Question it answers |
|--------|---------------------|
| **Queue / wait** | How long from `queued` until the first workflow step runs? |
| **Time to runner online** | How long until GitHub sees an `online` runner for this job? |
| **Cold vs warm** | Is the first job after deploy/idle much slower than the next? |
| **Concurrency** | Can *N* jobs start together, or do they serialize behind capacity? |
| **Idle cost** | What do you pay when nothing is running? |

**Queue** is `queued → first step`. Workflow duration after that is mostly independent of the runner host.

Critical path: admit → `Sandbox.create` → mint JIT in the job → runner online → first step → done.

### vs GitHub-hosted

Same metric: time until a runner is ready. Chart values are order-of-magnitude samples (warm control plane), not an SLA.

![Time to runner ready](docs/time-to-ready.png)

| Path | Ready in | What we timed |
|------|----------|----------------|
| **GitHub-hosted** (`ubuntu-latest`) | **~3 s** | `run.created` → `job.started` |
| **runner-modal** (warm Named Image) | **~4 s** | webhook → GitHub runner `online` |
| **runner-modal** (cold image rebuild) | **~30 s** | rebuilding Actions runner layers on the request |

Warm Modal lands near GitHub-hosted. `Runner.create` publishes `{name}-job` so the webhook path uses `Image.from_name`.

### Where warm queue time goes

![Warm queue budget](docs/queue-budget.png)

| Stage | Role |
|-------|------|
| Admit / claim | Webhook HMAC, label check, delivery idempotency |
| Sandbox create | Schedule the job container |
| JIT mint (in job) | GitHub `generate-jitconfig` from Secret |
| Runner online | Actions `run.sh` registers with GitHub |

### Concurrency / burst

With headroom under `max_concurrent`, jobs start as parallel Sandboxes — time-to-ready stays flat as N grows. If effective concurrency is 1, wait stacks.

![Concurrency / burst](docs/concurrency.png)

`max_concurrent` is a **soft** list-then-create check (TOCTOU). Concurrent creators can briefly overshoot.

### Idle cost

GitHub-hosted has no idle bill. Modal keeps a small control plane (`min_containers`); each job is an ephemeral Sandbox that stops with the run.

![Idle vs active cost](docs/idle-cost.png)

Regenerate charts:

```bash
uv run --group dev python scripts/charts.py
```

Profile the hot path:

```bash
modal run scripts/profile.py --with-job --with-online --repository owner/repo
```

Set `RUNNER_MODAL_PROFILE=1` on the Server to print stage timers in Modal logs.

## Secrets

- **Required, explicit.** Pass `secret=` on create — never rely on ambient `GITHUB_TOKEN` / `WEBHOOK_SECRET` in the parent process.
- **Named only.** `Secret.from_name(...)` so the name can be persisted and reattached for webhook jobs.
- **Roles.** Same Secret usually holds both keys: Server uses `WEBHOOK_SECRET`; each Job is given the Secret for `GITHUB_TOKEN` mint.
- **No tokens in `env=`.** Job inputs use `RUNNER_JOB_SPEC` JSON; JIT never goes in plain env.

## Label admission

Webhook hydrates Runner meta, then admits only when:

1. Job labels include `self-hosted`
2. Pool labels are non-empty
3. `pool ⊆ job.labels`

Empty pool → admit nothing. Extra labels (e.g. unique `job-…`) are fine.

## Security notes

Self-hosted runners execute workflow code with access to the runner environment. Treat fork PRs and untrusted workflows as hostile. Prefer private repos or trusted branches before exposing a shared org webhook.

## Imperative jobs

`Job.create` always needs an explicit `secret=` (even if you used `Runner.objects.create` without a webhook deploy):

```python
gh = modal.Secret.from_name("github-runner", required_keys=["GITHUB_TOKEN"])

job = Runner.Job.create(
    runner,
    repository="acme/api",
    labels=["modal", "acme", "job-1"],
    secret=gh,
    gpu="t4",
)
job.wait()
```

Docker / VM (no GPU):

```python
Runner.Job.create(
    runner,
    repository="acme/api",
    secret=gh,
    experimental_options={"vm_runtime": True},
)
```

## API map

| Call | Modal analogue | What |
|------|----------------|------|
| `Runner.create(app, name, secret=…)` | `@app.server` | Definition-time control plane |
| `Runner.from_name` / `objects` / `ephemeral` | `Volume.*` | Named handle + admin |
| `runner.url` | `Server.get_url()` | `str \| None` until ready |
| `Runner.Job.create(…, repository=…, secret=…)` | `Sandbox.create` | Eager job Sandbox |
| `Runner.Job.from_id` / `from_name` / `wait` | `Sandbox.*` | Lookup / block |
| `POST {url}/github` | — | Claim → create → 200 / 204 / 5xx |

Job resources are Modal-flat: `cpu`, `memory`, `gpu`, `experimental_options`.

## Errors vs soft absence

Soft miss → `None` or HTTP 204. Failures raise.

| Situation | Result |
|-----------|--------|
| Undeployed / no URL yet | `runner.url is None` |
| Ignored webhook | HTTP 204 |
| Bad name / repo | `ValueError` |
| Missing Runner meta | `LookupError` |
| Bad HMAC / empty webhook secret | `AuthError` |
| At `max_concurrent` | `ConcurrencyLimitError` |
| Delivery in progress / capacity | HTTP 503 |
| `job.wait(timeout=…)` exceeded | `JobTimeoutError` |

`has_capacity()` is soft list-then-create — not a lock; concurrent creators can overshoot.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src/runner_modal tests examples
uv run ruff format --check src/runner_modal tests examples
uv run ty check
```

Layout: `src/runner_modal/` (`runner`, `job`, `api`, `server`, `exceptions`). Conventions: [`AGENTS.md`](AGENTS.md).

## License

No license file yet — treat as private until one is added.
