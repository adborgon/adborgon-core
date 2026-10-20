"""Per-project Docker image lifecycle: build, run, publish, install.

Implements the container pipeline for every Adborgon project:

* **Build** the project image from its ``Dockerfile`` (the Adborgon template
  ships one; the agent specializes it per project type: web API, CLI, ...).
* **Run** it locally in a dev container for development and testing.
* **Push** it to a container registry (GHCR by default) so the exact image
  can be installed on any other machine.
* **Install** (pull + run) a published image on a remote machine.

All operations go through the ``docker`` Python SDK — no shell commands.

Typical usage (agent side)::

    from Core.tools.project_docker_manager import ProjectDockerManager

    with ProjectDockerManager(r"C:\\Adborgon\\projects\\tienda-web") as docker:
        docker.build()
        docker.run_local(ports={"8000/tcp": 8000}, env={"APP_NAME": "tienda"})
        # ... develop / test locally ...
        docker.push(tag="1.0.0")  # -> ghcr.io/<owner>/tienda-web:1.0.0

On any other machine::

    with ProjectDockerManager(some_project_dir) as docker:
        docker.pull_and_run("ghcr.io/adborgon/tienda-web:1.0.0",
                            ports={"8000/tcp": 8000})
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import docker
from docker.errors import DockerException, ImageNotFound

try:  # Package context (``Core.tools`` or a standalone ``tools`` package).
    from ._docker_runtime import connect_docker
    from .exceptions import (
        ContainerRunError,
        DockerBuildError,
        ProjectDockerError,
        RegistryAuthError,
        RegistryError,
    )
    from .logging_utils import get_logger
except ImportError:  # Direct execution of a script from the tools directory.
    from _docker_runtime import connect_docker
    from exceptions import (
        ContainerRunError,
        DockerBuildError,
        ProjectDockerError,
        RegistryAuthError,
        RegistryError,
    )
    from logging_utils import get_logger

logger = get_logger(__name__)

DEFAULT_REGISTRY: str = "ghcr.io"
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Convert a project name into a lowercase registry/container slug.

    Args:
        name: Project (folder) name.

    Returns:
        A lowercase string safe for image and container names.
    """
    return _SLUG_PATTERN.sub("-", name.lower()).strip("-") or "project"


@dataclass(frozen=True)
class ProjectRunResult:
    """Handle for a running project container.

    Attributes:
        container_id: Docker identifier of the container.
        name: Assigned container name.
        image: Image reference the container was started from.
        ports: Requested container→host port mapping.
    """

    container_id: str
    name: str
    image: str
    ports: Mapping[str, int | str] = field(default_factory=dict)


