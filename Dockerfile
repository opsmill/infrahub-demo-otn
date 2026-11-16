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

WORKDIR /opt/local

COPY pyproject.toml README.md ./
COPY src/ src/

# Install only this repository's package, into the image's existing virtualenv.
#
# --no-deps is the point. The base image ships the infrahub-sdk, opentelemetry,
# pydantic and prefect versions its server and workers were built and tested
# with. `uv sync` from uv.lock replaced about 50 of them with the versions this
# repository resolved for the host, and the image's task workers then failed
# to start: first on an older infrahub-sdk, then on an opentelemetry-api that
# no longer matched opentelemetry-sdk.
#
# What the workers import from here is infrahub_demo_otn, which needs only the
# standard library, and infrahub_sdk, which the image already provides.
# `uv pip` does not read UV_PROJECT_ENVIRONMENT, so the target is explicit.
RUN uv pip install --python /.venv/bin/python --no-deps .

WORKDIR /source
