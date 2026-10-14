"""Bionic Core.

Local abstraction layer used by the Bionic orchestrator to manage software
projects autonomously:

* **Sandbox** — ephemeral, network-isolated Docker test execution
  (:class:`Core.tools.DockerSandboxManager`).
* **Local VCS** — Git operations through GitPython, never through raw shell
  commands (:class:`Core.tools.GitLocalManager`).
* **Bootstrap** — CLI that replicates the shared project template
  (``adborgon/GithubTemplate``, materialized locally by the agent through
  its GitHub MCP tools) and initializes the local repository with the
  initial commit (:mod:`Core.tools.bionic_bootstrap`).

The public API is resolved lazily (PEP 562) so importing the package does
not force the optional ``docker`` dependency::

    from Core import GitLocalManager, BionicCoreError
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__: str = "1.0.0"

__all__: list[str] = [
    "BionicCoreError",
    "DockerSandboxManager",
    "GitLocalManager",
    "ProjectDockerError",
    "ProjectDockerManager",
    "SandboxExecutionResult",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from Core.tools.exceptions import BionicCoreError, ProjectDockerError
    from Core.tools.git_local_manager import GitLocalManager
    from Core.tools.project_docker_manager import ProjectDockerManager
    from Core.tools.sandbox_manager import DockerSandboxManager, SandboxExecutionResult

_LAZY_EXPORTS: dict[str, str] = {
    "BionicCoreError": "Core.tools.exceptions",
    "GitLocalManager": "Core.tools.git_local_manager",
    "ProjectDockerError": "Core.tools.exceptions",
    "ProjectDockerManager": "Core.tools.project_docker_manager",
    "DockerSandboxManager": "Core.tools.sandbox_manager",
    "SandboxExecutionResult": "Core.tools.sandbox_manager",
}


def __getattr__(name: str) -> Any:
    """Lazily resolve a public symbol on first access (PEP 562).

    Args:
        name: Attribute name requested on the package.

    Returns:
        The requested public symbol.

    Raises:
        AttributeError: If ``name`` is not part of the public API.
    """
    if name in _LAZY_EXPORTS:
        module = importlib.import_module(_LAZY_EXPORTS[name])
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Expose the public API to ``dir()`` and editor autocompletion."""
    return sorted(set(globals()) | set(__all__))
