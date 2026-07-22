# Agent rules — runner-modal

Concise always-on rules for this repo. Prefer Modal SDK shapes, idiomatic Python, and library-native APIs. Do not treat this as a product README.

## Naming

- Prefer short, concrete nouns and verbs. Say what the thing **is** or **does** in one pass.
- **No underscore-prefix for “privacy.”** Types, module constants, methods, and test helpers are normal names; omit from `__all__` if internal. Do not invent `_Foo`, `_helper`, `_post`.
- **Do not shadow Modal / stdlib names.** Never call our FastAPI control plane `App` (that is `modal.App`). Prefer `WebhookApp`, `DeliveryStore`, etc.
- **Avoid redundant / encoding noise** in names: no `…Helper`, `…Manager`, `…Utils`, `…Sync` (ambiguous), `…Data`, `FooBarBazResponse` when `FooResponse` / `JitResponse` is enough. Spell units in constants (`DELIVERY_TTL_SECONDS`, not `DELIVERY_TTL_S`).
- **Tag / env keys:** name the *key* `…_TAG` (e.g. `KIND_TAG`, `POOL_TAG`) and the *value* plainly (`JOB_KIND`). Do not use `TAG_KIND` + `TAG_KIND_VALUE`.
- **Async boundary:** `async` route reads I/O; sync worker has a clear verb (`process_webhook`), not `…_sync`.
- Temporary diagnosis hooks must read as temporary (`RUNNER_DIAG_ENTRYPOINT`), not as the permanent product entrypoint name.
- Match Modal twin vocabulary for public entities (`create` / `from_name` / `objects` / `ephemeral` / `hydrate`); do not invent parallel jargon for the same idea.

## Product / DX

- Public surface is entity-based: `Runner` + nested `Runner.Job`. Mirror Modal (`create` / `from_name` / `objects` / `ephemeral`; Job ≈ Sandbox).
- Export only via `__all__`. Do not underscore-prefix types for “privacy”; omit them from `__all__` instead.
- No free public helpers (`spawn`, `parse_labels`, pools, ASGI attach helpers).
- One `Runner.create` per App. Modal Server class is stable `GitHubServer` (Servers have no parameters). Runner identity is `RUNNER_MODAL_NAME`.
- Soft capacity (`has_capacity` / `max_concurrent`) is **soft** (list-then-create TOCTOU). Document it as soft; never present it as a linearizable lock.
- **Job resources:** Modal-twin flat kwargs — `cpu`, `memory`, `gpu`, `experimental_options` (e.g. `{"vm_runtime": True}` for Docker/VM). Do **not** invent `ResourceSpec | DockerResources`, xor validators, or `isinstance` resource dispatch. Prefer `docker_image()` when `vm_runtime` is set; let Modal enforce GPU vs VM limits.

## Python construction & style

- Normal `__init__` + classmethod factories. No blocked constructors, no `object.__new__`, no `getattr` / `hasattr` / `setattr`, no dynamic class creation / `__name__` mutation.
- No process-local registries or fake idempotency caches — use Modal Dict (and claim keys properly).
- Call Modal / FastAPI / httpx with real kwargs. No build-dict / strip-Nones / `**kwargs` bags.
- Prefer library-native APIs (Modal, FastAPI, Pydantic, httpx, tenacity, uv Image methods).
- Composition over inheritance: wrap `Sandbox` / `Dict` / `Volume`; do not subclass Modal types for product API.
- Frozen Pydantic models for boundary DTOs / snapshots (`ConfigDict(frozen=True)` preferred).
- EAFP at mutation boundaries (`Job.create` raises). Optional LBYL helpers must be labeled soft.
- Break import cycles with lazy imports at Modal lifecycle boundaries (`@modal.enter`), not circular top-level imports.

## Errors, HTTP, secrets

- Soft absence → `None` or HTTP 204 (e.g. undeployed `url`, ignored webhook). Failures raise a small set: `ValueError`, `LookupError`, `AuthError`, `ConcurrencyLimitError`, `JobTimeoutError`. Base `RunnerError` is rare.
- Do not wrap Modal / httpx failures in `RunnerError`. Propagate; map only product/auth cases.
- FastAPI: HTTP status conveys success. Response models without `ok: bool`. Use `HTTPException` for 401 / 400 / 503.
- Sync I/O (Modal SDK, httpx): use sync `def` endpoints or `asyncio.to_thread`. Never block `async def` handlers with sync Modal/httpx calls.
- Credentials and JIT configs via `modal.Secret`. Never put `GITHUB_TOKEN`, `WEBHOOK_SECRET`, or JIT strings in Sandbox `env=` dicts.
- Sandbox / Job `secrets=` do **not** authenticate parent-process API calls (JIT mint reads process env / Server Secret). Document that; never imply otherwise.
- Verify GitHub webhooks with `hmac.compare_digest` on the raw body before parse.

## Concurrency & shared state

- Webhook idempotency: **claim** the delivery ID before side effects (`put` / `skip_if_exists` or CAS). Never mark done only after `Job.create` (duplicate Sandboxes on retry/race).
- Soft list-based capacity is enough — never fake linearizable concurrency locks.
- Shared Modal Dict writes: prefer conditional writes for init and idempotency keys. Do not overwrite shared meta without merge / CAS intent.
- Do not full-scan / trim entire Dict stores on every request without an explicit GC strategy (separate hot path from opportunistic cleanup).

## Layout, images, tests

- Modules by responsibility: `runner` / `api` / `server` / `exceptions`. Keep nested `Job` if it matches the Modal twin; extract collaborators (JIT, images, meta) if `runner.py` grows further — don’t pile every concern into one god module.
- Images: examples use `Image.…uv_sync()` from the repo root; published control plane uses `uv_pip_install("runner-modal")`. No Path / `add_local_dir` for package install.
- One unit test file per impl file (`test_runner.py`, `test_api.py`, `test_server.py`, `test_exceptions.py`).
- Test HMAC failure, delivery claim, and capacity semantics — not only exports / signatures.
- Retries (tenacity): transient network / timeouts / 5xx only. Never retry 401 / 403 / validation errors.

## Never do

- Process-local `_REGISTRY` / create caches / fake idempotency
- Underscore-prefixing types or helpers for privacy (`_Foo`, `_helper`) — omit from `__all__` instead
- Naming our types `App` (conflicts with `modal.App`) or other Modal entity names
- `getattr` / `hasattr` / `object.__new__` / blocked `__init__` / mutating `__name__` for Modal Server identity
- None-filtered `**kwargs` bags into Modal APIs
- Tagged resource unions / xor validators / `sandbox_kwargs` helpers instead of Modal flat kwargs
- `ok: bool` on HTTP response models
- Blanket `except Exception: raise RunnerError(...)`
- Exception taxonomy for every Modal failure mode
- Tokens or JIT in `env=`; assuming Job `secrets=` mint JIT in the parent
- Blocking the asyncio event loop with Modal / httpx in `async def`
- Claim-after-create webhook deliveries
- Treating soft capacity as a hard reservation
- Inheriting Modal SDK types for the product API
- Free public helper functions as the primary DX
- Path / `add_local_dir` instead of uv-native Image install
- Substituting export/signature tests for security and race/idempotency tests
