# No default on purpose. `FROM` interpolates this, so a stale default would build
# the previous release and say nothing; `invoke build` and ci.yml pass the version
# `infrahub_demo_otn.baseversion` derives from the installed infrahub-testcontainers.
#
# `docker build --check` reports InvalidDefaultArgInFrom here and wants a default.
# Do not add one: that is the silent stale declaration this repository removed,
# and `tests/unit/test_pins.py` fails if it returns. A plain build without the arg
# stops at `invalid reference format`, which is the intended loud failure.
ARG INFRAHUB_BASE_VERSION
FROM registry.opsmill.io/opsmill/infrahub:${INFRAHUB_BASE_VERSION}

# Install into the image's existing virtualenv rather than a new one.
ENV UV_PROJECT_ENVIRONMENT="/.venv"

WORKDIR /opt/local

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# --no-dev keeps the dev group out, which would otherwise drag in a second
# infrahub-sdk and fight the one the base image ships.
# --inexact leaves the existing infrahub environment in place instead of
# pruning everything uv did not put there.
RUN uv sync --no-dev --frozen --inexact

WORKDIR /source
