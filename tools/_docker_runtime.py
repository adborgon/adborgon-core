"""Shared Docker daemon connection helper for the container tools.

Both :mod:`Core.tools.sandbox_manager` and
:mod:`Core.tools.project_docker_manager` resolve the Docker daemon through
:func:`connect_docker`, so connection logic, error translation and logging
stay identical across the package.
"""
from __future__ import annotations

import docker
import requests
from docker.errors import DockerException

try:  # Package context (``Core.tools`` or a standalone ``tools`` package).
    from .exceptions import DockerUnavailableError
    from .logging_utils import get_logger
except ImportError:  # Direct execution of a script from the tools directory.
    from exceptions import DockerUnavailableError
    from logging_utils import get_logger

logger = get_logger(__name__)


def connect_docker(client: "docker.DockerClient | None" = None) -> docker.DockerClient:
    """Return a live Docker client, creating and pinging one if necessary.

    Args:
        client: An existing client to reuse (e.g., injected for testing).

    Returns:
        A connected :class:`docker.DockerClient`.

    Raises:
        DockerUnavailableError: When the daemon cannot be reached.
    """
    if client is not None:
        return client
    try:
        candidate = docker.from_env()
        candidate.ping()
    except (DockerException, requests.exceptions.RequestException) as exc:
        raise DockerUnavailableError(
            "Could not reach the Docker daemon. Is Docker installed and running?",
            details={"error": str(exc)},
        ) from exc
    version_info = candidate.version
    if callable(version_info):  # docker SDK >= 7 exposes version() as a method
        version_info = version_info()
    logger.info(
        "Connected to Docker daemon (API version %s).",
        version_info.get("ApiVersion", "unknown"),
    )
    return candidate
