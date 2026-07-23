"""Runner — Modal-shaped CI control plane + Job Sandboxes."""

from __future__ import annotations

import concurrent.futures
import os
import re
import time
import uuid
from collections.abc import Collection, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import ClassVar, Self

import modal
from pydantic import BaseModel, Field

from runner_modal.exceptions import ConcurrencyLimitError, JobTimeoutError
from runner_modal.job import JOB_SPEC_ENV, JobSpec
from runner_modal.server import SERVER_CLASS_NAME, GitHubServer

RUNNER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
META_KEY = "runner"
KIND_TAG = "runner_modal"
JOB_KIND = "runner"
POOL_TAG = "runner_pool"
DEFAULT_RUNNER_VERSION = "2.336.0"
JOB_MODULE = "runner_modal.job"


class RunnerMeta(BaseModel):
    """Persisted Runner Dict payload."""

    name: str
    labels: list[str] = Field(default_factory=list)
    max_concurrent: int | None = None
    cache: bool = True
    app_name: str | None = None
    server_name: str | None = None
    # Named Modal Secret from Runner.create (GITHUB_TOKEN + WEBHOOK_SECRET).
    secret_name: str | None = None
    github_enterprise_domain: str | None = None
    # Slim defaults applied by Job.create / webhook when kwargs are omitted.
    region: str | list[str] | None = None
    idle_timeout: int | None = None
    experimental_options: dict[str, object] | None = None
    runner_group_id: int = 1


class RunnerInfo(BaseModel):
    """Snapshot from ``Runner.info()`` (like reading Volume/Dict metadata)."""

    model_config = {"frozen": True}

    name: str
    labels: list[str]
    max_concurrent: int | None
    cache: bool
    active_runners: int
    app_name: str | None
    server_name: str | None
    secret_name: str | None


class RunnerObjects:
    """Admin namespace — same shape as ``Volume.objects`` / ``Dict.objects``."""

    @staticmethod
    def create(
        name: str,
        *,
        allow_existing: bool = False,
        environment_name: str | None = None,
        labels: Sequence[str] | None = None,
        max_concurrent: int | None = None,
        cache: bool = True,
        client: modal.Client | None = None,
    ) -> None:
        if not RUNNER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid Runner name {name!r}")
        modal.Dict.objects.create(
            f"{name}-runner-meta",
            allow_existing=allow_existing,
            environment_name=environment_name,
            client=client,
        )
        if cache:
            modal.Volume.objects.create(
                f"{name}-cache",
                allow_existing=True,
                environment_name=environment_name,
                client=client,
            )
        Runner.from_name(
            name,
            create_if_missing=True,
            environment_name=environment_name,
            labels=list(labels or []),
            max_concurrent=max_concurrent,
            cache=cache,
            client=client,
        ).hydrate()

    @staticmethod
    def delete(
        name: str,
        *,
        allow_missing: bool = True,
        environment_name: str | None = None,
        client: modal.Client | None = None,
    ) -> None:
        modal.Dict.objects.delete(
            f"{name}-runner-meta",
            allow_missing=allow_missing,
            environment_name=environment_name,
            client=client,
        )
        modal.Volume.objects.delete(
            f"{name}-cache",
            allow_missing=True,
            environment_name=environment_name,
            client=client,
        )

    @staticmethod
    def list(
        *,
        environment_name: str | None = None,
        client: modal.Client | None = None,
    ) -> Sequence[str]:
        suffix = "-runner-meta"
        names: list[str] = []
        for d in modal.Dict.objects.list(
            environment_name=environment_name or "",
            client=client,
        ):
            label = d.name or ""
            if label.endswith(suffix):
                names.append(label[: -len(suffix)])
        return sorted(names)


