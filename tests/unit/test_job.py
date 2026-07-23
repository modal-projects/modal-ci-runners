"""Tests for runner_modal.job."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from runner_modal.exceptions import AuthError
from runner_modal.job import (
    ACTIONS_RUNNER,
    JOB_SPEC_ENV,
    JobSpec,
    main,
    mint_jitconfig,
    run,
)


def test_job_spec_roundtrip() -> None:
    spec = JobSpec(
        repository="a/b",
        labels=["modal"],
        runner_group_id=2,
        runner_name="r1",
        use_docker=True,
    )
    assert JobSpec.model_validate_json(spec.model_dump_json()).use_docker is True


def test_main_requires_job_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JOB_SPEC_ENV, raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match=JOB_SPEC_ENV):
        main()


def test_main_requires_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = JobSpec(repository="a/b", labels=["m"], runner_name="r")
    monkeypatch.setenv(JOB_SPEC_ENV, spec.model_dump_json())
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(AuthError, match="GITHUB_TOKEN"):
        main()


def test_run_mints_and_execs() -> None:
    spec = JobSpec(repository="a/b", labels=["modal"], runner_name="r1")
    with (
        patch("runner_modal.job.mint_jitconfig", return_value="JIT") as mint,
        patch("runner_modal.job.os.execv") as execv,
    ):
        run(spec, token="tok")
        mint.assert_called_once()
        assert mint.call_args.kwargs["token"] == "tok"
        execv.assert_called_once_with(
            ACTIONS_RUNNER, [ACTIONS_RUNNER, "--jitconfig", "JIT"]
        )


def test_run_boots_docker_when_requested() -> None:
    spec = JobSpec(
        repository="a/b", labels=["modal"], runner_name="r1", use_docker=True
    )
    with (
        patch("runner_modal.job.boot_docker") as boot,
        patch("runner_modal.job.mint_jitconfig", return_value="JIT"),
        patch("runner_modal.job.os.execv"),
    ):
        run(spec, token="tok")
        boot.assert_called_once()


def test_mint_requires_token_and_labels() -> None:
    with pytest.raises(AuthError, match="token is required"):
        mint_jitconfig(repository="a/b", labels=["m"], token="", name="r")
    with pytest.raises(ValueError, match="labels must be non-empty"):
        mint_jitconfig(repository="a/b", labels=[], token="t", name="r")
    with pytest.raises(ValueError, match="owner/repo"):
        mint_jitconfig(repository="bad", labels=["m"], token="t", name="r")


def test_mint_no_enterprise_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ENTERPRISE_DOMAIN", "evil.example")
    with patch("runner_modal.job.httpx.post") as post:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"encoded_jit_config": "x"}
        post.return_value = resp
        mint_jitconfig(repository="a/b", labels=["m"], token="t", name="r")
        url = post.call_args.args[0]
        assert url.startswith("https://api.github.com/")
        assert "evil.example" not in url
