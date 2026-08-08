"""Job Sandbox entrypoint — mint JIT from Secret, run Actions runner.

``python -m runner_modal.job``

- ``GITHUB_TOKEN`` — from Modal Secret (required)
- ``RUNNER_JOB_SPEC`` — JSON ``JobSpec`` (required)
"""

from __future__ import annotations

import os
import subprocess
import time

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from runner_modal.exceptions import AuthError
from runner_modal.profile import StageClock

JOB_SPEC_ENV = "RUNNER_JOB_SPEC"
ACTIONS_RUNNER = "/actions-runner/run.sh"


class JobSpec(BaseModel):
    """Non-secret job inputs passed into the Sandbox."""

    model_config = ConfigDict(frozen=True)

    repository: str
    labels: list[str]
    runner_group_id: int = 1
    runner_name: str
    github_enterprise_domain: str | None = None
    use_docker: bool = False


class JitResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    encoded_jit_config: str


def transient_http(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


@retry(
    retry=retry_if_exception(transient_http),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def post_jitconfig(
    url: str, payload: dict[str, object], headers: dict[str, str]
) -> str:
    resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
    if resp.status_code in (401, 403):
        raise AuthError(f"generate-jitconfig: HTTP {resp.status_code}")
    if resp.status_code >= 500:
        resp.raise_for_status()
    if resp.status_code >= 400:
        raise ValueError(f"generate-jitconfig: HTTP {resp.status_code}")
    try:
        return JitResponse.model_validate(resp.json()).encoded_jit_config
    except ValidationError as e:
        raise ValueError("generate-jitconfig: missing encoded_jit_config") from e


def mint_jitconfig(
    *,
    repository: str,
    labels: list[str],
    token: str,
    runner_group_id: int = 1,
    name: str,
    github_enterprise_domain: str | None = None,
) -> str:
    if not token:
        raise AuthError("token is required to mint JIT config")
    if repository.count("/") != 1:
        raise ValueError(f"repository must be 'owner/repo', got {repository!r}")
    if not labels:
        raise ValueError("labels must be non-empty")

    owner, repo = repository.split("/", 1)
    base = (
        f"https://{github_enterprise_domain}/api/v3"
        if github_enterprise_domain
        else "https://api.github.com"
    )
    return post_jitconfig(
        f"{base}/repos/{owner}/{repo}/actions/runners/generate-jitconfig",
        {
            "name": name,
            "runner_group_id": runner_group_id,
            "labels": labels,
        },
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "runner-modal",
        },
    )


def wait_for_docker(*, attempts: int = 120) -> None:
    for _ in range(attempts):
        if (
            subprocess.run(
                ["docker", "info"],
                check=False,
                capture_output=True,
            ).returncode
            == 0
        ):
            return
        time.sleep(1)
    raise RuntimeError("dockerd did not become ready")


def boot_docker() -> None:
    subprocess.Popen(
        ["dockerd", "-D"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    wait_for_docker()


def run(spec: JobSpec, *, token: str) -> None:
    if spec.use_docker:
        with StageClock("boot_docker"):
            boot_docker()
    with StageClock("jit_mint"):
        jit = mint_jitconfig(
            repository=spec.repository,
            labels=spec.labels,
            token=token,
            runner_group_id=spec.runner_group_id,
            name=spec.runner_name,
            github_enterprise_domain=spec.github_enterprise_domain,
        )
    # run.sh replaces this process — no stage timer after exec.
    os.execv(ACTIONS_RUNNER, [ACTIONS_RUNNER, "--jitconfig", jit])


def main() -> None:
    try:
        raw = os.environ[JOB_SPEC_ENV]
    except KeyError as e:
        raise RuntimeError(f"{JOB_SPEC_ENV} is required") from e
    try:
        token = os.environ["GITHUB_TOKEN"]
    except KeyError as e:
        raise AuthError("GITHUB_TOKEN is required (modal.Secret on Job.create)") from e
    run(JobSpec.model_validate_json(raw), token=token)


if __name__ == "__main__":
    main()
