"""Which Infrahub release this repository builds and runs, resolved in one place.

No file here states that version. It is the version of the installed
`infrahub-testcontainers`, the one package that has to agree with the server
image: it ships the compose file the stack is built from, so a mismatch pairs
one release's topology with another's server. `pyproject.toml` names the floor,
`uv.lock` resolves it, and everything that needs a tag asks this module.

That is what lets Dependabot move the version. A lock refresh moves the only
declaration there is, and the Dockerfile, the compose override, `tasks.py` and
the integration stack all follow without being rewritten. It replaces six
literals across five files that a bump had to move together, and the workflow
and test module that existed to keep them agreeing.

The resolvers take the environment and the installed version as arguments so
they can be tested without Docker and without patching `os.environ`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

try:
    from infrahub_testcontainers import __version__ as _installed
except ImportError:  # pragma: no cover
    # The image installs this package with `uv sync --no-dev`, which leaves
    # testcontainers out. Nothing in the image resolves a version, so an absent
    # package is only an error for a caller that actually asks.
    _installed = ""

PACKAGED_VERSION = _installed
"""Version of the installed `infrahub-testcontainers`, or empty when absent."""

OVERRIDE_VARIABLE = "INFRAHUB_BASE_VERSION"
"""Environment variable that builds and runs a release other than the installed one."""

IMAGE_REPOSITORY = "opsmill/infrahub-demo-otn"
"""The image `invoke build` tags and the integration stack runs."""


def base_version(env: Mapping[str, str] | None = None, *, packaged: str | None = None) -> str:
    """Return the Infrahub release to build and run against.

    Args:
        env: Environment to read the override from. Defaults to `os.environ`.
        packaged: Installed testcontainers version. Defaults to the real one.

    Returns:
        The override when one is set, otherwise the installed version.

    Raises:
        RuntimeError: When no override is set and the package is not installed,
            which is the only case where no version can be resolved at all.
    """
    environment = os.environ if env is None else env
    override = environment.get(OVERRIDE_VARIABLE, "").strip()
    if override:
        return override

    installed = PACKAGED_VERSION if packaged is None else packaged
    if not installed:
        message = (
            "infrahub-testcontainers is not installed, so the Infrahub version cannot be resolved. "
            f"Run `uv sync`, or set {OVERRIDE_VARIABLE} to build against a specific release."
        )
        raise RuntimeError(message)
    return installed


def image_reference(env: Mapping[str, str] | None = None, *, packaged: str | None = None) -> str:
    """Return the `repository:tag` of the image this repository builds.

    Args:
        env: Environment to read the override from. Defaults to `os.environ`.
        packaged: Installed testcontainers version. Defaults to the real one.

    Returns:
        The reference, in the form `docker image inspect` expects.
    """
    return f"{IMAGE_REPOSITORY}:{base_version(env, packaged=packaged)}"