class Runner:
    """Named CI runner — Volume-shaped lookup + App Server registration.

    ``Runner.create(app, name, …)`` registers the control plane
    (definition-time). ``Runner.Job.create(runner, …)`` starts a job
    Sandbox (eager, like ``Sandbox.create``).
    """

    objects: ClassVar[RunnerObjects] = RunnerObjects()

    def __init__(
        self,
        name: str,
        *,
        environment_name: str | None = None,
        create_if_missing: bool = False,
        labels: Sequence[str] | None = None,
        max_concurrent: int | None = None,
        cache: bool = True,
        client: modal.Client | None = None,
    ) -> None:
        self.name = name
        self.environment_name = environment_name
        self.create_if_missing = create_if_missing
        self.client = client
        self.meta = RunnerMeta(
            name=name,
            labels=list(labels or []),
            max_concurrent=max_concurrent,
            cache=cache,
        )
        self.meta_dict: modal.Dict | None = None
        self.volume: modal.Volume | None = None
        self.hydrated = False

    class Job:
        """One CI job — Sandbox twin (``sb-…``)."""

        def __init__(self, sandbox: modal.Sandbox) -> None:
            self.sandbox = sandbox

        @classmethod
        def from_id(
            cls, sandbox_id: str, *, client: modal.Client | None = None
        ) -> Runner.Job:
            return cls(modal.Sandbox.from_id(sandbox_id, client=client))

        @classmethod
        def from_name(
            cls,
            app_name: str,
            name: str,
            *,
            environment_name: str | None = None,
            client: modal.Client | None = None,
        ) -> Runner.Job:
            return cls(
                modal.Sandbox.from_name(
                    app_name,
                    name,
                    environment_name=environment_name,
                    client=client,
                )
            )

        @classmethod
        def list(
            cls,
            *,
            app_id: str | None = None,
            tags: dict[str, str] | None = None,
            client: modal.Client | None = None,
        ) -> Iterator[Runner.Job]:
            merged = {KIND_TAG: JOB_KIND, **(tags or {})}
            for sb in modal.Sandbox.list(app_id=app_id, tags=merged, client=client):
                yield cls(sb)

        @classmethod
        def create(
            cls,
            runner: Runner,
            *,
            repository: str,
            secret: modal.Secret,
            app: modal.App | None = None,
            labels: Sequence[str] | None = None,
            runner_group_id: int | None = None,
            runner_name: str | None = None,
            github_enterprise_domain: str | None = None,
            image: modal.Image | None = None,
            env: dict[str, str | None] | None = None,
            secrets: Collection[modal.Secret] | None = None,
            volumes: Mapping[
                str | os.PathLike[str], modal.Volume | modal.CloudBucketMount
            ]
            | None = None,
            timeout: int = 60 * 60,
            idle_timeout: int | None = None,
            workdir: str | None = None,
            cpu: float | None = None,
            memory: int | None = None,
            gpu: str | None = None,
            cloud: str | None = None,
            region: str | Sequence[str] | None = None,
            block_network: bool = False,
            outbound_cidr_allowlist: Sequence[str] | None = None,
            inbound_cidr_allowlist: Sequence[str] | None = None,
            proxy: modal.Proxy | None = None,
            name: str | None = None,
            tags: dict[str, str] | None = None,
            experimental_options: dict[str, object] | None = None,
            environment_name: str | None = None,
            client: modal.Client | None = None,
        ) -> Runner.Job:
            """Create a job Sandbox (eager) — same role as ``modal.Sandbox.create``.

            ``secret`` must provide ``GITHUB_TOKEN`` (typically
            ``Secret.from_name(..., required_keys=["GITHUB_TOKEN"])``). JIT mint
            runs inside the Sandbox via ``python -m runner_modal.job``.
            """
            if repository.count("/") != 1:
                raise ValueError(f"repository must be 'owner/repo', got {repository!r}")

            runner.hydrate()
            if not runner.has_capacity():
                raise ConcurrencyLimitError(
                    f"Runner {runner.name!r} at max_concurrent="
                    f"{runner.meta.max_concurrent}"
                )

            merged_labels = list(dict.fromkeys([*(labels or []), *runner.meta.labels]))
            if not merged_labels:
                raise ValueError("Job.create requires non-empty labels")

            group_id = (
                runner_group_id
                if runner_group_id is not None
                else runner.meta.runner_group_id
            )
            job_region = region if region is not None else runner.meta.region
            job_idle = (
                idle_timeout if idle_timeout is not None else runner.meta.idle_timeout
            )
            exp = dict(runner.meta.experimental_options or {})
            exp.update(experimental_options or {})
            domain = (
                github_enterprise_domain
                if github_enterprise_domain is not None
                else runner.meta.github_enterprise_domain
            )

            use_docker = bool(exp.get("vm_runtime"))
            if image is None:
                image = Runner.docker_image() if use_docker else Runner.default_image()

            vol_map: dict[
                str | os.PathLike[str], modal.Volume | modal.CloudBucketMount
            ] = dict(volumes) if volumes else {}
            if runner.volume is not None and "/cache" not in {
                str(path) for path in vol_map
            }:
                vol_map["/cache"] = runner.volume

            merged_tags: dict[str, str] = {
                **(tags or {}),
                KIND_TAG: JOB_KIND,
                POOL_TAG: runner.name,
                "repository": repository.replace("/", "_"),
            }

            spec = JobSpec(
                repository=repository,
                labels=merged_labels,
                runner_group_id=group_id,
                runner_name=runner_name or name or f"modal-{int(time.time())}",
                github_enterprise_domain=domain,
                use_docker=use_docker,
            )
            run_env = dict(env or {})
            run_env.setdefault("RUNNER_ALLOW_RUNASROOT", "1")
            run_env[JOB_SPEC_ENV] = spec.model_dump_json()

            sandbox = modal.Sandbox.create(
                "python",
                "-m",
                JOB_MODULE,
                app=app,
                image=image,
                env=run_env,
                secrets=[secret, *(secrets or [])],
                volumes=vol_map,
                timeout=timeout,
                idle_timeout=job_idle,
                workdir=workdir,
                gpu=gpu,
                cloud=cloud,
                region=job_region,
                cpu=cpu,
                memory=memory,
                block_network=block_network,
                outbound_cidr_allowlist=outbound_cidr_allowlist,
                inbound_cidr_allowlist=inbound_cidr_allowlist,
                proxy=proxy,
                name=name,
                tags=merged_tags,
                experimental_options=exp or None,
                environment_name=environment_name or runner.environment_name,
                client=client or runner.client,
            )
            return cls(sandbox)

        @property
        def object_id(self) -> str:
            return self.sandbox.object_id

        def terminate(self) -> None:
            self.sandbox.terminate()

        def wait(self, *, timeout: float | None = None) -> int | None:
            """Block until the Sandbox finishes (like ``Sandbox.wait``)."""
            if timeout is None:
                self.sandbox.wait(raise_on_termination=False)
                return self.sandbox.returncode

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:

                def wait_sandbox() -> None:
                    self.sandbox.wait(raise_on_termination=False)

                fut: concurrent.futures.Future[None] = pool.submit(wait_sandbox)
                try:
                    fut.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    self.sandbox.terminate()
                    raise JobTimeoutError(
                        f"Runner.Job {self.object_id} did not finish within {timeout}s"
                    ) from None
            return self.sandbox.returncode

        def poll(self) -> int | None:
            """Exit code if finished, else ``None`` (like ``Sandbox.poll``)."""
            return self.sandbox.poll()

        def __repr__(self) -> str:
            return f"Runner.Job.from_id({self.object_id!r})"

    @classmethod
    def control_plane_image(cls) -> modal.Image:
        """Slim Server image — installs the ``runner-modal`` package."""
        return modal.Image.debian_slim(python_version="3.12").uv_pip_install(
            "runner-modal"
        )

    @classmethod
    def install_actions_runner(
        cls, image: modal.Image, *, runner_version: str = DEFAULT_RUNNER_VERSION
    ) -> modal.Image:
        """Layer the GitHub Actions runner binary onto an image."""
        return image.run_commands(
            f"curl -fsSL -o /tmp/actions-runner.tar.gz "
            f"https://github.com/actions/runner/releases/download/"
            f"v{runner_version}/actions-runner-linux-x64-{runner_version}.tar.gz",
            "mkdir -p /actions-runner",
            "tar -xzf /tmp/actions-runner.tar.gz -C /actions-runner",
            "rm /tmp/actions-runner.tar.gz",
            "chmod +x /actions-runner/run.sh /actions-runner/config.sh",
        )

    @classmethod
    def default_image(
        cls, *, runner_version: str = DEFAULT_RUNNER_VERSION
    ) -> modal.Image:
        """gVisor job image with Actions runner (CPU/GPU)."""
        return cls.install_actions_runner(
            modal.Image.debian_slim(python_version="3.12")
            .uv_pip_install("runner-modal")
            .apt_install(
                "curl",
                "ca-certificates",
                "git",
                "libicu-dev",
                "liblttng-ust1",
                "libssl3",
                "tar",
                "unzip",
                "zip",
            )
            .env({"RUNNER_ALLOW_RUNASROOT": "1"}),
            runner_version=runner_version,
        )

    @classmethod
    def docker_image(
        cls, *, runner_version: str = DEFAULT_RUNNER_VERSION
    ) -> modal.Image:
        """VM job image with dockerd (no GPU)."""
        return cls.install_actions_runner(
            modal.Image.from_registry("ubuntu:24.04")
            .env({"DEBIAN_FRONTEND": "noninteractive", "RUNNER_ALLOW_RUNASROOT": "1"})
            .uv_pip_install("runner-modal")
            .apt_install(
                "curl",
                "ca-certificates",
                "git",
                "docker.io",
                "docker-buildx",
                "libicu-dev",
                "tar",
                "unzip",
                "zip",
            ),
            runner_version=runner_version,
        )

    @classmethod
    def create(
        cls,
        app: modal.App,
        name: str,
        *,
        secret: modal.Secret,
        labels: Sequence[str] | None = None,
        max_concurrent: int | None = None,
        cache: bool = True,
        image: modal.Image | None = None,
        volumes: Mapping[str | PurePosixPath, modal.Volume | modal.CloudBucketMount]
        | None = None,
        env: dict[str, str | None] | None = None,
        compute_region: str | Sequence[str] | None = None,
        routing_region: str = "us-east",
        cloud: str | None = None,
        min_containers: int = 1,
        max_containers: int | None = None,
        unauthenticated: bool = True,
        gpu: str | list[str] | None = None,
        cpu: float | tuple[float, float] | None = None,
        memory: int | tuple[int, int] | None = None,
        target_concurrency: int | None = None,
        buffer_containers: int | None = None,
        scaleup_window: int | None = None,
        scaledown_window: int | None = None,
        startup_timeout: int = 30,
        port: int = 8000,
        ephemeral_disk: int | None = None,
        proxy: modal.Proxy | None = None,
        environment_name: str | None = None,
        client: modal.Client | None = None,
        idle_timeout: int | None = None,
        experimental_options: dict[str, object] | None = None,
        runner_group_id: int | None = None,
        github_enterprise_domain: str | None = None,
    ) -> Self:
        """Register control plane on ``app`` (definition-time — not a Sandbox).

        Call once per App. Runner ``name`` identifies Dict/Volume state; the Modal
        Server class is always ``GitHubServer`` (Modal has no Server parameters).

        ``secret`` must be a named Secret (``Secret.from_name``) with
        ``GITHUB_TOKEN`` and ``WEBHOOK_SECRET``. The name is persisted so the
        webhook can pass the same Secret into ``Job.create``.

        ``cache=True`` mounts a shared Volume at ``/cache`` on job Sandboxes — this is
        not the GitHub Actions cache service (``actions/cache``).
        """
        if not RUNNER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid Runner name {name!r}")
        if not app.name:
            raise ValueError(
                "Runner.create requires a named App, e.g. modal.App('acme-ci')"
            )
        secret_name = secret.name
        if not secret_name:
            raise ValueError(
                "Runner.create requires a named Secret (modal.Secret.from_name)"
            )

        runner = cls.from_name(
            name,
            create_if_missing=True,
            environment_name=environment_name,
            labels=labels,
            max_concurrent=max_concurrent,
            cache=cache,
            client=client,
        )
        runner.hydrate()
        prior = runner.meta
        if compute_region is None:
            region_default: str | list[str] | None = prior.region
        elif isinstance(compute_region, str):
            region_default = compute_region
        else:
            region_default = list(compute_region)

        runner.meta = RunnerMeta(
            name=name,
            labels=list(labels) if labels is not None else list(prior.labels),
            max_concurrent=(
                max_concurrent if max_concurrent is not None else prior.max_concurrent
            ),
            cache=cache,
            app_name=app.name,
            server_name=SERVER_CLASS_NAME,
            secret_name=secret_name,
            github_enterprise_domain=(
                github_enterprise_domain
                if github_enterprise_domain is not None
                else prior.github_enterprise_domain
            ),
            region=region_default,
            idle_timeout=(
                idle_timeout if idle_timeout is not None else prior.idle_timeout
            ),
            experimental_options=(
                dict(experimental_options)
                if experimental_options is not None
                else prior.experimental_options
            ),
            runner_group_id=(
                runner_group_id
                if runner_group_id is not None
                else prior.runner_group_id
            ),
        )
        runner.persist_meta()

        vol_map: dict[str | PurePosixPath, modal.Volume | modal.CloudBucketMount] = (
            dict(volumes) if volumes else {}
        )
        if (
            cache
            and runner.volume is not None
            and not any(str(path) == "/cache" for path in vol_map)
        ):
            vol_map["/cache"] = runner.volume

        run_env = dict(env or {})
        run_env["RUNNER_MODAL_NAME"] = name

        app.server(
            image=image if image is not None else cls.control_plane_image(),
            secrets=[secret],
            volumes=vol_map,
            env=run_env,
            compute_region=compute_region,
            routing_region=routing_region,
            cloud=cloud,
            min_containers=min_containers,
            max_containers=max_containers,
            unauthenticated=unauthenticated,
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            target_concurrency=target_concurrency,
            buffer_containers=buffer_containers,
            scaleup_window=scaleup_window,
            scaledown_window=scaledown_window,
            startup_timeout=startup_timeout,
            port=port,
            ephemeral_disk=ephemeral_disk,
            proxy=proxy,
        )(GitHubServer)
        return runner

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        environment_name: str | None = None,
        create_if_missing: bool = False,
        labels: Sequence[str] | None = None,
        max_concurrent: int | None = None,
        cache: bool = True,
        client: modal.Client | None = None,
    ) -> Self:
        """Reference a Runner by name (Volume-shaped; hydrate on first use)."""
        if not RUNNER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid Runner name {name!r}")
        return cls(
            name,
            environment_name=environment_name,
            create_if_missing=create_if_missing,
            labels=labels,
            max_concurrent=max_concurrent,
            cache=cache,
            client=client,
        )

    @classmethod
    @contextmanager
    def ephemeral(
        cls,
        *,
        labels: Sequence[str] | None = None,
        max_concurrent: int | None = None,
        cache: bool = True,
        client: modal.Client | None = None,
    ) -> Generator[Self, None, None]:
        """Anonymous Runner for the duration of the context (Volume.ephemeral)."""
        name = f"ephemeral_{uuid.uuid4().hex}"
        runner = cls.from_name(
            name,
            create_if_missing=True,
            labels=labels,
            max_concurrent=max_concurrent,
            cache=cache,
            client=client,
        )
        try:
            runner.hydrate()
            yield runner
        finally:
            cls.objects.delete(name, client=client)

    def __repr__(self) -> str:
        return f"Runner.from_name({self.name!r})"

    def hydrate(self) -> None:
        if self.hydrated:
            return

        self.meta_dict = modal.Dict.from_name(
            f"{self.name}-runner-meta",
            environment_name=self.environment_name,
            create_if_missing=self.create_if_missing,
            client=self.client,
        )
        existing = self.meta_dict.get(META_KEY)
        if existing is None:
            if not self.create_if_missing:
                raise LookupError(
                    f"Runner {self.name!r} does not exist; "
                    "call Runner.create(...) first"
                )
            written = self.meta_dict.put(
                META_KEY, self.meta.model_dump(mode="json"), skip_if_exists=True
            )
            if not written:
                existing = self.meta_dict.get(META_KEY)
                if existing is None:
                    raise LookupError(f"Runner {self.name!r} meta init race")
                self.meta = RunnerMeta.model_validate(existing)
        else:
            self.meta = RunnerMeta.model_validate(existing)

        self.bind_volume()
        self.hydrated = True

    def bind_volume(self) -> None:
        if self.meta.cache:
            self.volume = modal.Volume.from_name(
                f"{self.name}-cache",
                environment_name=self.environment_name,
                create_if_missing=True,
                client=self.client,
            )
        else:
            self.volume = None

    def persist_meta(self) -> None:
        """Write ``self.meta`` to the Dict (does not reload over local meta)."""
        if self.meta_dict is None:
            self.meta_dict = modal.Dict.from_name(
                f"{self.name}-runner-meta",
                environment_name=self.environment_name,
                create_if_missing=True,
                client=self.client,
            )
        self.meta_dict[META_KEY] = self.meta.model_dump(mode="json")
        self.bind_volume()
        self.hydrated = True

    def github_secret(self) -> modal.Secret:
        """Named Secret for Job JIT mint (``GITHUB_TOKEN``)."""
        self.hydrate()
        if not self.meta.secret_name:
            raise LookupError(
                f"Runner {self.name!r} has no secret_name; "
                "call Runner.create(secret=...) first"
            )
        return modal.Secret.from_name(
            self.meta.secret_name,
            required_keys=["GITHUB_TOKEN"],
            environment_name=self.environment_name,
            client=self.client,
        )

    def admits(self, labels: Sequence[str]) -> bool:
        """Whether webhook labels match this pool (after hydrate)."""
        self.hydrate()
        pool = set(self.meta.labels)
        job_labels = set(labels)
        return bool(pool) and "self-hosted" in job_labels and pool <= job_labels

    @property
    def url(self) -> str | None:
        """Server URL after deploy, or ``None`` if not ready (like ``Server.get_url``).

        Absence is ``None``; use ``if url is None`` rather than expecting an exception.
        """
        self.hydrate()
        if not self.meta.app_name or not self.meta.server_name:
            return None
        try:
            server = modal.Server.from_name(
                self.meta.app_name,
                self.meta.server_name,
                environment_name=self.environment_name,
                client=self.client,
            )
            return server.get_url()
        except modal.exception.Error:
            return None

    def active_count(self, *, app_id: str | None = None) -> int:
        tags: dict[str, str] = {KIND_TAG: JOB_KIND, POOL_TAG: self.name}
        return sum(
            1 for _ in modal.Sandbox.list(app_id=app_id, tags=tags, client=self.client)
        )

    def has_capacity(self, *, app_id: str | None = None) -> bool:
        """Soft LBYL concurrency check (list-then-create; not a reservation).

        ``Job.create`` still raises ``ConcurrencyLimitError`` if full at call time.
        Concurrent creators can overshoot ``max_concurrent``.
        """
        self.hydrate()
        limit = self.meta.max_concurrent
        if limit is None:
            return True
        return self.active_count(app_id=app_id) < limit

    def info(self, *, app_id: str | None = None) -> RunnerInfo:
        self.hydrate()
        return RunnerInfo(
            name=self.meta.name,
            labels=list(self.meta.labels),
            max_concurrent=self.meta.max_concurrent,
            cache=self.meta.cache,
            active_runners=self.active_count(app_id=app_id),
            app_name=self.meta.app_name,
            server_name=self.meta.server_name,
            secret_name=self.meta.secret_name,
        )
