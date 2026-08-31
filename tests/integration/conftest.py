"""Fixtures for the integration layer.

The testcontainers stack needs four project-specific settings. They are applied
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
