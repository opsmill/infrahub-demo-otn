"""Adjustments to the compose project ``infrahub_testcontainers`` generates.

Everything else about the stack is set through environment variables, which the
``.env`` writer in ``infrahub_testcontainers.container`` reads per key. The
healthchecks are the exception: they are literals in the packaged
``docker-compose.test.yml`` and no variable reaches them. Editing the copy that
``InfrahubDockerCompose.init`` writes into the project directory is the only way
to change them, and it has to happen after ``init`` and before ``start``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

HEALTHCHECK_RETRIES = 300
"""Consecutive failed probes a service may take before Compose calls it
unhealthy.

The packaged file allows 100 at ``interval: 1s``, so roughly 100 seconds, with
no ``start_period``. That is sized for a workstation. On a four-core CI runner
``task-manager`` has needed 61 seconds just to boot its gunicorn worker, before
Prefect answers ``/api/health`` at all, and when it misses the window
``infrahub-server`` never starts, because it waits on ``task-manager`` being
healthy. `docker compose up --wait` then reports a stack that was still coming
up as a broken one.

A ceiling, not a target: Compose stops waiting at the first probe that passes,
so a host that is keeping up pays nothing for the headroom. The cost of setting
it too high is only how long a genuinely dead container takes to be called dead,
and the job timeout still bounds that.
"""


def relax_healthcheck_budgets(compose_file: Path, retries: int = HEALTHCHECK_RETRIES) -> None:
    """Give every healthchecked service in a compose file room to start slowly.

    Args:
        compose_file: The ``docker-compose.yml`` written into the project
            directory. Rewritten in place.
        retries: Consecutive failed probes to allow.

    Raises:
        ValueError: If the file declares no services, which means the layout
            changed and this rewrite silently stopped applying.
    """
    document: dict[str, Any] = yaml.safe_load(compose_file.read_text())
    services: dict[str, Any] = document.get("services") or {}
    if not services:
        msg = f"{compose_file} declares no services; the compose layout has changed"
        raise ValueError(msg)

    # Only `retries` is touched. `interval` and `timeout` decide how hard the
    # probe itself hits a host that is already short of cores, and raising the
    # count is what buys time without probing harder.
    for service in services.values():
        healthcheck = service.get("healthcheck")
        if healthcheck:
            healthcheck["retries"] = retries

    compose_file.write_text(yaml.safe_dump(document, sort_keys=False))
