"""Tests for runner_modal.runner — Modal-shaped Runner / Job API."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

import runner_modal
from runner_modal import Runner, RunnerInfo
from runner_modal.exceptions import AuthError, ConcurrencyLimitError, JobTimeoutError
from runner_modal.runner import JitConfig, TAG_KIND, TAG_KIND_VALUE, TAG_POOL


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
    assert list(sig.parameters)[0] == "runner"


def test_create_signature_is_server_knobs_not_sandbox() -> None:
    sig = inspect.signature(Runner.create)
    assert not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
    for name in ("repository", "timeout", "region", "resources"):
        assert name not in sig.parameters
    # Server may take gpu=; Job takes Sandbox gpu=/cpu=/experimental_options=
    assert "gpu" in inspect.signature(Runner.Job.create).parameters


def test_job_create_raises_when_at_capacity() -> None:
    runner = Runner.from_name("full", create_if_missing=True, max_concurrent=1)

    with (
        patch.object(runner, "hydrate"),
        patch.object(runner, "has_capacity", return_value=False),
        pytest.raises(ConcurrencyLimitError, match="max_concurrent"),
    ):
        Runner.Job.create(runner, repository="a/b", jit_config="x")


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


def test_job_create_uses_jit_secret() -> None:
    fake_sb = MagicMock()
    fake_sb.object_id = "sb-test"
    runner = Runner.from_name("p", create_if_missing=True, labels=["modal"])

    with (
        patch.object(runner, "hydrate"),
        patch.object(runner, "has_capacity", return_value=True),
        patch.object(JitConfig, "mint", return_value=JitConfig(encoded="JIT")),
        patch("runner_modal.runner.modal.Secret.from_dict") as from_dict,
        patch("runner_modal.runner.modal.Sandbox.create", return_value=fake_sb) as create,
        patch.object(Runner, "default_image", return_value=MagicMock()),
    ):
        jit_secret = MagicMock()
        from_dict.return_value = jit_secret
        job = Runner.Job.create(
            runner,
            repository="a/b",
            labels=["job-1"],
            cpu=2.0,
            secrets=[MagicMock()],
        )
        assert job.object_id == "sb-test"
        kwargs = create.call_args.kwargs
        assert jit_secret in kwargs["secrets"]
        assert "MODAL_RUNNER_JIT" not in (kwargs.get("env") or {})
        assert kwargs["tags"][TAG_KIND] == TAG_KIND_VALUE
        assert kwargs["tags"][TAG_POOL] == "p"
        assert kwargs["cpu"] == 2.0


def test_job_create_docker_sets_vm() -> None:
    fake_sb = MagicMock()
    fake_sb.object_id = "sb-d"
    runner = Runner.from_name("d", create_if_missing=True)

    with (
        patch.object(runner, "hydrate"),
        patch.object(runner, "has_capacity", return_value=True),
        patch.object(JitConfig, "mint", return_value=JitConfig(encoded="x")),
        patch("runner_modal.runner.modal.Secret.from_dict", return_value=MagicMock()),
        patch("runner_modal.runner.modal.Sandbox.create", return_value=fake_sb) as create,
        patch.object(Runner, "docker_image", return_value=MagicMock()),
    ):
        Runner.Job.create(
            runner,
            repository="a/b",
            jit_config="x",
            experimental_options={"vm_runtime": True},
        )
        assert create.call_args.kwargs["experimental_options"]["vm_runtime"] is True


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


def test_jit_auth_and_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(AuthError, match="GITHUB_TOKEN"):
        JitConfig.mint(repository="a/b", labels=["m"])
    with pytest.raises(ValueError, match="owner/repo"):
        JitConfig.mint(repository="bad", labels=["m"], token="t")


def test_runner_info_model() -> None:
    assert "name" in RunnerInfo.model_fields
