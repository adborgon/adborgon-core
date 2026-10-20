# Adborgon sandbox image — extended test environment for DockerSandboxManager.
#
# The sandbox runs with network_disabled=True at runtime, so every testing
# tool must be baked in here. Base: python:3.11-slim.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Testing tooling (pytest also runs unittest-based suites).
RUN pip install --no-cache-dir "pytest>=8"

# Unprivileged user: containers also drop all capabilities, so the process
# runs with no privileges at all.
RUN useradd --create-home --shell /usr/sbin/nologin tester
USER tester

WORKDIR /workspace
