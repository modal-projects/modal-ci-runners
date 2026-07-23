"""Tests for runner_modal.runner — Modal-shaped Runner / Job API."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

import runner_modal
from runner_modal import Runner, RunnerInfo
from runner_modal.exceptions import ConcurrencyLimitError, JobTimeoutError
from runner_modal.job import JOB_SPEC_ENV, JobSpec
from runner_modal.runner import JOB_KIND, JOB_MODULE, KIND_TAG, POOL_TAG


def test_public_exports_match_modal_style_surface() -> None:
    assert "Runner" in runner_modal.__all__
    assert "ConcurrencyLimitError" in runner_modal.__all__
    for name in (
        "ResourceSpec",
        "DockerResources",
        "RunnerPool",
        "spawn",
        "parse_labels",
        "NotReadyError",
        "LabelError",
        "ConfigError",
        "InvalidNameError",
        "AlreadyExistsError",
        "JitConfig",
        "GitHubServer",
        "JobSpec",
    ):
        assert name not in runner_modal.__all__
        assert name not in vars(runner_modal)


def test_runner_factories_mirror_volume() -> None:
    assert callable(Runner.create)
    assert callable(Runner.from_name)
    assert callable(Runner.ephemeral)
    assert isinstance(Runner.objects, object)
    sig = inspect.signature(Runner.from_name)
    assert "create_if_missing" in sig.parameters
    assert list(sig.parameters)[0] == "name"


def test_job_factories_mirror_sandbox() -> None:
    assert callable(Runner.Job.create)
    assert callable(Runner.Job.from_id)
    assert callable(Runner.Job.from_name)
    assert callable(Runner.Job.list)
    assert "spawn" not in vars(Runner)
    sig = inspect.signature(Runner.Job.create)
    assert "cpu" in sig.parameters
    assert "gpu" in sig.parameters
    assert "experimental_options" in sig.parameters
    assert "resources" not in sig.parameters
    assert "jit_config" not in sig.parameters
    assert "secret" in sig.parameters
    assert list(sig.parameters)[0] == "runner"


def test_create_signature_requires_named_secret() -> None:
    sig = inspect.signature(Runner.create)
    assert not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
    assert "secret" in sig.parameters
    assert "secrets" not in sig.parameters
    for name in ("repository", "timeout", "region", "resources"):
        assert name not in sig.parameters
    assert "gpu" in inspect.signature(Runner.Job.create).parameters


def test_job_create_raises_when_at_capacity() -> None:
    runner = Runner.from_name("full", create_if_missing=True, max_concurrent=1)
    secret = MagicMock()

    with (
        patch.object(runner, "hydrate"),
        patch.object(runner, "has_capacity", return_value=False),
        pytest.raises(ConcurrencyLimitError, match="max_concurrent"),
    ):
        Runner.Job.create(runner, repository="a/b", secret=secret)


def test_has_capacity_lbyl() -> None:
    runner = Runner.from_name("cap", create_if_missing=True, max_concurrent=2)

    with (
        patch.object(runner, "hydrate"),
        patch.object(runner, "active_count", return_value=2),
    ):
        assert runner.has_capacity() is False
    with (
        patch.object(runner, "hydrate"),
        patch.object(runner, "active_count", return_value=1),
    ):
        assert runner.has_capacity() is True


def test_url_none_when_undeployed() -> None:
    runner = Runner.from_name("u")
    with patch.object(runner, "hydrate"):
        assert runner.url is None


def test_job_create_passes_secret_and_spec() -> None:
    fake_sb = MagicMock()
    fake_sb.object_id = "sb-test"
    runner = Runner.from_name("p", create_if_missing=True, labels=["modal"])
    gh = MagicMock()

    with (
        patch.object(runner, "hydrate"),
        patch.object(runner, "has_capacity", return_value=True),
        patch(
            "runner_modal.runner.modal.Sandbox.create", return_value=fake_sb
        ) as create,
        patch.object(Runner, "default_image", return_value=MagicMock()),
    ):
        job = Runner.Job.create(
            runner,
            repository="a/b",
            labels=["job-1"],
            secret=gh,
            cpu=2.0,
        )
        assert job.object_id == "sb-test"
        assert create.call_args.args[:3] == ("python", "-m", JOB_MODULE)
        kwargs = create.call_args.kwargs
        assert kwargs["secrets"][0] is gh
        assert "MODAL_RUNNER_JIT" not in (kwargs.get("env") or {})
        spec = JobSpec.model_validate_json(kwargs["env"][JOB_SPEC_ENV])
        assert spec.repository == "a/b"
        assert "modal" in spec.labels
        assert "job-1" in spec.labels
        assert kwargs["tags"][KIND_TAG] == JOB_KIND
        assert kwargs["tags"][POOL_TAG] == "p"
        assert kwargs["cpu"] == 2.0


def test_job_create_docker_sets_vm() -> None:
    fake_sb = MagicMock()
    fake_sb.object_id = "sb-d"
    runner = Runner.from_name("d", create_if_missing=True)
    gh = MagicMock()

    with (
        patch.object(runner, "hydrate"),
        patch.object(runner, "has_capacity", return_value=True),
        patch(
            "runner_modal.runner.modal.Sandbox.create", return_value=fake_sb
        ) as create,
        patch.object(Runner, "docker_image", return_value=MagicMock()),
    ):
        Runner.Job.create(
            runner,
            repository="a/b",
            labels=["modal"],
            secret=gh,
            experimental_options={"vm_runtime": True},
        )
        assert create.call_args.kwargs["experimental_options"]["vm_runtime"] is True
        spec = JobSpec.model_validate_json(create.call_args.kwargs["env"][JOB_SPEC_ENV])
        assert spec.use_docker is True


def test_wait_timeout() -> None:
    sb = MagicMock()
    sb.object_id = "sb-w"

    def hang(**kwargs: object) -> None:
        import time

        time.sleep(10)

    sb.wait.side_effect = hang
    job = Runner.Job(sb)
    with pytest.raises(JobTimeoutError):
        job.wait(timeout=0.05)
    sb.terminate.assert_called()


def test_jit_auth_and_repo() -> None:
    from runner_modal.exceptions import AuthError
    from runner_modal.job import mint_jitconfig

    with pytest.raises(AuthError, match="token is required"):
        mint_jitconfig(repository="a/b", labels=["m"], token="", name="r")
    with pytest.raises(ValueError, match="owner/repo"):
        mint_jitconfig(repository="bad", labels=["m"], token="t", name="r")


def test_runner_info_model() -> None:
    assert "secret_name" in RunnerInfo.model_fields


def test_admits() -> None:
    runner = Runner.from_name(
        "p", create_if_missing=True, labels=["self-hosted", "modal"]
    )
    with patch.object(runner, "hydrate"):
        runner.meta.labels = ["self-hosted", "modal"]
        assert runner.admits(["self-hosted", "modal", "job-1"]) is True
        assert runner.admits(["modal", "job-1"]) is False
        assert runner.admits(["ubuntu-latest"]) is False
    runner.meta.labels = []
    with patch.object(runner, "hydrate"):
        assert runner.admits(["self-hosted", "modal"]) is False
