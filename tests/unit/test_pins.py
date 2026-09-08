"""The Infrahub version is declared once, and no file may restate it.

`src/infrahub_demo_otn/baseversion.py` resolves it from the installed
`infrahub-testcontainers`. That is the whole model, and these tests hold the
tree to it: the resolver behaves, and nothing anywhere writes a version literal
that could go stale beside it.

It used to be six literals across five files that a bump had to move together,
guarded by a module that compared all six against the Dockerfile. The failure
was never a syntax error: the stack built one version and the tests exercised
another. Worse, the six could only be moved by the one workflow that knew all
five files, so a Dependabot lock refresh, which moves the version that actually
installs, produced a pull request that could not be made green.

Deriving instead of declaring removes both problems. What has to be guarded is
no longer agreement between six copies but the absence of a second copy, which
is what most of this module now checks. `.env.example` and `ci.yml` are here for
their own reason: each once carried a seventh and an eighth declaration that
nothing rewrote and nothing checked.
"""

from __future__ import annotations

import re
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

from infrahub_demo_otn.baseversion import (
    IMAGE_REPOSITORY,
    OVERRIDE_VARIABLE,
    PACKAGED_VERSION,
    base_version,
    image_reference,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"
TASKS = REPO_ROOT / "tasks.py"
INTEGRATION_CONFTEST = REPO_ROOT / "tests" / "integration" / "conftest.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

TESTCONTAINERS = "infrahub-testcontainers"


def test_the_resolver_returns_the_installed_version() -> None:
    """With no override, the version is the one that installed."""
    assert base_version({}, packaged="1.11.2") == "1.11.2"


def test_an_override_wins_over_the_installed_version() -> None:
    """How CI and a developer build against a release other than the locked one."""
    assert base_version({OVERRIDE_VARIABLE: "1.12.0"}, packaged="1.11.2") == "1.12.0"


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_override_is_not_a_version(value: str) -> None:
    """`INFRAHUB_BASE_VERSION=` in a `.env` must not resolve to the empty tag.

    `.env` is layered over the environment, so a line someone uncommented and
    left blank would otherwise produce `opsmill/infrahub-demo-otn:`.

    Args:
        value: A blank override spelling.
    """
    assert base_version({OVERRIDE_VARIABLE: value}, packaged="1.11.2") == "1.11.2"


def test_no_version_at_all_is_an_error_naming_the_fix() -> None:
    """The one unresolvable case fails loudly rather than guessing a release."""
    with pytest.raises(RuntimeError, match="uv sync"):
        base_version({}, packaged="")


def test_the_image_reference_is_the_repository_at_the_resolved_version() -> None:
    """What `invoke build` tags and what the integration stack inspects."""
    assert image_reference({}, packaged="1.11.2") == f"{IMAGE_REPOSITORY}:1.11.2"


def test_the_installed_version_resolves_in_this_environment() -> None:
    """The derivation works here, not just in principle.

    Guards the import too: `infrahub_testcontainers.__version__` disappearing
    would otherwise surface as a stack that pulls a strange tag.
    """
    assert PACKAGED_VERSION, "infrahub-testcontainers exports no __version__, so no version can be derived"
    assert re.fullmatch(r"\d+\.\d+\.\d+[0-9a-z.]*", PACKAGED_VERSION), (
        f"the installed testcontainers version {PACKAGED_VERSION!r} does not look like an Infrahub release"
    )


def test_the_lock_resolves_the_version_the_derivation_returns() -> None:
    """`uv.lock` is what installs, so it is what the resolver must report.

    The check that closes the loop: everything else here trusts the installed
    package, and this proves the installed package is the one the repository
    committed rather than a stale local sync.
    """
    for entry in tomllib.loads(UV_LOCK.read_text())["package"]:
        if entry["name"] == TESTCONTAINERS:
            locked = str(entry["version"])
            break
    else:
        raise AssertionError(f"uv.lock resolves no {TESTCONTAINERS}, so the stack cannot start")
    assert locked == PACKAGED_VERSION, (
        f"uv.lock resolves {TESTCONTAINERS} {locked} but {PACKAGED_VERSION} is installed. "
        f"Run `uv sync` to install what the lock selects."
    )


def test_testcontainers_is_declared_so_there_is_something_to_derive_from() -> None:
    """The single declaration. Read operator-agnostically: the floor is house policy."""
    dependencies = tomllib.loads(PYPROJECT.read_text())["dependency-groups"]["dev"]
    declarations = [d for d in dependencies if d.startswith(TESTCONTAINERS)]
    assert len(declarations) == 1, f"expected exactly one {TESTCONTAINERS} declaration, found {declarations}"
    assert re.fullmatch(rf"{TESTCONTAINERS}(\[[^]]*\])?[=<>~!]+[^,]+", declarations[0]), (
        f"{declarations[0]!r} is not a specifier this guard knows how to read"
    )


def test_the_dockerfile_declares_the_arg_without_a_default() -> None:
    """A default would be a second declaration, and a silent one.

    `FROM` interpolates the arg, so a default does not fail when it goes stale:
    it builds the previous release and says nothing. Without one, a build that
    forgets to pass the version cannot resolve a tag and stops.
    """
    content = DOCKERFILE.read_text()
    assert re.search(rf"^ARG {OVERRIDE_VARIABLE}$", content, re.MULTILINE), (
        f"the Dockerfile must declare `ARG {OVERRIDE_VARIABLE}` with no default; "
        f"a default is a version literal that nothing updates"
    )
    assert f"FROM registry.opsmill.io/opsmill/infrahub:${{{OVERRIDE_VARIABLE}}}" in content, (
        f"the FROM line no longer interpolates {OVERRIDE_VARIABLE}, so the build arg does nothing"
    )


def test_the_compose_override_requires_the_version_rather_than_defaulting() -> None:
    """`:?` over `:-`, so a missing version is an error and not last release.

    `tasks.py` exports the resolved version, so the substitution always has a
    value on the supported path. The spelling matters for every other path: a
    bare `docker compose` with `:-` would silently start whatever number was
    committed here.
    """
    content = COMPOSE_OVERRIDE.read_text()
    fallbacks = re.findall(rf"\$\{{{OVERRIDE_VARIABLE}:-([^}}]*)\}}", content)
    assert not fallbacks, (
        f"docker-compose.override.yml gives {OVERRIDE_VARIABLE} the fallback {fallbacks[0]!r}. "
        f"Use `:?` so compose fails when the version is unset instead of starting a stale release."
    )
    required = re.findall(rf"\$\{{{OVERRIDE_VARIABLE}:\?[^}}]*\}}", content)
    assert len(required) == 2, (
        f"expected the image tag and the build arg to read ${{{OVERRIDE_VARIABLE}:?...}}, found {len(required)}"
    )


# Every file that once held a declaration, plus the two that held one nobody
# knew about. The rule for all of them is the same now: state no version.
LEAK_CANDIDATES = [
    (
        DOCKERFILE,
        "the ARG default is the declaration a bump used to have to move, and a stale one builds the "
        "previous release without failing",
    ),
    (
        COMPOSE_OVERRIDE,
        "a `:-` fallback here is what `docker compose` resolves to when nothing exports the version, "
        "so the stack would run a release the lock does not select",
    ),
    (
        TASKS,
        "`BASE_VERSION` is derived from the installed package, so a literal beside it would tag the "
        "image and pick the upstream compose file by a number nothing moves",
    ),
    (
        INTEGRATION_CONFTEST,
        "the integration stack would run an image other than the one `invoke build` produced",
    ),
    (
        ENV_EXAMPLE,
        "`cp .env.example .env` hands a fresh clone a version no bump moves, and `tasks.py::_env` "
        "layers `.env` over the environment, so it reaches compose",
    ),
    (
        CI_WORKFLOW,
        "the docker-build job would keep building a pinned release rather than the one under test",
    ),
]

LEAK_IDS = [path.name for path, _ in LEAK_CANDIDATES]


@pytest.mark.parametrize(("path", "why"), LEAK_CANDIDATES, ids=LEAK_IDS)
def test_no_file_assigns_the_version_a_value(path: Path, why: str) -> None:
    """No file may assign `INFRAHUB_BASE_VERSION` a version, current or stale.

    Keyed on the variable name rather than on a value, which is what makes this
    catch the case that matters. A declaration left behind holds the version
    being moved *off*, so a check comparing against the resolved version would
    pass it: the number no longer matching is precisely the bug.

    `UV_VERSION` and the pinned actions in `ci.yml` are versions of other things,
    so naming the variable also avoids them without a value allowlist.

    Args:
        path: File that must declare no version.
        why: What goes wrong when it does, for the failure message.
    """
    declarations = re.findall(
        rf"{OVERRIDE_VARIABLE}[=:\s]+[\"']?(\d+\.\d+\.\d+[0-9a-z.]*)",
        path.read_text(),
    )
    assert not declarations, (
        f"{path.relative_to(REPO_ROOT)} sets {OVERRIDE_VARIABLE} to {declarations[0]}, which it must not: "
        f"{why}. Take the value out; `infrahub_demo_otn.baseversion` resolves it."
    )


@pytest.mark.parametrize(("path", "why"), LEAK_CANDIDATES, ids=LEAK_IDS)
def test_no_file_names_the_version_now_installed(path: Path, why: str) -> None:
    """No file may contain the current version anywhere, prose included.

    The second layer, and it catches the spellings the first cannot: an image
    tag, a compose URL, or a number sitting in a comment beside the variable.

    Args:
        path: File that must not name the installed version.
        why: What goes wrong when it does, for the failure message.
    """
    found = re.findall(r"\b\d+\.\d+\.\d+[0-9a-z.]*\b", path.read_text())
    leaked = [version for version in found if version == base_version()]
    assert not leaked, (
        f"{path.relative_to(REPO_ROOT)} names the Infrahub version {base_version()}, which it must not: {why}."
    )


def test_dependabot_may_move_the_version() -> None:
    """The point of deriving it: a lock refresh is a complete bump.

    Held explicitly because this repository once had to ignore the package. Six
    declarations meant Dependabot could only ever move the lock, which the tests
    then refused, so its pull requests could not be made green. One declaration
    means the lock *is* the bump, and an `ignore` rule here would now only stop
    the version from being upgraded at all.
    """
    ecosystems = yaml.safe_load(DEPENDABOT.read_text())["updates"]
    uv = [entry for entry in ecosystems if entry["package-ecosystem"] == "uv"]
    assert len(uv) == 1, f"expected exactly one uv ecosystem in dependabot.yml, found {len(uv)}"
    ignored = [str(rule["dependency-name"]) for rule in uv[0].get("ignore", [])]
    blocked = [pattern for pattern in ignored if fnmatch(TESTCONTAINERS, pattern)]
    assert not blocked, (
        f"dependabot.yml ignores {TESTCONTAINERS} via {blocked[0]!r}, so the Infrahub version can no longer "
        f"be upgraded by a lock refresh. Nothing restates the version now, so the rule is not needed."
    )
