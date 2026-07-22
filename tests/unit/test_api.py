"""Tests for runner_modal.api."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from runner_modal.api import App, Deliveries, DeliveryRecord, WorkflowJobEvent
from runner_modal.exceptions import AuthError, ConcurrencyLimitError


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _queued_body() -> bytes:
    return json.dumps(
        {
            "action": "queued",
            "workflow_job": {"id": 42, "run_id": 1, "labels": ["modal"]},
            "repository": {"full_name": "acme/api"},
        }
    ).encode()


def test_workflow_event_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    with pytest.raises(AuthError, match="WEBHOOK_SECRET"):
        WorkflowJobEvent.from_request(b"{}", {})


def test_workflow_event_bad_signature() -> None:
    body = _queued_body()
    with pytest.raises(AuthError, match="mismatch"):
        WorkflowJobEvent.from_request(
            body,
            {
                "x-github-event": "workflow_job",
                "x-hub-signature-256": _sign(body, "wrong"),
            },
            secret="hook",
        )


def test_workflow_event_queued() -> None:
    body = _queued_body()
    secret = "hook"
    event = WorkflowJobEvent.from_request(
        body,
        {
            "x-github-event": "workflow_job",
            "x-github-delivery": "d1",
            "x-hub-signature-256": _sign(body, secret),
        },
        secret=secret,
    )
    assert event is not None
    assert event.repo == "acme/api"
    assert event.job_id == 42
    assert event.delivery_id == "d1"


def test_workflow_event_ignores_non_queued() -> None:
    payload = {
        "action": "completed",
        "workflow_job": {"id": 1, "run_id": 2, "labels": []},
        "repository": {"full_name": "a/b"},
    }
    body = json.dumps(payload).encode()
    secret = "hook"
    assert (
        WorkflowJobEvent.from_request(
            body,
            {
                "x-github-event": "workflow_job",
                "x-hub-signature-256": _sign(body, secret),
            },
            secret=secret,
        )
        is None
    )


def test_delivery_record_roundtrip() -> None:
    record = DeliveryRecord(status="done", ts=1.0, object_id="sb-1")
    assert DeliveryRecord.model_validate(record.model_dump()).object_id == "sb-1"


def test_github_webhook_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    fake_job = MagicMock()
    fake_job.object_id = "sb-ok"

    with (
        patch.object(Deliveries, "__init__", lambda self, name: None),
        patch.object(Deliveries, "try_claim", return_value=None),
        patch.object(Deliveries, "mark_done") as mark_done,
        patch("runner_modal.runner.Runner.from_name") as from_name,
        patch("runner_modal.runner.Runner.Job.create", return_value=fake_job),
    ):
        from_name.return_value = MagicMock()
        client = TestClient(App.for_runner("acme"))
        body = _queued_body()
        resp = client.post(
            "/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "deliv-1",
                "X-Hub-Signature-256": _sign(body, "hook"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"object_id": "sb-ok", "name": "job-42"}
        mark_done.assert_called_once_with("deliv-1", "sb-ok")


def test_github_webhook_401_bad_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    with patch.object(Deliveries, "__init__", lambda self, name: None):
        client = TestClient(App.for_runner("acme"))
        body = _queued_body()
        resp = client.post(
            "/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "d",
                "X-Hub-Signature-256": _sign(body, "wrong"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401


def test_github_webhook_204_when_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")

    with patch.object(Deliveries, "__init__", lambda self, name: None):
        client = TestClient(App.for_runner("acme"))
        payload = {
            "action": "completed",
            "workflow_job": {"id": 1, "run_id": 1, "labels": []},
            "repository": {"full_name": "a/b"},
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "d",
                "X-Hub-Signature-256": _sign(body, "hook"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 204


def test_github_webhook_duplicate_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    done = DeliveryRecord(status="done", ts=1.0, object_id="sb-dup")

    with (
        patch.object(Deliveries, "__init__", lambda self, name: None),
        patch.object(Deliveries, "try_claim", return_value=done),
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        client = TestClient(App.for_runner("acme"))
        body = _queued_body()
        resp = client.post(
            "/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "d",
                "X-Hub-Signature-256": _sign(body, "hook"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["object_id"] == "sb-dup"
        create.assert_not_called()


def test_github_webhook_503_when_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    pending = DeliveryRecord(status="pending", ts=1.0)

    with (
        patch.object(Deliveries, "__init__", lambda self, name: None),
        patch.object(Deliveries, "try_claim", return_value=pending),
        patch("runner_modal.runner.Runner.Job.create") as create,
    ):
        client = TestClient(App.for_runner("acme"))
        body = _queued_body()
        resp = client.post(
            "/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "d",
                "X-Hub-Signature-256": _sign(body, "hook"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 503
        create.assert_not_called()


def test_github_webhook_503_when_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")

    with (
        patch.object(Deliveries, "__init__", lambda self, name: None),
        patch.object(Deliveries, "try_claim", return_value=None),
        patch.object(Deliveries, "release") as release,
        patch("runner_modal.runner.Runner.from_name", return_value=MagicMock()),
        patch(
            "runner_modal.runner.Runner.Job.create",
            side_effect=ConcurrencyLimitError("full"),
        ),
    ):
        client = TestClient(App.for_runner("acme"))
        body = _queued_body()
        resp = client.post(
            "/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "d",
                "X-Hub-Signature-256": _sign(body, "hook"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 503
        release.assert_called_once_with("d")


def test_github_webhook_500_releases_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")

    with (
        patch.object(Deliveries, "__init__", lambda self, name: None),
        patch.object(Deliveries, "try_claim", return_value=None),
        patch.object(Deliveries, "mark_done") as mark_done,
        patch.object(Deliveries, "release") as release,
        patch("runner_modal.runner.Runner.from_name") as from_name,
        patch(
            "runner_modal.runner.Runner.Job.create",
            side_effect=RuntimeError("boom"),
        ),
    ):
        from_name.return_value = MagicMock()
        client = TestClient(App.for_runner("acme"), raise_server_exceptions=False)
        body = json.dumps(
            {
                "action": "queued",
                "workflow_job": {"id": 7, "run_id": 1, "labels": ["modal"]},
                "repository": {"full_name": "acme/api"},
            }
        ).encode()
        resp = client.post(
            "/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "d-fail",
                "X-Hub-Signature-256": _sign(body, "hook"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 500
        mark_done.assert_not_called()
        release.assert_called_once_with("d-fail")
