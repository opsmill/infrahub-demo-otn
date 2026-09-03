"""Fixtures for the integration layer.

The testcontainers stack needs eight project-specific settings. They are applied
at import time so they land before ``infrahub_testcontainers`` snapshots the
environment, and through ``setdefault`` so an explicit environment variable
still wins:

1. The default stack runs the vanilla ``opsmill/infrahub`` image, which has no
   ``infrahub_demo_otn`` module. Every check, transform and generator import
   fails during repository sync. Point it at the image ``invoke build``
   produces.
2. Skip the registry pull. That image only exists locally.
3. ``docker compose up --wait`` fails on a project containing a zero-replica
   service, reporting it as a missing dependency. That is a Compose bug
   (docker/compose#13899), not a stack problem. The stack declares
   ``task-manager-background-svc`` with ``replicas: 0`` and nothing depends on
   it, so scheduling one replica is a harmless workaround. Drop it once Compose
   ships the fix.
4. ``task-worker`` declares ``depends_on: [infrahub-server]`` in short form,
   which waits for the container to start and not for the application to be
   ready. The worker's first call is a GraphQL query it gives
   ``INFRAHUB_TIMEOUT`` seconds to answer, and the stock 60 is not enough on a
   four-core GitHub runner: two API servers of four gunicorn workers each,
   two task workers, Neo4j, Postgres, RabbitMQ, Redis, HAProxy, cAdvisor and
   VictoriaMetrics contend for the cores, and the observed run took the API
   server 3m17s to answer its first request. The workers timed out at 60s,
   exited 1, and took ``docker compose up --wait`` down with them, so every
   test errored in fixture setup. HAProxy is configured with ``timeout server
   0``, so the longer deadline simply lets the worker wait for the answer.
5. The same slowness reaches the host side. ``Config`` reads ``INFRAHUB_TIMEOUT``
   from the environment and defaults to 60 seconds, and ``TestInfrahubDocker``'s
   ``execute_command`` hands its subprocesses a copy of that environment, so one
   variable covers both the ``client`` fixture and every ``infrahubctl``
   invocation. ``infrahubctl schema load`` on a cold deployment is the call that
   exceeds the default first. Set here rather than in ``ci.yml`` so a laptop and
   a runner agree.
6. The stack ships sized for a workstation: two API servers of four gunicorn
   workers each and two task workers, which is ten Infrahub processes beside
   Neo4j, Postgres, RabbitMQ, Redis, HAProxy, cAdvisor and VictoriaMetrics. Four
   cores cannot run that well, so the gunicorn count per server and the task
   worker count come down.

   The API server count deliberately does not. It was cut to one for a while,
   to get the server healthy inside the roughly 110 seconds the packaged
   compose file allowed, and it cost more than it bought: one server saturates
   under pipeline load and answers HTTP 429, the SDK backs off ten seconds per
   retry, and a check that spends its sixty-second Prefect budget on backoff
   fails rather than finishing. The proposed-change test then reports a check
   that never produced a validator. Runs that failed that way logged 350 and
   394 rate-limited responses; the run that passed logged none.
   ``tests/integration/stack.py`` now buys the startup time instead, so the
   second server stays.

   ``INFRAHUB_TESTING_WEB_CONCURRENCY`` is the one that has to be set before
   ``infrahub_testcontainers.container`` is imported, because its default
   entrypoint interpolates the value at import time; a conftest at this level
   runs before the test module that imports it.
"""

from __future__ import annotations

import os
import shlex
import subprocess  # noqa: S404
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from infrahub_sdk.yaml import SchemaFile

CURRENT_DIRECTORY = Path(__file__).parent.resolve()
REPO_ROOT = CURRENT_DIRECTORY.parent.parent

TESTING_IMAGE = "opsmill/infrahub-demo-otn"
# Mirrors the tag docker-compose.override.yml builds and the Dockerfile default.
TESTING_IMAGE_VERSION = os.environ.get("INFRAHUB_BASE_VERSION", "1.11.0")

