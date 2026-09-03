"""The Infrahub version is declared in six places; they must agree.

Nothing derives this version from anything else. The Dockerfile needs it as a
build arg before any Python runs, Compose needs it to tag the image it builds,
and the test stack needs it to know which image to pull, so each states it
independently. That is six literals that a bump has to move together, and the
failure when one is missed is not a syntax error: the stack builds one version
and the tests exercise another, which surfaces as behaviour nobody can
reproduce locally.

`update-infrahub.yml` moves all six. This module is what makes that claim
checkable, and what fails the moment a seventh declaration appears somewhere
the workflow does not know about.

The Dockerfile is the reference because it is the one the image is actually
built from. Everything else is compared against it rather than against a
constant restated here, so this file needs no edit when the version moves.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"
TASKS = REPO_ROOT / "tasks.py"
INTEGRATION_CONFTEST = REPO_ROOT / "tests" / "integration" / "conftest.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Files that must state the version NOWHERE. The six declarations above are the
# ones a bump moves; these two were a seventh and an eighth that nothing moved
# and nothing checked, so the rule for them is not "agree with the Dockerfile"
# but "carry no version at all".
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _base_version() -> str:
    """The version the image is built from, read from the Dockerfile.

    Returns:
        The default value of the ``INFRAHUB_BASE_VERSION`` build arg.
    """
    match = re.search(r"^ARG INFRAHUB_BASE_VERSION=(\S+)$", DOCKERFILE.read_text(), re.MULTILINE)
    assert match, "Dockerfile no longer declares `ARG INFRAHUB_BASE_VERSION=<version>`"
    return match.group(1)


def test_the_dockerfile_declares_a_base_version() -> None:
    """The reference every other assertion here is made against."""
    assert re.fullmatch(r"\d+\.\d+\.\d+[0-9a-z.]*", _base_version()), (
        f"the base version {_base_version()!r} does not look like an Infrahub release"
    )


def test_the_dockerfile_builds_from_the_arg_it_declares() -> None:
    """A `FROM` with a literal tag would silently ignore the arg."""
    assert "FROM registry.opsmill.io/opsmill/infrahub:${INFRAHUB_BASE_VERSION}" in DOCKERFILE.read_text(), (
        "the FROM line no longer interpolates INFRAHUB_BASE_VERSION, so the build arg does nothing"
    )


@pytest.mark.parametrize(
    ("path", "pattern", "what"),
    [
        (COMPOSE_OVERRIDE, r"image: opsmill/infrahub-demo-otn:\$\{INFRAHUB_BASE_VERSION:-([^}]+)\}", "the image tag"),
        (
            COMPOSE_OVERRIDE,
            r"INFRAHUB_BASE_VERSION: \"\$\{INFRAHUB_BASE_VERSION:-([^}]+)\}\"",
            "the build arg passed to the Dockerfile",
        ),
        (TASKS, r'BASE_VERSION = os\.getenv\("INFRAHUB_BASE_VERSION", "([^"]+)"\)', "the version `invoke build` tags"),
        (
            INTEGRATION_CONFTEST,
            r'TESTING_IMAGE_VERSION = os\.environ\.get\("INFRAHUB_BASE_VERSION", "([^"]+)"\)',
            "the image the test stack runs",
        ),
    ],
)
def test_every_fallback_matches_the_dockerfile(path: Path, pattern: str, what: str) -> None:
    """Each site states the same version as its own environment-variable fallback.

    Args:
        path: File declaring the version.
        pattern: Expression whose first group is the declared version.
        what: What that declaration controls, for the failure message.
    """
    match = re.search(pattern, path.read_text())
    assert match, f"{path.relative_to(REPO_ROOT)} no longer declares {what} in the shape this guard expects"
    assert match.group(1) == _base_version(), (
        f"{path.relative_to(REPO_ROOT)} sets {what} to {match.group(1)}, "
        f"but the Dockerfile builds {_base_version()}. A bump moved one and not the other."
    )


@pytest.mark.parametrize(
    ("path", "why"),
    [
        (
            ENV_EXAMPLE,
            "`cp .env.example .env` would hand a fresh clone a version no bump moves, and "
            "tasks.py::_load_env reads .env, so that stale value wins over every fallback",
        ),
        (
            CI_WORKFLOW,
            "the docker-build job would keep building the previous release after a bump, "
            "proving the worker imports the shared package against the wrong version",
        ),
    ],
)
def test_no_version_literal_leaks_into_a_file_no_bump_rewrites(path: Path, why: str) -> None:
    """Neither file may state an Infrahub version, in code or in prose.

    Prose counts. The sweep at the end of `update-infrahub.yml` is a fixed-string
    search, so a version left in a comment fails the bump run just as a live one
    does, and the fix would be to edit the comment under time pressure.

    Args:
        path: File that must carry no version.
        why: What goes wrong when it does, for the failure message.
    """
    found = re.findall(r"\b\d+\.\d+\.\d+[0-9a-z.]*\b", path.read_text())
    # The uv and Node pins in ci.yml are versions of other things entirely, and
    # they are not what a bump moves. Only the Infrahub line is in scope.
    leaked = [version for version in found if version == _base_version()]
    assert not leaked, (
        f"{path.relative_to(REPO_ROOT)} states the Infrahub version {_base_version()}, which it must not: {why}. "
        f"Take the number out; the value comes from the Dockerfile's ARG default at build time."
    )


def test_testcontainers_is_pinned_to_the_version_the_stack_runs() -> None:
    """The package that starts the stack and the image it starts must match.

    `infrahub-testcontainers` ships the compose file the stack is built from, so
    a mismatch pairs one release's topology with another's server.
    """
    dependencies = tomllib.loads(PYPROJECT.read_text())["dependency-groups"]["dev"]
    pins = [d for d in dependencies if d.startswith("infrahub-testcontainers")]
    assert len(pins) == 1, f"expected exactly one infrahub-testcontainers pin, found {pins}"
    assert pins[0] == f"infrahub-testcontainers=={_base_version()}", (
        f"{pins[0]!r} does not pin the version the Dockerfile builds ({_base_version()})"
    )
