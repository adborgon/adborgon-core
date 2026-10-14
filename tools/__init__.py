"""Bionic Core local tools.

Public API:

* :class:`GitLocalManager` — local Git operations (GitPython).
* :class:`DockerSandboxManager` / :class:`SandboxExecutionResult` —
  ephemeral Docker test sandbox.
* :class:`ProjectDockerManager` / :class:`ProjectRunResult` — per-project
  Docker lifecycle (build, run, push, pull & run).
* Exception hierarchy — :mod:`Core.tools.exceptions`.
* :func:`get_logger` — standardized logging for traceability.

The Docker-dependent symbols are resolved lazily (PEP 562) so that the
``docker`` package is only required when a container tool is actually used.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .exceptions import (
    BionicCoreError,
    BootstrapError,
    ContainerRunError,
    DirtyWorktreeError,
    DockerBuildError,
    DockerUnavailableError,
    GitLocalError,
    GitOperationError,
    InvalidProjectNameError,
    ProjectDockerError,
    RegistryAuthError,
    RegistryError,
    RepositoryNotFoundError,
    RollbackError,
    SandboxError,
    SandboxExecutionError,
    SandboxImageError,
    TargetDirectoryExistsError,
    TemplateSourceError,
)
from .git_local_manager import GitLocalManager
from .logging_utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project_docker_manager import ProjectDockerManager, ProjectRunResult
    from .sandbox_manager import DockerSandboxManager, SandboxExecutionResult

__all__: list[str] = [
    "BionicCoreError",
    "BootstrapError",
    "ContainerRunError",
    "DockerBuildError",
    "DockerSandboxManager",
    "DockerUnavailableError",
    "DirtyWorktreeError",
    "GitLocalError",
    "GitLocalManager",
    "GitOperationError",
    "InvalidProjectNameError",
    "ProjectDockerError",
    "ProjectDockerManager",
    "ProjectRunResult",
    "RegistryAuthError",
    "RegistryError",
    "RepositoryNotFoundError",
    "RollbackError",
    "SandboxError",
    "SandboxExecutionError",
    "SandboxExecutionResult",
    "SandboxImageError",
    "TargetDirectoryExistsError",
    "TemplateSourceError",
    "get_logger",
]


def __getattr__(name: str) -> Any:
    """Lazily import Docker-dependent symbols on first access (PEP 562).

    Args:
        name: Attribute name requested on the package.

    Returns:
        The requested symbol.

    Raises:
        AttributeError: If the symbol is not part of the public API.
    """
    if name in ("DockerSandboxManager", "SandboxExecutionResult"):
        from . import sandbox_manager

        return getattr(sandbox_manager, name)
    if name in ("ProjectDockerManager", "ProjectRunResult"):
        from . import project_docker_manager

        return getattr(project_docker_manager, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
