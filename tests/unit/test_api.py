"""Tests for runner_modal.api."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from runner_modal.api import (
    PENDING_TTL_SECONDS,
    DeliveryRecord,
    DeliveryStore,
    WebhookApp,
    WorkflowJobEvent,
)
from runner_modal.exceptions import AuthError, ConcurrencyLimitError
from runner_modal.runner import RunnerMeta


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


POOL_LABELS = ["self-hosted", "modal", "acme"]
HOOK = "hook"


def queued_body(*, labels: list[str] | None = None) -> bytes:
    return json.dumps(
        {
            "action": "queued",
            "workflow_job": {
                "id": 42,
                "run_id": 1,
                "labels": labels if labels is not None else list(POOL_LABELS),
            },
            "repository": {"full_name": "acme/api"},
        }
    ).encode()


def cancelled_body(*, job_id: int = 42) -> bytes:
    return json.dumps(
        {
            "action": "cancelled",
            "workflow_job": {"id": job_id, "run_id": 1, "labels": list(POOL_LABELS)},
            "repository": {"full_name": "acme/api"},
        }
    ).encode()


def mock_runner(*, labels: list[str] | None = None) -> MagicMock:
    """Unhydrated handle: labels empty until ``hydrate`` loads persisted pool meta."""
    pool = list(labels) if labels is not None else list(POOL_LABELS)
    runner = MagicMock()
    runner.meta = RunnerMeta(name="acme", labels=[])
    runner.environment_name = None
    runner.client = None

    def hydrate() -> None:
        runner.meta = RunnerMeta(
            name="acme",
            labels=pool,
            app_name=runner.meta.app_name,
            server_name=runner.meta.server_name,
            secret_name="github-runner",
        )

    def admits(job_labels: list[str]) -> bool:
        runner.hydrate()
        configured = set(runner.meta.labels)
        present = set(job_labels)
        return bool(configured) and "self-hosted" in present and configured <= present

    runner.hydrate.side_effect = hydrate
    runner.admits.side_effect = admits
    runner.github_secret.return_value = MagicMock(name="github-secret")
    return runner


def client() -> TestClient:
    return TestClient(WebhookApp.for_runner("acme", webhook_secret=HOOK))


def post_github(test_client: TestClient, body: bytes, *, delivery: str = "d") -> object:
    return test_client.post(
        "/github",
        content=body,
        headers={
            "X-GitHub-Event": "workflow_job",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign(body, HOOK),
            "Content-Type": "application/json",
        },
    )


def test_workflow_event_requires_secret() -> None:
    with pytest.raises(AuthError, match="webhook secret is required"):
        WorkflowJobEvent.from_request(b"{}", {}, secret="")


def test_workflow_event_bad_signature() -> None:
    body = queued_body()
    with pytest.raises(AuthError, match="mismatch"):
        WorkflowJobEvent.from_request(
            body,
            {
                "x-github-event": "workflow_job",
                "x-hub-signature-256": sign(body, "wrong"),
            },
            secret=HOOK,
        )


def test_workflow_event_queued() -> None:
    body = queued_body()
    event = WorkflowJobEvent.from_request(
        body,
        {
            "x-github-event": "workflow_job",
            "x-github-delivery": "d1",
            "x-hub-signature-256": sign(body, HOOK),
        },
        secret=HOOK,
    )
    assert event is not None
    assert event.repo == "acme/api"
    assert event.job_id == 42
    assert event.delivery_id == "d1"


def test_workflow_event_cancelled() -> None:
    body = cancelled_body()
    event = WorkflowJobEvent.from_request(
        body,
        {
            "x-github-event": "workflow_job",
            "x-github-delivery": "c1",
            "x-hub-signature-256": sign(body, HOOK),
        },
        secret=HOOK,
    )
    assert event is not None
    assert event.action == "cancelled"


def test_workflow_event_ignores_completed() -> None:
    payload = {
        "action": "completed",
        "workflow_job": {"id": 1, "run_id": 2, "labels": []},
        "repository": {"full_name": "a/b"},
    }
    body = json.dumps(payload).encode()
    assert (
        WorkflowJobEvent.from_request(
            body,
            {
                "x-github-event": "workflow_job",
                "x-hub-signature-256": sign(body, HOOK),
            },
            secret=HOOK,
        )
        is None
    )


def test_delivery_record_roundtrip() -> None:
    record = DeliveryRecord(status="done", ts=1.0, object_id="sb-1")
    assert DeliveryRecord.model_validate(record.model_dump()).object_id == "sb-1"


def test_try_claim_reclaims_stale_pending() -> None:
    store = MagicMock()
    stale = DeliveryRecord(
        status="pending", ts=time.time() - PENDING_TTL_SECONDS - 1
    ).model_dump(mode="json")
    store.get.side_effect = [stale, None]
    store.put.return_value = True

    deliveries = DeliveryStore.__new__(DeliveryStore)
    deliveries.store = store
    assert deliveries.try_claim("d-stale") is None
    store.__delitem__.assert_called()
    store.put.assert_called_once()


def test_github_webhook_200() -> None:
    fake_job = MagicMock()
    fake_job.object_id = "sb-ok"
    runner = mock_runner()
    gh = MagicMock()
    runner.github_secret.return_value = gh

    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch.object(DeliveryStore, "try_claim", return_value=None),
        patch.object(DeliveryStore, "mark_done") as mark_done,
        patch("runner_modal.runner.Runner.from_name", return_value=runner),
        patch("runner_modal.runner.Runner.Job.create", return_value=fake_job) as create,
    ):
        resp = post_github(client(), queued_body(), delivery="deliv-1")
        assert resp.status_code == 200
        assert resp.json() == {"object_id": "sb-ok", "name": "job-42"}
        mark_done.assert_called_once_with("deliv-1", "sb-ok")
        assert create.call_args.kwargs["secret"] is gh


def test_github_webhook_401_bad_hmac() -> None:
    with patch.object(DeliveryStore, "__init__", lambda self, name: None):
        body = queued_body()
        resp = client().post(
            "/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "d",
                "X-Hub-Signature-256": sign(body, "wrong"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401


def test_github_webhook_204_when_ignored() -> None:
    with patch.object(DeliveryStore, "__init__", lambda self, name: None):
        payload = {
            "action": "completed",
            "workflow_job": {"id": 1, "run_id": 1, "labels": []},
            "repository": {"full_name": "a/b"},
        }
        resp = post_github(client(), json.dumps(payload).encode())
        assert resp.status_code == 204


def test_github_webhook_204_label_mismatch() -> None:
    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch(
            "runner_modal.runner.Runner.from_name",
            return_value=mock_runner(labels=POOL_LABELS),
        ),
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        resp = post_github(client(), queued_body(labels=["modal"]))
        assert resp.status_code == 204
        create.assert_not_called()


def test_admission_hydrates_before_pool_check() -> None:
    """Regression: from_name alone leaves labels=[]; admits must hydrate first."""
    runner = mock_runner(labels=POOL_LABELS)
    assert runner.meta.labels == []

    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch("runner_modal.runner.Runner.from_name", return_value=runner),
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        resp = post_github(client(), queued_body(labels=["ubuntu-latest"]))
        assert resp.status_code == 204
        runner.admits.assert_called()
        runner.hydrate.assert_called()
        create.assert_not_called()


def test_admission_rejects_missing_self_hosted() -> None:
    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch(
            "runner_modal.runner.Runner.from_name",
            return_value=mock_runner(labels=["modal", "acme"]),
        ),
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        resp = post_github(client(), queued_body(labels=["modal", "acme"]))
        assert resp.status_code == 204
        create.assert_not_called()


def test_admission_rejects_empty_pool() -> None:
    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch(
            "runner_modal.runner.Runner.from_name",
            return_value=mock_runner(labels=[]),
        ),
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        resp = post_github(client(), queued_body(labels=POOL_LABELS))
        assert resp.status_code == 204
        create.assert_not_called()


def test_github_webhook_cancelled_terminates() -> None:
    job = MagicMock()
    runner = mock_runner()

    def hydrate() -> None:
        runner.meta = RunnerMeta(
            name="acme",
            labels=POOL_LABELS,
            app_name="acme-ci",
            secret_name="github-runner",
        )

    runner.hydrate.side_effect = hydrate

    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch("runner_modal.runner.Runner.from_name", return_value=runner),
        patch(
            "runner_modal.runner.Runner.Job.from_name", return_value=job
        ) as from_name,
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        resp = post_github(client(), cancelled_body(job_id=99))
        assert resp.status_code == 204
        from_name.assert_called_once()
        assert from_name.call_args.args[:2] == ("acme-ci", "job-99")
        job.terminate.assert_called_once()
        create.assert_not_called()


def test_github_webhook_cancelled_missing_job() -> None:
    runner = mock_runner()

    def hydrate() -> None:
        runner.meta = RunnerMeta(
            name="acme",
            labels=POOL_LABELS,
            app_name="acme-ci",
            secret_name="github-runner",
        )

    runner.hydrate.side_effect = hydrate

    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch("runner_modal.runner.Runner.from_name", return_value=runner),
        patch(
            "runner_modal.runner.Runner.Job.from_name",
            side_effect=LookupError("gone"),
        ),
    ):
        resp = post_github(client(), cancelled_body())
        assert resp.status_code == 204


def test_github_webhook_duplicate_delivery() -> None:
    done = DeliveryRecord(status="done", ts=1.0, object_id="sb-dup")

    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch.object(DeliveryStore, "try_claim", return_value=done),
        patch("runner_modal.runner.Runner.from_name", return_value=mock_runner()),
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        resp = post_github(client(), queued_body())
        assert resp.status_code == 200
        assert resp.json()["object_id"] == "sb-dup"
        create.assert_not_called()


def test_github_webhook_503_when_pending() -> None:
    pending = DeliveryRecord(status="pending", ts=1.0)

    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch.object(DeliveryStore, "try_claim", return_value=pending),
        patch("runner_modal.runner.Runner.from_name", return_value=mock_runner()),
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        resp = post_github(client(), queued_body())
        assert resp.status_code == 503
        create.assert_not_called()


def test_github_webhook_503_when_full() -> None:
    runner = mock_runner()
    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch.object(DeliveryStore, "try_claim", return_value=None),
        patch.object(DeliveryStore, "release") as release,
        patch("runner_modal.runner.Runner.from_name", return_value=runner),
        patch(
            "runner_modal.runner.Runner.Job.create",
            side_effect=ConcurrencyLimitError("full"),
        ),
    ):
        resp = post_github(client(), queued_body())
        assert resp.status_code == 503
        release.assert_called_once_with("d")


def test_github_webhook_500_releases_claim() -> None:
    runner = mock_runner()
    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch.object(DeliveryStore, "try_claim", return_value=None),
        patch.object(DeliveryStore, "mark_done") as mark_done,
        patch.object(DeliveryStore, "release") as release,
        patch("runner_modal.runner.Runner.from_name", return_value=runner),
        patch(
            "runner_modal.runner.Runner.Job.create",
            side_effect=RuntimeError("boom"),
        ),
    ):
        test_client = TestClient(
            WebhookApp.for_runner("acme", webhook_secret=HOOK),
            raise_server_exceptions=False,
        )
        resp = post_github(test_client, queued_body(), delivery="d-fail")
        assert resp.status_code == 500
        mark_done.assert_not_called()
        release.assert_called_once_with("d-fail")


def test_github_webhook_mark_done_failure_does_not_release() -> None:
    fake_job = MagicMock()
    fake_job.object_id = "sb-ok"
    runner = mock_runner()

    with (
        patch.object(DeliveryStore, "__init__", lambda self, name: None),
        patch.object(DeliveryStore, "try_claim", return_value=None),
        patch.object(DeliveryStore, "mark_done", side_effect=RuntimeError("dict")),
        patch.object(DeliveryStore, "release") as release,
        patch("runner_modal.runner.Runner.from_name", return_value=runner),
        patch("runner_modal.runner.Runner.Job.create", return_value=fake_job),
    ):
        test_client = TestClient(
            WebhookApp.for_runner("acme", webhook_secret=HOOK),
            raise_server_exceptions=False,
        )
        resp = post_github(test_client, queued_body())
        assert resp.status_code == 500
        release.assert_not_called()
