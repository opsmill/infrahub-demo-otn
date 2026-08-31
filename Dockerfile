ARG INFRAHUB_BASE_VERSION=1.11.0
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
