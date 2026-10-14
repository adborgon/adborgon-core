"""Custom exception hierarchy for Bionic Core.

Every error raised inside this package derives from
:class:`BionicCoreError`, so an orchestrator can catch a single base class
while still being able to discriminate fine-grained failures::

    from Core.tools.exceptions import BionicCoreError, SandboxError

    try:
        manager.execute("pytest -v", code_path)
    except SandboxError as exc:
        # Inspect ``exc.details`` for structured context.
        ...
"""
from __future__ import annotations

from typing import Any, Mapping


class BionicCoreError(Exception):
    """Base class for every error raised by Bionic Core.

    Attributes:
        message: Human-readable description of the failure.
        details: Optional structured context attached to the error (e.g.,
            exit codes, paths). Consumed by loggers and by the agent to
            decide on a recovery strategy.
    """

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of the failure.
            details: Optional structured context attached to the error.
        """
        super().__init__(message)
        self.message: str = message
        self.details: Mapping[str, Any] = dict(details or {})


# ---------------------------------------------------------------------------
# Sandbox (Docker) errors
# ---------------------------------------------------------------------------
class SandboxError(BionicCoreError):
    """Base class for failures of the Docker test sandbox."""


class DockerUnavailableError(SandboxError):
    """The Docker daemon could not be reached or is not running."""


class SandboxImageError(SandboxError):
    """The sandbox image could not be pulled or resolved."""


class SandboxExecutionError(SandboxError):
    """A sandboxed command could not be executed to completion.

    Attributes:
        exit_code: Process exit code (``-1`` when unknown).
        stdout: Captured standard output of the failed run.
        stderr: Captured standard error of the failed run.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = -1,
        stdout: str = "",
        stderr: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize the error with captured execution context.

        Args:
            message: Human-readable description of the failure.
            exit_code: Process exit code (``-1`` when unknown).
            stdout: Captured standard output of the failed run.
            stderr: Captured standard error of the failed run.
            details: Optional structured context attached to the error.
        """
        merged: dict[str, Any] = dict(details or {})
        merged.setdefault("exit_code", exit_code)
        super().__init__(message, details=merged)
        self.exit_code: int = exit_code
        self.stdout: str = stdout
        self.stderr: str = stderr


# ---------------------------------------------------------------------------
# Local Git errors
# ---------------------------------------------------------------------------
class GitLocalError(BionicCoreError):
    """Base class for local Git operation failures."""


class RepositoryNotFoundError(GitLocalError):
    """No Git repository exists at the requested path."""


class DirtyWorktreeError(GitLocalError):
    """The working tree has pending changes and a clean state was required."""


class GitOperationError(GitLocalError):
    """An underlying Git operation failed."""


# ---------------------------------------------------------------------------
# Bootstrap (CLI orchestration) errors
# ---------------------------------------------------------------------------
class BootstrapError(BionicCoreError):
    """Base class for project bootstrap failures."""


class InvalidProjectNameError(BootstrapError):
    """The provided project name is not a valid folder name."""


class TemplateSourceError(BootstrapError):
    """The local template source could not be located or is invalid."""


class TargetDirectoryExistsError(BootstrapError):
    """A directory for the target project already exists."""


class RollbackError(BootstrapError):
    """The automatic rollback of a failed bootstrap could not complete."""


# ---------------------------------------------------------------------------
# Project Docker lifecycle errors
# ---------------------------------------------------------------------------
class ProjectDockerError(BionicCoreError):
    """Base class for per-project Docker lifecycle failures."""


class DockerBuildError(ProjectDockerError):
    """The project image could not be built."""


class ContainerRunError(ProjectDockerError):
    """A project container could not be started or used."""


class RegistryAuthError(ProjectDockerError):
    """Container registry credentials are missing or were rejected."""


class RegistryError(ProjectDockerError):
    """A container registry operation (pull/push) failed."""
