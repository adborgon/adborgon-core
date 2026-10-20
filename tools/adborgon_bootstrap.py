"""Adborgon project bootstrap — CLI entry point and local orchestrator.

Orchestration contract (implicit MCP integration)
=================================================
1. **Template acquisition (Adborgon agent + MCP).** When this script is
   invoked, the Adborgon agent connects to the ``adborgon/GithubTemplate``
   repository through its native GitHub MCP tools, downloads the base files
   and materializes them in a local template directory. This script does
   **not** talk to the GitHub API (by design, no Python-level GitHub
   integration): it consumes the already materialized template
   (``--template-source``).
2. **Local replication.** The template is copied into a fresh project
   directory (``--workdir``/``--name``), the README and the ``.Adborgon/``
   metadata are specialized for the new project.
3. **Local Git initialization.** :class:`GitLocalManager` initializes the
   repository, stages every downloaded file and creates the initial
   commit.
4. **Rollback.** If any step fails, the partially created project
   directory is removed and the script aborts with a non-zero exit code.

Usage::

    python C:\\Adborgon\\Core\\tools\\adborgon_bootstrap.py \\
        --name my-project --description "What the project does"
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import re
import shutil
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

try:  # Package context (``Core.tools`` or a standalone ``tools`` package).
    from .exceptions import (
        AdborgonCoreError,
        BootstrapError,
        InvalidProjectNameError,
        RollbackError,
        TargetDirectoryExistsError,
        TemplateSourceError,
    )
    from .git_local_manager import GitLocalManager
    from .logging_utils import get_logger
except ImportError:  # Direct execution of this file from the tools directory.
    from exceptions import (
        AdborgonCoreError,
        BootstrapError,
        InvalidProjectNameError,
        RollbackError,
        TargetDirectoryExistsError,
        TemplateSourceError,
    )
    from git_local_manager import GitLocalManager
    from logging_utils import get_logger

logger = get_logger(__name__)

_SCRIPT_VERSION: str = "1.0.0"

#: Upstream template repository, managed by the agent through its MCP tools.
TEMPLATE_REPOSITORY: str = "adborgon/GithubTemplate"

#: Fallback location where the agent materializes the template.
ADBORGON_ROOT: Path = Path(os.environ.get("ADBORGON_ROOT", r"C:\Adborgon"))
DEFAULT_TEMPLATE_DIR: Path = ADBORGON_ROOT / "templates" / TEMPLATE_REPOSITORY
TEMPLATE_SOURCE_ENV_VAR: str = "ADBORGON_TEMPLATE_SOURCE"

#: Characters allowed in a project (folder) name.
_PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: Device names that must never be used as folder names on Windows.
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: Template entries that must never be replicated into a new project.
_IGNORED_TEMPLATE_ENTRIES: tuple[str, ...] = (".git", "__pycache__", "*.pyc")


class ProjectBootstrapper:
    """Orchestrates the creation of a new local project from a template.

    Responsibilities (single, cohesive):
        * Validate the requested project name and description.
        * Replicate the locally materialized template.
        * Specialize the project metadata (``.Adborgon/project.json``) and
          the README.
        * Initialize the local Git repository and create the initial
          commit via :class:`GitLocalManager`.
        * Roll back (delete the created directory) on any failure.
    """

    def __init__(self, template_source: str | Path, workdir: str | Path) -> None:
        """Bind the orchestrator to a template source and a target parent.

        Args:
            template_source: Directory containing the template files
                downloaded from ``adborgon/GithubTemplate`` via MCP.
            workdir: Directory in which the new project folder is created.
        """
        self._template_source: Path = Path(template_source).expanduser().resolve()
        self._workdir: Path = Path(workdir).expanduser().resolve()

    @property
    def template_source(self) -> Path:
        """Directory containing the materialized template."""
        return self._template_source

    @property
    def workdir(self) -> Path:
        """Parent directory where project folders are created."""
        return self._workdir

    def bootstrap(self, name: str, description: str, *, author: str | None = None) -> Path:
        """Create a new project from the template.

        Args:
            name: Project name (also used as the folder name).
            description: Short project description recorded in the
                metadata and the README.
            author: Optional ``Name <email>`` identity for the initial
                commit. Defaults to the local Git configuration.

        Returns:
            The path of the created project directory.

        Raises:
            InvalidProjectNameError: When ``name`` is not a valid folder name.
            BootstrapError: When the description is empty or an unexpected
                failure occurs (after rollback).
            TargetDirectoryExistsError: When the target directory already exists.
            TemplateSourceError: When the template source is missing/invalid.
            RollbackError: When the automatic cleanup after a failure fails.
            GitLocalError subclasses: Propagated after rollback.
        """
        self._validate_name(name)
        if not description or not description.strip():
            raise BootstrapError("Project description must not be empty.")

        target = self._workdir / name
        if target.exists():
            raise TargetDirectoryExistsError(
                f"Target directory '{target}' already exists; refusing to overwrite it.",
                details={"target": str(target)},
            )

        logger.info(
            "Bootstrapping project '%s' from template '%s' into '%s'.",
            name,
            self._template_source,
            self._workdir,
        )
        try:
            self._replicate_template(target)
            self._write_project_metadata(target, name, description)
            self._customize_readme(target, name, description)
            self._initialize_git_repository(target, name, description, author)
        except Exception as exc:
            # Release the traceback first: the frames it references can keep
            # GitPython file handles alive (notably on Windows), which would
            # block the directory removal performed by the rollback.
            exc.__traceback__ = None
            gc.collect()
            self._rollback(target)
            if isinstance(exc, AdborgonCoreError):
                raise
            logger.exception("Unexpected failure while bootstrapping '%s'.", name)
            raise BootstrapError(
                f"Unexpected failure while bootstrapping '{name}': {exc}"
            ) from exc

        logger.info("Project '%s' successfully bootstrapped at '%s'.", name, target)
        return target

    # -- pipeline steps ---------------------------------------------------------
    def _replicate_template(self, target: Path) -> None:
        """Copy the template tree into the target project directory.

        Args:
            target: Destination project directory (must not exist yet).

        Raises:
            TemplateSourceError: When the template source is missing or is
                not a directory.
            BootstrapError: When the copy operation fails.
        """
        if not self._template_source.is_dir():
            raise TemplateSourceError(
                f"Template source '{self._template_source}' does not exist or is not a "
                f"directory. The Adborgon agent must first download the base files from "
                f"'{TEMPLATE_REPOSITORY}' using its GitHub MCP tools and materialize them "
                f"at that path (or pass --template-source explicitly).",
                details={
                    "template_source": str(self._template_source),
                    "repository": TEMPLATE_REPOSITORY,
                },
            )
        try:
            shutil.copytree(
                self._template_source,
                target,
                ignore=shutil.ignore_patterns(*_IGNORED_TEMPLATE_ENTRIES),
            )
        except OSError as exc:
            raise BootstrapError(
                f"Failed to replicate template into '{target}': {exc}"
            ) from exc
        logger.info("Template replicated from '%s' to '%s'.", self._template_source, target)

    def _write_project_metadata(self, target: Path, name: str, description: str) -> None:
        """Persist machine-readable project metadata under ``.Adborgon/``.

        Args:
            target: Project directory.
            name: Project name.
            description: Project description.

        Raises:
            BootstrapError: When the metadata cannot be written.
        """
        payload = {
            "name": name,
            "description": description,
            "template": {
                "repository": TEMPLATE_REPOSITORY,
                "materialized_from": str(self._template_source),
                "acquisition": "adborgon-agent-mcp",
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
            "bootstrap": {"script": "adborgon_bootstrap.py", "version": _SCRIPT_VERSION},
        }
        try:
            metadata_dir = target / ".Adborgon"
            metadata_dir.mkdir(parents=True, exist_ok=True)
            (metadata_dir / "project.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise BootstrapError(
                f"Failed to write project metadata into '{target}': {exc}"
            ) from exc
        logger.info("Project metadata written to '%s'.", target / ".Adborgon" / "project.json")

    def _customize_readme(self, target: Path, name: str, description: str) -> None:
        """Specialize the template README with the new project identity.

        The first H1 heading is replaced by the project name and the
        description is inserted right below it. When no H1 exists, a header
        block is prepended.

        Args:
            target: Project directory.
            name: Project name.
            description: Project description.

        Raises:
            BootstrapError: When the README cannot be updated.
        """
        readme = target / "README.md"
        if not readme.is_file():
            logger.debug("No README.md found in the template; skipping customization.")
            return
        lines = readme.read_text(encoding="utf-8").splitlines()
        header = [f"# {name}", "", description.strip(), ""]
        output: list[str] = []
        replaced = False
        skip_next_blank = False
        for line in lines:
            if skip_next_blank:
                skip_next_blank = False
                if line.strip() == "":
                    continue  # avoid a double blank line after the inserted header
            if not replaced and line.lstrip().startswith("# "):
                output.extend(header)
                replaced = True
                skip_next_blank = True
                continue
            output.append(line)
        if not replaced:
            output = header + lines
        try:
            readme.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")
        except OSError as exc:
            raise BootstrapError(f"Failed to update README in '{target}': {exc}") from exc
        logger.info("README.md specialized for project '%s'.", name)

    def _initialize_git_repository(
        self, target: Path, name: str, description: str, author: str | None
    ) -> None:
        """Initialize Git, stage the template files and create the initial commit.

        Args:
            target: Project directory to version.
            name: Project name (used in the commit message).
            description: Project description (used in the commit body).
            author: Optional commit identity (``Name <email>``).

        Raises:
            GitLocalError subclasses: Propagated to the caller (rollback
                is triggered by :meth:`bootstrap`).
        """
        manager = GitLocalManager(target, default_branch="main")
        manager.init()
        manager.stage(["."])
        commit_message = (
            f"chore: bootstrap project '{name}' from {TEMPLATE_REPOSITORY}\n\n"
            f"{description.strip()}"
        )
        manager.commit(commit_message, author=author)
        branch = manager.current_branch()
        logger.info(
            "Initial commit created on branch '%s'; working tree clean=%s.",
            branch or "(detached HEAD)",
            manager.is_clean(),
        )

    # -- validation & rollback ---------------------------------------------------
    @staticmethod
    def _validate_name(name: str) -> None:
        """Validate the project (folder) name.

        Args:
            name: Candidate project name.

        Raises:
            InvalidProjectNameError: When the name is not a safe folder name.
        """
        if not name:
            raise InvalidProjectNameError("Project name must not be empty.")
        if not _PROJECT_NAME_PATTERN.match(name):
            raise InvalidProjectNameError(
                f"Invalid project name '{name}': use letters, digits, '.', '_' or '-' "
                "and start with a letter or digit.",
                details={"name": name},
            )
        if name.upper().split(".")[0] in _WINDOWS_RESERVED_NAMES:
            raise InvalidProjectNameError(
                f"Project name '{name}' is a reserved Windows device name.",
                details={"name": name},
            )

    def _rollback(self, target: Path) -> None:
        """Remove a partially created project directory.

        Safe to call when the target does not exist (no-op).

        Args:
            target: Project directory to delete.

        Raises:
            RollbackError: When the directory cannot be removed; manual
                cleanup is then required.
        """
        if not target.exists():
            return
        last_error: OSError | None = None
        for attempt in range(1, 4):
            try:
                _rmtree_safe(target)
            except OSError as exc:
                last_error = exc
                logger.warning(
                    "Rollback attempt %d failed for '%s': %s",
                    attempt,
                    target,
                    exc,
                )
                time.sleep(0.5 * attempt)
                continue
            logger.warning(
                "Rollback complete: removed partially created project at '%s' (attempt %d).",
                target,
                attempt,
            )
            return
        raise RollbackError(
            f"Rollback FAILED: could not remove '{target}'; manual cleanup required.",
            details={"target": str(target), "error": str(last_error)},
        ) from last_error


# -- CLI layer --------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="adborgon_bootstrap",
        description=(
            f"Bootstrap a new local project from the '{TEMPLATE_REPOSITORY}' template. "
            "The template files must already be materialized locally by the Adborgon agent "
            "(GitHub MCP). The script replicates the structure, initializes the local "
            "Git repository and creates the initial commit, rolling back automatically "
            "on failure."
        ),
    )
    parser.add_argument(
        "--name",
        required=True,
        metavar="PROJECT_NAME",
        help="Name of the new project (folder name).",
    )
    parser.add_argument(
        "--description",
        required=True,
        metavar="TEXT",
        help="Short description of the new project.",
    )
    parser.add_argument(
        "--template-source",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Local directory with the template files downloaded via MCP "
            f"(default: ${TEMPLATE_SOURCE_ENV_VAR} or {DEFAULT_TEMPLATE_DIR})."
        ),
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.cwd(),
        metavar="DIR",
        help="Parent directory for the new project (default: current working directory).",
    )
    parser.add_argument(
        "--author",
        default=os.environ.get("ADBORGON_AUTHOR"),
        metavar="NAME <EMAIL>",
        help=(
            "Identity for the initial commit "
            "(default: local Git configuration or $ADBORGON_AUTHOR)."
        ),
    )
    return parser


def _rmtree_safe(target: Path) -> None:
    """Recursively remove a tree, handling read-only files (Windows).

    Git marks loose objects as read-only; on Windows ``os.unlink`` fails
    with ``PermissionError`` (WinError 5) for such files unless the
    read-only attribute is cleared first. This helper transparently
    clears the attribute and retries the failed operation.

    Args:
        target: Directory tree to remove.

    Raises:
        OSError: When a file or directory cannot be removed even after
            the read-only attribute is cleared.
    """
    def _on_error(func: object, path: str, exc_info: object) -> None:
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            pass
        func(path)  # type: ignore[operator]

    try:  # Python 3.12+
        shutil.rmtree(target, onexc=_on_error)  # type: ignore[call-overload]
    except TypeError:  # Python <= 3.11
        shutil.rmtree(target, onerror=_on_error)  # type: ignore[call-overload]


def _resolve_template_source(cli_value: Path | None) -> Path:
    """Resolve the effective template source directory.

    Priority: CLI flag > environment variable > built-in default.

    Args:
        cli_value: Value provided via ``--template-source`` (or ``None``).

    Returns:
        The resolved template source path.
    """
    if cli_value is not None:
        return cli_value.expanduser().resolve()
    env_value = os.environ.get(TEMPLATE_SOURCE_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return DEFAULT_TEMPLATE_DIR


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: ``0`` on success, ``1`` on a handled bootstrap
        failure, ``2`` on an unhandled error or invalid workdir.
    """
    args = build_parser().parse_args(argv)
    template_source = _resolve_template_source(args.template_source)
    workdir = args.workdir.expanduser().resolve()
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create workdir '%s': %s", workdir, exc)
        return 2

    bootstrapper = ProjectBootstrapper(template_source=template_source, workdir=workdir)
    try:
        target = bootstrapper.bootstrap(
            name=args.name,
            description=args.description,
            author=args.author,
        )
    except AdborgonCoreError as exc:
        logger.error("Bootstrap aborted: %s", exc)
        return 1
    except Exception:
        logger.exception("Unhandled error during bootstrap.")
        return 2

    print(f"Project '{args.name}' bootstrapped successfully at: {target}")
    print(f"Template: {TEMPLATE_REPOSITORY} (materialized at {template_source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
