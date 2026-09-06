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

That promise was made before and not kept: `.env.example` and `ci.yml` each
held one, neither was rewritten and neither was checked, so a bump moved six of
eight. Both now carry none, and the last two tests here hold them to it.

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


LEAK_CANDIDATES = [
    (
        ENV_EXAMPLE,
        "`cp .env.example .env` hands a fresh clone a version no bump moves. `tasks.py::_run` "
        "passes `_env()`, which layers `.env` over the process environment, to every subprocess, "
        "so the stale value reaches `docker compose` and the override file's "
        "`${INFRAHUB_BASE_VERSION:-...}` resolves to it. `BASE_VERSION` in tasks.py reads "
        "`os.getenv` and does not see `.env`, so `invoke build` tags the new release while the "
        "stack it starts asks for the old one",
    ),
    (
        CI_WORKFLOW,
        "the docker-build job would keep building the previous release after a bump, proving the "
        "worker imports the shared package against the wrong version",
    ),
]

# The `why` strings are paragraphs, so pytest's generated ids are unreadable in a
# failure summary. Name the cases by file instead.
LEAK_IDS = [path.name for path, _ in LEAK_CANDIDATES]


@pytest.mark.parametrize(("path", "why"), LEAK_CANDIDATES, ids=LEAK_IDS)
def test_neither_file_declares_the_base_version_at_any_value(path: Path, why: str) -> None:
    """Neither file may assign `INFRAHUB_BASE_VERSION` a version, current or stale.

    Keyed on the variable name rather than on the value, which is what makes this
    catch the case that matters. A declaration left behind by a bump holds the
    version being moved *off*, so a check comparing against the Dockerfile would
    pass it: the number no longer matches, which is precisely the bug. Matching
    the assignment catches it whatever it says.

    `UV_VERSION` and the pinned actions in ci.yml are versions of other things and
    are not what a bump moves, so naming the variable also avoids them without a
    value allowlist.

    Args:
        path: File that must declare no base version.
        why: What goes wrong when it does, for the failure message.
    """
    declarations = re.findall(r"INFRAHUB_BASE_VERSION[=:\s]+[\"']?(\d+\.\d+\.\d+[0-9a-z.]*)", path.read_text())
    assert not declarations, (
        f"{path.relative_to(REPO_ROOT)} sets INFRAHUB_BASE_VERSION to {declarations[0]}, which it must not: "
        f"{why}. Take the assignment out; the Dockerfile's ARG default supplies the value."
    )


@pytest.mark.parametrize(("path", "why"), LEAK_CANDIDATES, ids=LEAK_IDS)
def test_neither_file_mentions_the_version_the_dockerfile_builds(path: Path, why: str) -> None:
    """Neither file may contain the current version anywhere, prose included.

    The second layer, and it guards the bump rather than the repository. The sweep
    at the end of `update-infrahub.yml` is a fixed-string search for the version
    being moved off, so a number left in a comment fails the bump run just as a
    live declaration would, and the fix would land under time pressure. Catching
    it here means it fails on the pull request that wrote the comment.

    Args:
        path: File that must not name the current version.
        why: What goes wrong when it does, for the failure message.
    """
    found = re.findall(r"\b\d+\.\d+\.\d+[0-9a-z.]*\b", path.read_text())
    leaked = [version for version in found if version == _base_version()]
    assert not leaked, (
        f"{path.relative_to(REPO_ROOT)} names the Infrahub version {_base_version()}, which it must not: {why}. "
        f"`update-infrahub.yml` sweeps both files for the outgoing version, and a mention in prose trips it."
    )


def test_testcontainers_declares_the_version_the_stack_runs() -> None:
    """The package that starts the stack and the image it starts must match.

    `infrahub-testcontainers` ships the compose file the stack is built from, so
    a mismatch pairs one release's topology with another's server.

    Read operator-agnostically. The specifier is a floor rather than a pin, so
    what has to equal the Dockerfile is the *version named in it*, not the whole
    string. Asserting the string meant this test had to be edited the day the
    house specifier policy changed, which is exactly the coupling the rest of
    this module avoids by deriving everything from the Dockerfile.
    """
    dependencies = tomllib.loads(PYPROJECT.read_text())["dependency-groups"]["dev"]
    declarations = [d for d in dependencies if d.startswith("infrahub-testcontainers")]
    assert len(declarations) == 1, f"expected exactly one infrahub-testcontainers declaration, found {declarations}"
    match = re.fullmatch(r"infrahub-testcontainers(\[[^]]*\])?[=<>~!]+([^,]+)", declarations[0])
    assert match, f"{declarations[0]!r} is not a specifier this guard knows how to read"
    assert match.group(2) == _base_version(), (
        f"{declarations[0]!r} names {match.group(2)}, but the Dockerfile builds {_base_version()}. "
        f"A bump moved one and not the other."
    )
