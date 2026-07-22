"""HTTP control plane — webhook FastAPI app, events, delivery store (not exported)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from collections.abc import Mapping
from typing import Literal, Self

import modal
from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, ValidationError

from runner_modal.exceptions import AuthError, ConcurrencyLimitError
from runner_modal.runner import Runner

DELIVERY_TTL_SECONDS = 7 * 24 * 3600
PENDING_TTL_SECONDS = 15 * 60


class DeliveryRecord(BaseModel):
    status: Literal["pending", "done"]
    ts: float
    object_id: str | None = None


class Repository(BaseModel):
    full_name: str


class WorkflowJob(BaseModel):
    id: int
    run_id: int = 0
    labels: list[str] = Field(default_factory=list)


class WorkflowJobEvent(BaseModel):
    """GitHub ``workflow_job`` payload.

    Use ``from_request`` for HMAC verification. Handler branches on ``action``.
    """

    action: str
    workflow_job: WorkflowJob
    repository: Repository
    delivery_id: str = ""

    @property
    def job_id(self) -> int:
        return self.workflow_job.id

    @property
    def labels(self) -> list[str]:
        return list(self.workflow_job.labels)

    @property
    def repo(self) -> str:
        return self.repository.full_name

    @classmethod
    def from_request(
        cls,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str | None = None,
    ) -> Self | None:
        secret = secret or os.environ.get("WEBHOOK_SECRET")
        if not secret:
            raise AuthError("WEBHOOK_SECRET is required to verify GitHub webhooks")

        hdrs = {k.lower(): v for k, v in headers.items()}
        signature = hdrs.get("x-hub-signature-256")
        if not signature or not signature.startswith("sha256="):
            raise AuthError("missing or invalid X-Hub-Signature-256")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        received = signature.removeprefix("sha256=")
        if not hmac.compare_digest(expected, received):
            raise AuthError("webhook signature mismatch")

        event_name = hdrs.get("x-github-event", "")
        if event_name and event_name != "workflow_job":
            return None

        event = cls.model_validate_json(body)
        if event.action not in ("queued", "cancelled"):
            return None

        delivery = hdrs.get("x-github-delivery") or str(event.workflow_job.id)
        return event.model_copy(update={"delivery_id": delivery})


class HealthResponse(BaseModel):
    """GET /health body — success is HTTP 200, not an ``ok`` field."""

    runner: str
    active_runners: int
    max_concurrent: int | None


class JobAccepted(BaseModel):
    """POST /github when a job was created or already delivered."""

    object_id: str
    name: str | None = None


class DeliveryStore:
    """Idempotency store for GitHub delivery IDs (Modal Dict).

    Claim before side effects: ``try_claim`` → create → ``mark_done``.
    ``trim`` is opportunistic GC — not called on the request hot path.
    Stale ``pending`` claims are reclaimed lazily in ``try_claim``.
    """

    def __init__(self, runner_name: str) -> None:
        self.store = modal.Dict.from_name(
            f"{runner_name}-runner-deliveries",
            create_if_missing=True,
        )

    def get(self, delivery_id: str) -> DeliveryRecord | None:
        val = self.store.get(delivery_id)
        if val is None:
            return None
        try:
            return DeliveryRecord.model_validate(val)
        except ValidationError:
            return None

    def try_claim(self, delivery_id: str) -> DeliveryRecord | None:
        """Claim ``delivery_id``. Returns ``None`` if this caller won.

        If the key already exists, returns the existing record (done or pending).
        Stale pending older than ``PENDING_TTL_SECONDS`` are deleted and reclaimed.
        """
        existing = self.get(delivery_id)
        if existing is not None:
            if (
                existing.status == "pending"
                and time.time() - existing.ts > PENDING_TTL_SECONDS
            ):
                del self.store[delivery_id]
            else:
                return existing

        pending = DeliveryRecord(status="pending", ts=time.time())
        written = self.store.put(
            delivery_id,
            pending.model_dump(mode="json"),
            skip_if_exists=True,
        )
        if written:
            return None
        return self.get(delivery_id)

    def mark_done(self, delivery_id: str, object_id: str) -> None:
        record = DeliveryRecord(status="done", ts=time.time(), object_id=object_id)
        self.store[delivery_id] = record.model_dump(mode="json")

    def release(self, delivery_id: str) -> None:
        """Drop a pending claim so GitHub can retry."""
        record = self.get(delivery_id)
        if record is not None and record.status == "pending":
            del self.store[delivery_id]

    def trim(self) -> None:
        """Opportunistic TTL cleanup — call from a schedule, not every request."""
        now = time.time()
        for key in list(self.store.keys()):
            record = self.get(key)
            if record is not None and now - record.ts > DELIVERY_TTL_SECONDS:
                del self.store[key]


class WebhookApp:
    """FastAPI control plane for a named Runner (Modal Server mounts this)."""

    def __init__(self, runner_name: str) -> None:
        self.runner_name = runner_name
        self.deliveries = DeliveryStore(runner_name)
        self.fastapi = FastAPI()
        self.fastapi.get("/health", response_model=HealthResponse)(self.health)
        self.fastapi.post(
            "/github",
            response_model=JobAccepted,
            responses={
                204: {"description": "Event ignored"},
                503: {"description": "Capacity full or delivery in progress"},
            },
        )(self.github_webhook)

    def health(self) -> HealthResponse:
        info = Runner.from_name(self.runner_name).info()
        return HealthResponse(
            runner=info.name,
            active_runners=info.active_runners,
            max_concurrent=info.max_concurrent,
        )

    async def github_webhook(self, request: Request) -> JobAccepted | Response:
        body = await request.body()
        headers = {k: v for k, v in request.headers.items()}
        return await asyncio.to_thread(self.process_webhook, body, headers)

    def process_webhook(
        self, body: bytes, headers: dict[str, str]
    ) -> JobAccepted | Response:
        try:
            event = WorkflowJobEvent.from_request(body, headers)
        except AuthError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)
            ) from e
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=e.errors()
            ) from e

        if event is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        runner = Runner.from_name(self.runner_name)

        if event.action == "cancelled":
            self.terminate_job(runner, event.job_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        pool = set(runner.meta.labels)
        if pool and not pool <= set(event.labels):
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        existing = self.deliveries.try_claim(event.delivery_id)
        if existing is not None:
            if existing.status == "done" and existing.object_id:
                return JobAccepted(object_id=existing.object_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="delivery in progress",
            )

        job_name = f"job-{event.job_id}"
        try:
            job = Runner.Job.create(
                runner,
                repository=event.repo,
                labels=event.labels,
                name=job_name,
            )
        except ConcurrencyLimitError as e:
            self.deliveries.release(event.delivery_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
            ) from e
        except modal.exception.AlreadyExistsError:
            job = self.get_job(runner, job_name)
            if job is None:
                self.deliveries.release(event.delivery_id)
                raise
        except Exception:
            self.deliveries.release(event.delivery_id)
            raise

        # Never release after a successful create — retry would double-spawn.
        self.deliveries.mark_done(event.delivery_id, job.object_id)
        return JobAccepted(
            object_id=job.object_id,
            name=job_name,
        )

    def terminate_job(self, runner: Runner, job_id: int) -> None:
        runner.hydrate()
        app_name = runner.meta.app_name
        if not app_name:
            return
        try:
            job = Runner.Job.from_name(
                app_name,
                f"job-{job_id}",
                environment_name=runner.environment_name,
                client=runner.client,
            )
            job.terminate()
        except (LookupError, modal.exception.NotFoundError):
            return

    def get_job(self, runner: Runner, job_name: str) -> Runner.Job | None:
        runner.hydrate()
        app_name = runner.meta.app_name
        if not app_name:
            return None
        try:
            return Runner.Job.from_name(
                app_name,
                job_name,
                environment_name=runner.environment_name,
                client=runner.client,
            )
        except (LookupError, modal.exception.NotFoundError):
            return None

    @classmethod
    def for_runner(cls, name: str) -> FastAPI:
        return cls(name).fastapi
