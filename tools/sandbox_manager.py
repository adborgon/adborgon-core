"""Ephemeral Docker sandbox for isolated test execution.

The sandbox runs untrusted or automated commands (e.g., ``pytest -v``)
inside a short-lived container:

* Image ``bionic-sandbox:latest`` by default: it extends
  ``python:3.11-slim`` with pytest and is built automatically on first use
  from the bundled context ``Core/docker/bionic-sandbox`` (configurable).
* The local code directory is bind-mounted at ``/workspace`` inside the
  container (read-only by default).
* The container is **network-isolated** (``network_disabled=True``),
  memory-capped (default 512 MB), drops all Linux capabilities and is
  destroyed automatically after every run — even when the command fails
  or the SDK raises an exception.

No shell is invoked on the host: container management goes exclusively
through the ``docker`` Python SDK (no ``subprocess``/``os.system``).

Typical usage::

    from Core.tools.sandbox_manager import DockerSandboxManager

    with DockerSandboxManager() as sandbox:
        result = sandbox.run_pytest("C:/projects/demo")
        if result.success:
            print(result.stdout)
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, Union

import docker
import requests
from docker.errors import DockerException, ImageNotFound
from urllib3.exceptions import ReadTimeoutError as Urllib3ReadTimeoutError

try:  # Package context (``Core.tools`` or a standalone ``tools`` package).
    from ._docker_runtime import connect_docker
    from .exceptions import DockerUnavailableError, SandboxError, SandboxImageError
    from .logging_utils import get_logger
except ImportError:  # Direct execution of a script from the tools directory.
    from _docker_runtime import connect_docker
    from exceptions import DockerUnavailableError, SandboxError, SandboxImageError
    from logging_utils import get_logger

logger = get_logger(__name__)

#: Default sandbox image (extends python:3.11-slim with pytest). It is
#: built automatically on first use from ``Core/docker/bionic-sandbox``.
DEFAULT_IMAGE: str = "bionic-sandbox:latest"
DEFAULT_MEMORY_LIMIT: str = "512m"
DEFAULT_CONTAINER_WORKDIR: str = "/workspace"
DEFAULT_COMMAND_TIMEOUT: int = 600
SANDBOX_LABEL: str = "bionic.sandbox"

try:  # ``workdir`` was renamed to ``working_dir`` in docker SDK 7.x.
    from docker.models.containers import RUN_CREATE_KWARGS as _SDK_CREATE_KWARGS

    _WORKING_DIR_PARAM: str = "working_dir" if "working_dir" in _SDK_CREATE_KWARGS else "workdir"
except ImportError:  # pragma: no cover - extremely old SDKs
    _WORKING_DIR_PARAM = "workdir"

#: Build context of the default sandbox image, bundled with this package.
_SANDBOX_DOCKERFILE_CONTEXT: Path = (
    Path(__file__).resolve().parents[1] / "docker" / "bionic-sandbox"
)


def _decode(data: bytes) -> str:
    """Decode container output, replacing undecodable bytes.

    Args:
        data: Raw output bytes captured from the container.

    Returns:
        UTF-8 text with replacement characters for invalid sequences, so
        binary output never crashes the pipeline.
    """
    return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class SandboxExecutionResult:
    """Immutable outcome of a single sandboxed command execution.

    Attributes:
        command: The command that was executed (as provided by the caller).
        exit_code: Process exit code (``0`` on success).
        stdout: Decoded standard output of the process.
        stderr: Decoded standard error of the process.
        container_id: Docker identifier of the one-shot container used.
        duration_seconds: Wall-clock duration of the run.
    """

    command: str
    exit_code: int
    stdout: str
    stderr: str
    container_id: str
    duration_seconds: float

    @property
    def success(self) -> bool:
        """Whether the command exited with code ``0``."""
        return self.exit_code == 0


class DockerSandboxManager:
    """Runs commands inside fresh, isolated, short-lived containers.

    Each call to :meth:`execute` starts a brand-new container, mounts the
    requested code directory, captures stdout/stderr separately and
    guarantees removal of the container in a ``finally`` block. The manager
    is a context manager so the underlying Docker client can be closed
    deterministically.

    A non-zero process exit code is **not** an exception: callers inspect
    :attr:`SandboxExecutionResult.success`. Exceptions are reserved for
    infrastructure failures (daemon unreachable, image missing, timeout).
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        container_workdir: str = DEFAULT_CONTAINER_WORKDIR,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
        mount_readonly: bool = True,
        client: "docker.DockerClient | None" = None,
    ) -> None:
        """Configure the sandbox.

        Args:
            image: Docker image used for the sandbox. The default
                ``bionic-sandbox:latest`` (pytest included) is built
                automatically on first use; pass a custom image only when
                it already contains the needed dependencies (the sandbox
                has no network access).
            memory_limit: Container memory limit, e.g. ``"512m"``.
            container_workdir: Path inside the container where the host
                code directory is mounted.
            command_timeout: Maximum seconds the command may run before the
                container is killed and a :class:`SandboxError` is raised.
            mount_readonly: Mount the code volume read-only (recommended
                for pure test runs). Disable it only for commands that must
                write build artifacts.
            client: Optional pre-connected :class:`docker.DockerClient`
                (mainly for testing).
        """
        self._image: str = image
        self._memory_limit: str = memory_limit
        self._container_workdir: str = container_workdir
        self._command_timeout: int = command_timeout
        self._mount_readonly: bool = mount_readonly
        self._client: "docker.DockerClient | None" = client

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "DockerSandboxManager":
        """Enter the runtime context, keeping the Docker client open."""
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Leave the runtime context and close the Docker client."""
        self.close()

    def close(self) -> None:
        """Close the underlying Docker client, if one was created here.

        Safe to call multiple times.
        """
        if self._client is not None:
            try:
                self._client.close()
            except DockerException:
                logger.debug("Docker client close failed.", exc_info=True)
            self._client = None

    def _get_client(self) -> docker.DockerClient:
        """Return a connected Docker client, creating one if necessary.

        Returns:
            A live :class:`docker.DockerClient` instance.

        Raises:
            DockerUnavailableError: When the daemon cannot be reached.
        """
        if self._client is None:
            self._client = connect_docker(None)
        return self._client

    def ensure_image(self) -> None:
        """Make sure the sandbox image is available locally, pulling it if needed.

        Raises:
            DockerUnavailableError: When the daemon is unreachable.
            SandboxImageError: When the image cannot be resolved or pulled.
        """
        client = self._get_client()
        try:
            client.images.get(self._image)
            logger.debug("Sandbox image '%s' already present locally.", self._image)
            return
        except ImageNotFound:
            pass
        if self._image == DEFAULT_IMAGE and (_SANDBOX_DOCKERFILE_CONTEXT / "Dockerfile").is_file():
            self._build_default_image(client)
            return
        logger.info("Pulling sandbox image '%s' (first run)...", self._image)
        try:
            client.images.pull(self._image)
        except ImageNotFound as exc:
            raise SandboxImageError(
                f"Sandbox image '{self._image}' was not found in any registry.",
                details={"image": self._image},
            ) from exc
        except DockerException as exc:
            raise DockerUnavailableError(
                f"Failed to pull sandbox image '{self._image}': {exc}",
                details={"image": self._image},
            ) from exc

    def _build_default_image(self, client: docker.DockerClient) -> None:
        """Build the default sandbox image from the bundled Dockerfile.

        Args:
            client: Connected Docker client.

        Raises:
            SandboxImageError: When the build fails.
        """
        logger.info(
            "Building default sandbox image '%s' from '%s' (first run)...",
            self._image,
            _SANDBOX_DOCKERFILE_CONTEXT,
        )
        try:
            image, logs = client.images.build(
                path=str(_SANDBOX_DOCKERFILE_CONTEXT),
                tag=self._image,
                rm=True,
                pull=True,
            )
        except (DockerException, ImageNotFound) as exc:
            raise SandboxImageError(
                f"Failed to build sandbox image '{self._image}': {exc}",
                details={"context": str(_SANDBOX_DOCKERFILE_CONTEXT)},
            ) from exc
        for chunk in logs:
            if chunk and chunk.get("stream"):
                logger.debug("[build] %s", chunk["stream"].rstrip())
        logger.info(
            "Sandbox image '%s' built successfully (%s).", self._image, image.id[:19]
        )

    # -- execution -------------------------------------------------------------
    def execute(
        self,
        command: Union[str, Sequence[str]],
        code_path: Union[str, Path],
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> SandboxExecutionResult:
        """Execute a command in a fresh, isolated container.

        The command runs inside a one-shot container with the host
        ``code_path`` mounted at the container workdir, no network access,
        a hard memory limit and all capabilities dropped. The container is
        removed automatically once the command finishes, in every code path
        (success, failure, timeout).

        Args:
            command: Command to execute. A ``str`` is interpreted by the
                container shell (``/bin/sh -c``); a sequence is executed
                directly as an argv list.
            code_path: Local directory mounted into the container.
            extra_env: Optional environment variables exposed to the process.

        Returns:
            A :class:`SandboxExecutionResult` with captured stdout/stderr,
            exit code and timing.

        Raises:
            SandboxError: On invalid inputs, a timeout or any run failure.
            DockerUnavailableError: When the Docker daemon is unreachable.
            SandboxImageError: When the sandbox image cannot be prepared.
        """
        host_dir = self._validate_code_path(code_path)
        self._get_client()
        self.ensure_image()

        argv = ["/bin/sh", "-c", command] if isinstance(command, str) else [str(part) for part in command]
        container_name = f"bionic-sandbox-{uuid.uuid4().hex[:12]}"
        volumes = {
            host_dir.as_posix(): {
                "bind": self._container_workdir,
                "mode": "ro" if self._mount_readonly else "rw",
            }
        }

        logger.info(
            "Starting sandbox container '%s' (image=%s, code=%s, memory=%s, network=off).",
            container_name,
            self._image,
            host_dir,
            self._memory_limit,
        )
        started = time.perf_counter()
        container: "docker.models.containers.Container | None" = None
        try:
            container = self._start_container(
                container_name=container_name,
                argv=argv,
                volumes=volumes,
                extra_env=extra_env,
            )
            exit_code = self._wait_for_exit(container)
            stdout_text, stderr_text = self._capture_logs(container)
        except Exception:
            logger.exception(
                "Sandbox run aborted; ensuring container '%s' is destroyed.", container_name
            )
            raise
        finally:
            duration = time.perf_counter() - started
            self._destroy_container(container)

        result = SandboxExecutionResult(
            command=command if isinstance(command, str) else " ".join(argv),
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            container_id=container.id,  # type: ignore[union-attr]
            duration_seconds=round(duration, 3),
        )
        logger.info(
            "Sandbox finished: exit_code=%d, stdout=%dB, stderr=%dB, duration=%.2fs.",
            result.exit_code,
            len(result.stdout),
            len(result.stderr),
            result.duration_seconds,
        )
        return result

    def run_pytest(
        self,
        code_path: Union[str, Path],
        *extra_args: str,
        extra_env: Mapping[str, str] | None = None,
    ) -> SandboxExecutionResult:
        """Run ``pytest`` against the mounted code directory.

        Args:
            code_path: Local project directory containing the tests.
            extra_args: Extra pytest arguments (e.g., ``"-v"``,
                ``"tests/test_smoke.py"``). Defaults to ``-v`` when omitted.
            extra_env: Optional environment variables for the process.

        Returns:
            The :class:`SandboxExecutionResult` of the pytest run.

        Note:
            The default ``bionic-sandbox`` image ships pytest. When using a
            custom ``image=`` without the test dependencies, remember that
            the sandbox has no network access: bake the dependencies into
            the image instead.
        """
        args = list(extra_args) or ["-v"]
        return self.execute(["python", "-m", "pytest", *args], code_path, extra_env=extra_env)

    # -- internals ---------------------------------------------------------------
    def _start_container(
        self,
        *,
        container_name: str,
        argv: list[str],
        volumes: Mapping[str, Mapping[str, str]],
        extra_env: Mapping[str, str] | None,
    ) -> "docker.models.containers.Container":
        """Start the one-shot sandbox container.

        Args:
            container_name: Unique name assigned to the container.
            argv: Full argv to execute inside the container.
            volumes: Bind-mount configuration (host dir -> container workdir).
            extra_env: Optional environment variables for the process.

        Returns:
            The running :class:`~docker.models.containers.Container`.

        Raises:
            SandboxError: When the container cannot be started.
        """
        client = self._get_client()
        try:
            return client.containers.run(
                self._image,
                argv,
                name=container_name,
                detach=True,
                stdout=True,
                stderr=True,
                **{_WORKING_DIR_PARAM: self._container_workdir},
                volumes=volumes,
                network_disabled=True,
                mem_limit=self._memory_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                environment=dict(extra_env or {}),
                labels={SANDBOX_LABEL: "true", f"{SANDBOX_LABEL}.image": self._image},
            )
        except (DockerException, ImageNotFound) as exc:
            raise SandboxError(
                f"Could not start sandbox container '{container_name}': {exc}",
                details={"image": self._image, "container_name": container_name},
            ) from exc

    def _wait_for_exit(self, container: "docker.models.containers.Container") -> int:
        """Block until the container exits, enforcing the timeout.

        Args:
            container: Running container.

        Returns:
            The process exit code (``-1`` when not reported).

        Raises:
            SandboxError: When the command exceeds the configured timeout or
                the wait itself fails at the transport level.
        """
        try:
            wait_result = container.wait(timeout=self._command_timeout)
        except requests.exceptions.RequestException as exc:
            if self._is_timeout_error(exc):
                raise SandboxError(
                    f"Sandbox command exceeded the timeout of {self._command_timeout}s.",
                    details={"timeout_seconds": self._command_timeout},
                ) from exc
            raise SandboxError(
                f"Failed while waiting for container {container.id[:12]} to exit: {exc}",
                details={"container_id": container.id[:12]},
            ) from exc
        exit_code = int(wait_result.get("StatusCode", -1))
        if exit_code == -1:
            logger.warning("Container %s exited without a reported status code.", container.id[:12])
        return exit_code

    @staticmethod
    def _is_timeout_error(exc: BaseException) -> bool:
        """Determine whether an exception chain contains a read timeout.

        The surfaced type depends on the transport: regular HTTP raises
        ``requests.exceptions.ReadTimeout``, while the Windows npipe
        streaming transport surfaces a ``requests.exceptions.ConnectionError``
        wrapping a raw ``urllib3`` ReadTimeoutError.

        Args:
            exc: The raised exception.

        Returns:
            ``True`` when any link in the cause/context chain is a timeout.
        """
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, (requests.exceptions.ReadTimeout, Urllib3ReadTimeoutError)):
                return True
            if "timed out" in str(current).lower():
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _capture_logs(container: "docker.models.containers.Container") -> tuple[str, str]:
        """Read the container logs, separating stdout and stderr.

        Two single-shot ``logs`` calls are used (one per stream, filtered
        server-side) instead of a streaming ``attach``: on recent Docker
        engines, attaching to an already-exited container can block
        indefinitely.

        Args:
            container: Exited container whose logs are read.

        Returns:
            A ``(stdout, stderr)`` tuple of decoded text.

        Raises:
            SandboxError: When the logs cannot be read.
        """
        try:
            stdout_text = _decode(container.logs(stdout=True, stderr=False))
            stderr_text = _decode(container.logs(stdout=False, stderr=True))
        except DockerException as exc:
            raise SandboxError(
                f"Could not read logs of container {container.id[:12]}: {exc}",
                details={"container_id": container.id[:12]},
            ) from exc
        return stdout_text, stderr_text

    @staticmethod
    def _destroy_container(container: "docker.models.containers.Container | None") -> None:
        """Kill and remove the container, swallowing secondary failures.

        This is the guarantee that the sandbox is ephemeral: no container
        survives a run, even when the run itself raised.

        Args:
            container: Container to destroy (``None`` when the run call
                itself failed before a container existed).
        """
        if container is None:
            return
        try:
            if container.status == "running":
                container.kill()
            container.remove(force=True)
            logger.info("Destroyed sandbox container %s.", container.id[:12])
        except DockerException as exc:
            logger.warning(
                "Could not guarantee removal of container %s: %s",
                getattr(container, "id", "<unknown>"),
                exc,
            )

    @staticmethod
    def _validate_code_path(code_path: Union[str, Path]) -> Path:
        """Resolve and validate the host directory to be mounted.

        Args:
            code_path: Local directory containing the code under test.

        Returns:
            The resolved absolute path.

        Raises:
            SandboxError: When the path does not exist or is not a directory.
        """
        resolved = Path(code_path).expanduser().resolve()
        if not resolved.exists():
            raise SandboxError(
                f"Code directory '{resolved}' does not exist.",
                details={"code_path": str(resolved)},
            )
        if not resolved.is_dir():
            raise SandboxError(
                f"Code path '{resolved}' is not a directory.",
                details={"code_path": str(resolved)},
            )
        return resolved