class ProjectDockerManager:
    """Builds and runs the Docker image of an Adborgon project.

    Pipeline: ``build()`` → ``run_local()`` (dev/test) → ``push()``
    (registry) → ``pull_and_run()`` (install elsewhere).

    The manager is a context manager: exiting it stops and removes the dev
    container and closes the Docker client, so no container is left behind.

    Raises:
        ProjectDockerError and subclasses: on any lifecycle failure.
    """

    def __init__(
        self,
        project_path: str | Path,
        *,
        image_name: str | None = None,
        default_registry: str = DEFAULT_REGISTRY,
        client: "docker.DockerClient | None" = None,
    ) -> None:
        """Bind the manager to a project directory.

        Args:
            project_path: Project directory containing the ``Dockerfile``.
            image_name: Local image name (default
                ``adborgon-<project-slug>:latest``).
            default_registry: Registry used by :meth:`push` /
                :meth:`pull_and_run` (default ``ghcr.io``).
            client: Optional pre-connected :class:`docker.DockerClient`
                (mainly for testing).
        """
        self._project_path: Path = Path(project_path).expanduser().resolve()
        self._slug: str = _slugify(self._project_path.name)
        self._image_name: str = image_name or f"adborgon-{self._slug}:latest"
        self._default_registry: str = default_registry
        self._client: "docker.DockerClient | None" = client
        self._dev_container: "docker.models.containers.Container | None" = None

    # -- properties ---------------------------------------------------------
    @property
    def project_path(self) -> Path:
        """Absolute path of the managed project."""
        return self._project_path

    @property
    def image_name(self) -> str:
        """Local image name (with default tag)."""
        return self._image_name

    @property
    def registry(self) -> str:
        """Container registry used for push/pull operations."""
        return self._default_registry

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "ProjectDockerManager":
        """Enter the runtime context."""
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Leave the runtime context: stop the dev container, close the client."""
        self.close()

    def close(self) -> None:
        """Stop the dev container (if running) and close the Docker client.

        Safe to call multiple times.
        """
        self.stop()
        if self._client is not None:
            try:
                self._client.close()
            except DockerException:
                logger.debug("Docker client close failed.", exc_info=True)
            self._client = None

    def _get_client(self) -> docker.DockerClient:
        """Return a connected Docker client, creating one if necessary.

        Returns:
            A live :class:`docker.DockerClient`.

        Raises:
            DockerUnavailableError: When the daemon cannot be reached.
        """
        if self._client is None:
            self._client = connect_docker(None)
        return self._client

    # -- build -----------------------------------------------------------------
    def build(self, *, tag: str | None = None, build_args: Mapping[str, str] | None = None) -> str:
        """Build the project image from the project's ``Dockerfile``.

        The project directory is used as the build context, so the
        project's ``.dockerignore`` is honoured automatically.

        Args:
            tag: Optional image tag (default: the manager's default tag,
                usually ``latest``).
            build_args: Optional ``--build-arg`` values.

        Returns:
            The full local image reference (e.g. ``adborgon-tienda:1.0.0``).

        Raises:
            DockerBuildError: When no Dockerfile exists or the build fails.
            DockerUnavailableError: When the daemon cannot be reached.
        """
        dockerfile = self._project_path / "Dockerfile"
        if not dockerfile.is_file():
            raise DockerBuildError(
                f"No Dockerfile found in '{self._project_path}'. Bootstrapped "
                "projects include one from the template.",
                details={"project_path": str(self._project_path)},
            )
        client = self._get_client()
        base_name = self._image_name.split(":", 1)[0]
        image_tag = tag or self._image_name.split(":", 1)[1]
        local_ref = f"{base_name}:{image_tag}"
        logger.info("Building image '%s' from context '%s'...", local_ref, self._project_path)
        try:
            image, logs = client.images.build(
                path=str(self._project_path),
                tag=local_ref,
                buildargs=dict(build_args or {}),
                rm=True,
                pull=True,
            )
        except (DockerException, ImageNotFound) as exc:
            raise DockerBuildError(
                f"Docker build failed for '{self._project_path}': {exc}",
                details={"image": local_ref},
            ) from exc
        for chunk in logs:
            if chunk and chunk.get("stream"):
                logger.debug("[build] %s", chunk["stream"].rstrip())
        logger.info("Image '%s' built successfully (%s).", local_ref, image.id[:19])
        return local_ref

    # -- run ---------------------------------------------------------------------
    def run_local(
        self,
        *,
        ports: Mapping[str, int | str] | None = None,
        env: Mapping[str, str] | None = None,
        command: Sequence[str] | None = None,
        name: str | None = None,
        image: str | None = None,
    ) -> ProjectRunResult:
        """Start the project image as a local dev container.

        Any previously started dev container is stopped and removed first,
        so calling this method twice always yields a single fresh container.

        Args:
            ports: Container→host port mapping, e.g. ``{"8000/tcp": 8000}``.
            env: Optional environment variables for the container.
            command: Optional argv override for the image's default CMD.
            name: Optional container name (default ``adborgon-dev-<slug>``).
            image: Optional image reference override (default: the local
                image built by :meth:`build`).

        Returns:
            A :class:`ProjectRunResult` handle.

        Raises:
            ContainerRunError: When the image is missing or the container
                cannot be started.
        """
        self.stop()
        client = self._get_client()
        image_ref = image or self._image_name
        container_name = name or f"adborgon-dev-{self._slug}"
        try:
            container = client.containers.run(
                image_ref,
                list(command) if command else None,
                detach=True,
                name=container_name,
                ports=dict(ports or {}),
                environment=dict(env or {}),
                labels={"adborgon.project": "true", "adborgon.project.slug": self._slug},
            )
        except ImageNotFound as exc:
            raise ContainerRunError(
                f"Image '{image_ref}' not found locally. Call build() first.",
                details={"image": image_ref},
            ) from exc
        except DockerException as exc:
            raise ContainerRunError(
                f"Could not start dev container '{container_name}': {exc}",
                details={"image": image_ref, "container_name": container_name},
            ) from exc
        self._dev_container = container
        logger.info(
            "Dev container '%s' started from image '%s' (id=%s).",
            container_name,
            image_ref,
            container.id[:12],
        )
        return ProjectRunResult(
            container_id=container.id,
            name=container_name,
            image=image_ref,
            ports=dict(ports or {}),
        )

    def logs(self, tail: int = 100) -> str:
        """Return the tail of the dev container's merged logs.

        Args:
            tail: Number of trailing log lines to return.

        Returns:
            The decoded log text.

        Raises:
            ProjectDockerError: When no dev container is known or the logs
                cannot be read.
        """
        if self._dev_container is None:
            raise ProjectDockerError("No dev container is running. Call run_local() first.")
        try:
            raw = self._dev_container.logs(stdout=True, stderr=True, tail=tail)
        except DockerException as exc:
            raise ProjectDockerError(f"Could not read dev container logs: {exc}") from exc
        return raw.decode("utf-8", errors="replace")

    def stop(self) -> None:
        """Stop and remove the dev container (no-op when not running).

        Secondary failures are logged, never raised, so cleanup paths
        (context exit, rollback) are not masked.
        """
        container, self._dev_container = self._dev_container, None
        if container is None:
            return
        try:
            if container.status == "running":
                container.stop(timeout=10)
            container.remove(force=True)
            logger.info("Dev container %s stopped and removed.", container.id[:12])
        except DockerException as exc:
            logger.warning(
                "Could not guarantee removal of dev container %s: %s",
                container.id[:12],
                exc,
            )

    # -- registry --------------------------------------------------------------------
    def push(self, *, tag: str | None = None) -> str:
        """Tag and push the project image to the container registry (GHCR).

        Authentication is read from the environment:

        * ``ghcr.io`` → ``GHCR_USERNAME`` + ``GHCR_TOKEN`` (a GitHub personal
          access token with the ``write:packages`` permission).
        * any other registry → ``REGISTRY_USERNAME`` + ``REGISTRY_PASSWORD``.

        Args:
            tag: Image tag to publish (default ``latest``).

        Returns:
            The remote image reference (e.g.
            ``ghcr.io/adborgon/tienda-web:1.0.0``).

        Raises:
            RegistryAuthError: When the credentials are not in the environment.
            RegistryError: When the image is missing, or login/push fails.
        """
        client = self._get_client()
        image_tag = tag or "latest"
        base_name = self._image_name.split(":", 1)[0]
        default_tag = self._image_name.split(":", 1)[1]
        try:
            image = client.images.get(self._image_name)
        except ImageNotFound as exc:
            raise RegistryError(
                f"Image '{self._image_name}' not found locally. Call build() first.",
                details={"image": self._image_name},
            ) from exc
        if image_tag != default_tag:
            # Re-tag the built image locally so the requested version exists
            # both locally and in the registry.
            image.tag(f"{base_name}:{image_tag}")

        username, password = self._registry_credentials()
        try:
            client.login(username=username, password=password, registry=self._default_registry)
        except DockerException as exc:
            raise RegistryError(
                f"Registry login failed for '{self._default_registry}': {exc}",
                details={"registry": self._default_registry},
            ) from exc

        remote_ref = f"{self._default_registry}/{username}/{self._slug}:{image_tag}"
        try:
            image.tag(remote_ref)
            # ``client.api.push`` works across docker SDK 6.x and 7.x (the
            # high-level ``Image.push`` model method was removed in 7.x).
            for chunk in client.api.push(remote_ref, stream=True):
                if not chunk:
                    continue
                if isinstance(chunk, (bytes, bytearray)):  # SDK >= 7 yields raw JSON lines
                    try:
                        chunk = json.loads(chunk.decode("utf-8", errors="replace"))
                    except ValueError:
                        logger.debug(
                            "[push] %s", chunk.decode("utf-8", errors="replace").strip()
                        )
                        continue
                if isinstance(chunk, dict):
                    if chunk.get("error"):
                        # Registry rejections arrive as stream chunks; surface
                        # them as a hard failure instead of a silent success.
                        raise RegistryError(
                            f"Push to '{remote_ref}' failed: {chunk['error']}",
                            details={
                                "registry": self._default_registry,
                                "error_detail": chunk.get("errorDetail"),
                            },
                        )
                    if chunk.get("status"):
                        logger.debug("[push] %s", chunk["status"])
        except DockerException as exc:
            raise RegistryError(f"Push to '{remote_ref}' failed: {exc}") from exc
        logger.info("Image pushed to '%s'.", remote_ref)
        return remote_ref

    def pull_and_run(
        self,
        image_ref: str,
        *,
        ports: Mapping[str, int | str] | None = None,
        env: Mapping[str, str] | None = None,
        name: str | None = None,
    ) -> ProjectRunResult:
        """Pull a published image and run it (install on another machine).

        Args:
            image_ref: Full remote reference (e.g.
                ``ghcr.io/adborgon/tienda-web:1.0.0``).
            ports: Container→host port mapping.
            env: Optional environment variables for the container.
            name: Optional container name.

        Returns:
            A :class:`ProjectRunResult` handle.

        Raises:
            RegistryError: When the pull fails.
            ContainerRunError: When the container cannot be started.
        """
        client = self._get_client()
        try:
            client.images.get(image_ref)
            logger.debug("Image '%s' already present locally.", image_ref)
        except ImageNotFound:
            # Private packages (e.g. GHCR) require authentication to pull.
            if image_ref.startswith(self._default_registry + "/"):
                username, password = self._registry_credentials()
                try:
                    client.login(
                        username=username,
                        password=password,
                        registry=self._default_registry,
                    )
                except DockerException as exc:
                    raise RegistryError(
                        f"Registry login failed for '{self._default_registry}': {exc}",
                        details={"registry": self._default_registry},
                    ) from exc
            logger.info("Pulling image '%s' (install flow)...", image_ref)
            try:
                client.images.pull(image_ref)
            except (DockerException, ImageNotFound) as exc:
                raise RegistryError(f"Pull of '{image_ref}' failed: {exc}") from exc
        return self.run_local(ports=ports, env=env, name=name, image=image_ref)

    def _registry_credentials(self) -> tuple[str, str]:
        """Resolve registry credentials from the environment.

        Returns:
            The ``(username, password)`` pair to use for login.

        Raises:
            RegistryAuthError: When the required variables are missing.
        """
        if self._default_registry == "ghcr.io":
            username = os.environ.get("GHCR_USERNAME", "").strip()
            password = os.environ.get("GHCR_TOKEN", "").strip()
            missing = "GHCR_USERNAME and GHCR_TOKEN (GitHub PAT with 'write:packages')"
        else:
            username = os.environ.get("REGISTRY_USERNAME", "").strip()
            password = os.environ.get("REGISTRY_PASSWORD", "").strip()
            missing = "REGISTRY_USERNAME and REGISTRY_PASSWORD"
        if not username or not password:
            raise RegistryAuthError(
                f"Missing registry credentials for '{self._default_registry}'. "
                f"Set {missing} in the environment and retry.",
                details={"registry": self._default_registry},
            )
        return username, password
