"""Fixtures for the integration layer.

The testcontainers stack needs nine project-specific settings. They are applied
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
   Neo4j, Postgres, RabbitMQ, Redis, HAProxy, cAdvisor and VictoriaMetrics. On
   four cores that contention is what makes startup miss
   ``infrahub-server``'s healthcheck, which allows about 110 seconds and is
   hardcoded in the packaged compose file where no variable reaches it. One
   server of two workers and one task worker is three processes for the same
   coverage: the load balancer still fronts the API and the worker still runs
   the pipeline. ``INFRAHUB_TESTING_WEB_CONCURRENCY`` is the one that has to be
   set before ``infrahub_testcontainers.container`` is imported, because its
   default entrypoint interpolates the value at import time; a conftest at this
   level runs before the test module that imports it.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404
from pathlib import Path
from typing import Any

import pytest
from infrahub_sdk.yaml import SchemaFile

CURRENT_DIRECTORY = Path(__file__).parent.resolve()

TESTING_IMAGE = "opsmill/infrahub-demo-otn"
# Mirrors the tag docker-compose.override.yml builds and the Dockerfile default.
TESTING_IMAGE_VERSION = os.environ.get("INFRAHUB_BASE_VERSION", "1.11.0")

os.environ.setdefault("INFRAHUB_TESTING_DOCKER_IMAGE", TESTING_IMAGE)
os.environ.setdefault("INFRAHUB_TESTING_IMAGE_VERSION", TESTING_IMAGE_VERSION)
os.environ.setdefault("INFRAHUB_TESTING_DOCKER_PULL", "false")
os.environ.setdefault("INFRAHUB_TESTING_TASKMGR_BACKGROUND_SVC_REPLICAS", "1")
os.environ.setdefault("INFRAHUB_TESTING_TIMEOUT", "300")
os.environ.setdefault("INFRAHUB_TIMEOUT", "300")
os.environ.setdefault("INFRAHUB_TESTING_API_SERVER_COUNT", "1")
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


@pytest.fixture
def root_directory() -> Path:
    """The root directory of the repository."""
    return CURRENT_DIRECTORY.parent.parent


@pytest.fixture
def schemas_directory(root_directory: Path) -> Path:
    return root_directory / "schemas"


@pytest.fixture
def schemas(schemas_directory: Path) -> list[dict[str, Any]]:
    schema_files = SchemaFile.load_from_disk(paths=[schemas_directory])
    return [item.content for item in schema_files if item.content]