os.environ.setdefault("INFRAHUB_TESTING_DOCKER_IMAGE", TESTING_IMAGE)
os.environ.setdefault("INFRAHUB_TESTING_IMAGE_VERSION", TESTING_IMAGE_VERSION)
os.environ.setdefault("INFRAHUB_TESTING_DOCKER_PULL", "false")
os.environ.setdefault("INFRAHUB_TESTING_TASKMGR_BACKGROUND_SVC_REPLICAS", "1")
os.environ.setdefault("INFRAHUB_TESTING_TIMEOUT", "300")
os.environ.setdefault("INFRAHUB_TIMEOUT", "300")
os.environ.setdefault("INFRAHUB_TESTING_TASK_WORKER_COUNT", "1")
os.environ.setdefault("INFRAHUB_TESTING_WEB_CONCURRENCY", "2")


@pytest.fixture(scope="session", autouse=True)
def require_testing_image() -> None:
    """Fail loudly, naming the fix, when the local image has not been built."""
    image = f"{os.environ['INFRAHUB_TESTING_DOCKER_IMAGE']}:{os.environ['INFRAHUB_TESTING_IMAGE_VERSION']}"
    inspect = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", image],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.fail(f"Docker image {image!r} is missing; run `uv run invoke build` before the integration tests")


def run_task(
    task: str,
    arguments: str = "",
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one task the way a reader runs it, and hand back the finished process.

    `COLUMNS` is set so `rich` never wraps a task name or a figure off the line
    a postcondition reads it from.
    """
    return subprocess.run(  # noqa: S603
        ["uv", "run", "invoke", task, *shlex.split(arguments)],  # noqa: S607
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "COLUMNS": "200", **(env or {})},
        timeout=timeout,
    )


def task_output(result: subprocess.CompletedProcess[str], command: str) -> str:
    """Everything the task printed, after failing on a non-zero exit."""
    assert result.returncode == 0, (
        f"`uv run invoke {command}` exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    return result.stdout + result.stderr


def _assert_a_task_reports(address: str) -> None:
    """Fail unless `invoke info` names the address just exported.

    A developer's `.env` points at a different stack on port 8000, so an
    override that failed to take would not error. It would run every task
    against the wrong server and quite possibly pass.
    """
    reported = run_task("info")
    if address not in reported.stdout:
        pytest.fail(
            f"`invoke info` did not report {address}, so task subprocesses are pointed "
            f"somewhere else:\n{reported.stdout}\n{reported.stderr}"
        )


@pytest.fixture(scope="class")
def task_environment(infrahub_port: int) -> Iterator[str]:
    """Export the testcontainers address and token, so task subprocesses inherit them.

    `tasks.py::_env()` puts `os.environ` ahead of `.env`, which is what lets this
    redirect a task. `infrahub_testcontainers.container` is imported inside the
    fixture rather than at module level because the settings above have to land
    before that module loads.
    """
    from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES  # noqa: PLC0415

    address = f"http://localhost:{infrahub_port}"
    overrides = {
        "INFRAHUB_ADDRESS": address,
        "INFRAHUB_API_TOKEN": PROJECT_ENV_VARIABLES["INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"],
    }
    previous = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        _assert_a_task_reports(address)
        yield address
    finally:
        for name, value in previous.items():
            if value is None:
                del os.environ[name]
            else:
                os.environ[name] = value


@pytest.fixture
def root_directory() -> Path:
    """The root directory of the repository."""
    return REPO_ROOT


@pytest.fixture
def schemas_directory(root_directory: Path) -> Path:
    return root_directory / "schemas"


@pytest.fixture
def schemas(schemas_directory: Path) -> list[dict[str, Any]]:
    schema_files = SchemaFile.load_from_disk(paths=[schemas_directory])
    return [item.content for item in schema_files if item.content]
