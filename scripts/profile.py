"""Stage-time the Job.create critical path (throwaway; not product API).

Matches the #4 shape: named Secret, JIT mint inside the Sandbox
(``python -m runner_modal.job``), Named Image when meta has ``job_image_name``.

Run from repo root:

  modal run scripts/profile.py
  modal run scripts/profile.py --with-job --repository owner/repo
  modal run scripts/profile.py --with-job --with-online --repository owner/repo
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import modal

from runner_modal import Runner
from runner_modal.profile import StageClock

app = modal.App("runner-modal-profile")

POOL = "profile"
APP_NAME = "runner-modal-profile"
SECRET_NAME = "github-runner"


@app.local_entrypoint()
def main(
    repository: str = "botirkhaltaev/runic",
    with_job: bool = False,
    with_volume: bool = True,
    with_online: bool = False,
) -> None:
    os.environ.setdefault("RUNNER_MODAL_PROFILE", "1")
    parent = modal.App.lookup(APP_NAME, create_if_missing=True)
    gh = modal.Secret.from_name(SECRET_NAME, required_keys=["GITHUB_TOKEN"])

    with StageClock("Runner.objects.create(allow_existing)"):
        Runner.objects.create(
            POOL,
            allow_existing=True,
            labels=["self-hosted", "modal", "profile"],
            max_concurrent=20,
            cache=with_volume,
        )

    with StageClock("Runner.from_name"):
        runner = Runner.from_name(POOL)
    with StageClock("hydrate (cold)"):
        runner.hydrate()
    with StageClock("hydrate (warm no-op)"):
        runner.hydrate()

    # Publish Named Image the same way Runner.create does.
    job_image_name = f"{POOL}-job"
    use_docker = bool((runner.meta.experimental_options or {}).get("vm_runtime"))
    recipe = Runner.docker_image() if use_docker else Runner.default_image()
    with StageClock(f"Image.publish → {job_image_name}") as publish:
        recipe.build(parent).publish(job_image_name)
    runner.meta = runner.meta.model_copy(update={"job_image_name": job_image_name})
    runner.persist_meta()
    print(f"         meta.job_image_name={runner.meta.job_image_name!r}")

    with StageClock("active_count / Sandbox.list"):
        runner.active_count()
    with StageClock("has_capacity"):
        runner.has_capacity()

    d = modal.Dict.from_name(f"{POOL}-profile-dict", create_if_missing=True)
    key = f"k-{uuid.uuid4().hex}"

    with StageClock("Dict put skip_if_exists (claim)"):
        d.put(key, {"status": "pending", "ts": time.time()}, skip_if_exists=True)
    with StageClock("Dict get (hit)"):
        d.get(key)
    with StageClock("Dict set (mark_done)"):
        d[key] = {"status": "done"}

    name = f"prof-echo-{int(time.time())}"
    vols: dict[str, modal.Volume] = {}
    if with_volume and runner.volume is not None:
        vols["/cache"] = runner.volume

    with StageClock(f"Sandbox.create echo (volume={bool(vols)})") as echo:
        sb = modal.Sandbox.create(
            "bash",
            "-lc",
            "echo ready; sleep 2",
            app=parent,
            image=modal.Image.debian_slim(python_version="3.12"),
            volumes=vols or None,
            timeout=60,
            idle_timeout=30,
            region="us-east",
            name=name,
            tags={"runner_modal": "profile"},
        )
    with StageClock("Sandbox.exec echo"):
        sb.exec("bash", "-lc", "echo hi").wait()
    sb.terminate()

    with StageClock("Image.from_name (published job image)"):
        modal.Image.from_name(job_image_name)

    if with_job:
        labels = ["self-hosted", "modal", "profile", f"job-{int(time.time())}"]
        runner_reg_name = f"profile-{int(time.time())}"
        job_name = f"prof-job-{int(time.time())}"

        with StageClock("Job.create (named image + in-job JIT)") as job_lap:
            job = Runner.Job.create(
                runner,
                app=parent,
                repository=repository,
                labels=labels,
                secret=gh,
                name=job_name,
                runner_name=runner_reg_name,
                region="us-east",
                timeout=120,
                idle_timeout=60,
            )
        print(f"         id  {job.object_id}")
        print(
            f"\nJob.create wall ≈ {job_lap.ms:.0f} ms  "
            f"(publish was {publish.ms:.0f} ms, separate from hot path)"
        )

        if with_online:
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                # Prefer reading via gh when Secret is only on Modal.
                import subprocess

                token = subprocess.check_output(
                    ["gh", "auth", "token"], text=True
                ).strip()
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            owner, repo = repository.split("/", 1)
            t_online0 = time.perf_counter()
            first_seen = None
            online_at = None
            for _ in range(60):
                r = httpx.get(
                    f"https://api.github.com/repos/{owner}/{repo}/actions/runners",
                    headers=headers,
                    timeout=30.0,
                )
                r.raise_for_status()
                for row in r.json().get("runners", []):
                    if row.get("name") != runner_reg_name:
                        continue
                    if first_seen is None:
                        first_seen = time.perf_counter()
                        print(
                            f"{(first_seen - t_online0) * 1000:8.1f} ms  "
                            f"GH runner first seen status={row.get('status')!r}"
                        )
                    if row.get("status") == "online":
                        online_at = time.perf_counter()
                        print(
                            f"{(online_at - t_online0) * 1000:8.1f} ms  "
                            f"GH runner online"
                        )
                        break
                if online_at:
                    break
                time.sleep(0.5)
            if online_at is None:
                print("         GH runner never online within 60s")
            elif first_seen is not None:
                print(
                    f"         create→first_seen="
                    f"{(first_seen - t_online0) * 1000:.0f} ms  "
                    f"first_seen→online="
                    f"{(online_at - first_seen) * 1000:.0f} ms"
                )

        time.sleep(2)
        job.terminate()
    else:
        print(
            "\n(skip Job.create — pass --with-job for full path; "
            "--with-online for create→online split)"
        )
        print(
            f"echo Sandbox.create alone ≈ {echo.ms:.0f} ms "
            "(lower bound before Actions runner image)"
        )
