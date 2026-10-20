"""Local Git version-control operations built on GitPython.

This module deliberately avoids shelling out to the ``git`` binary (no
``subprocess``/``os.system``): every operation goes through the GitPython
API, which keeps behaviour deterministic, fully typeable and testable
across platforms.

Typical usage::

    from Core.tools.git_local_manager import GitLocalManager

    manager = GitLocalManager("C:/projects/demo")
    manager.init()
    manager.create_branch("feature/x")
    manager.stage(["."])
    manager.commit("chore: initial commit")
    manager.add_remote("origin", "https://github.com/adborgon/demo.git")
    manager.push()
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from git import Actor, PushInfo, Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

try:  # Package context (``Core.tools`` or a standalone ``tools`` package).
    from .exceptions import (
        DirtyWorktreeError,
        GitLocalError,
        GitOperationError,
        RepositoryNotFoundError,
    )
    from .logging_utils import get_logger
except ImportError:  # Direct execution of a script from the tools directory.
    from exceptions import (
        DirtyWorktreeError,
        GitLocalError,
        GitOperationError,
        RepositoryNotFoundError,
    )
    from logging_utils import get_logger

logger = get_logger(__name__)

#: Format of a commit identity: ``Name <email>``.
_ACTOR_PATTERN = re.compile(r"^(?P<name>.+?)\s*<(?P<email>[^<>]+)>\s*$")

#: Branch names must not start with a slash/dot, contain ``..`` or ``@{``,
#: or end with ``.lock`` (reserved by Git).
_BRANCH_NAME_PATTERN = re.compile(
    r"^(?!/)"                 # no leading slash
    r"(?!\.)"                 # no leading dot
    r"(?!.*\.\.)"             # no path traversal
    r"(?!.*@{)"               # no revision-list token
    r"(?!.*\.lock$)"          # no .lock suffix (reserved)
    r"(?!.*//$)"              # no trailing slash
    r"[A-Za-z0-9._/-]+$"      # allowed charset, at least one character
)

#: Remote names: single namespace segment (no slashes), starting with an
#: alphanumeric character, no ``..``, no trailing dot, no ``.lock`` suffix.
_REMOTE_NAME_PATTERN = re.compile(
    r"^(?!.*\.\.)"             # no path traversal
    r"(?!.*\.lock$)"          # no .lock suffix (reserved)
    r"(?!.*\.$)"               # no trailing dot
    r"[A-Za-z0-9][A-Za-z0-9._-]*$"
)


class GitLocalManager:
    """High-level manager for a local Git repository.

    The manager wraps :class:`git.Repo` with input validation, consistent
    error translation (GitPython exceptions become :class:`GitLocalError`
    subclasses) and structured logging, so callers never handle raw
    GitPython exceptions.

    Attributes:
        repo_path: Absolute path of the repository working tree.
        default_branch: Branch name used by :meth:`init` when no explicit
            branch is provided.
    """

    def __init__(self, repo_path: str | Path, *, default_branch: str = "main") -> None:
        """Bind the manager to a repository path.

        Args:
            repo_path: Path of the (future) repository working tree.
            default_branch: Initial branch name used by :meth:`init`.
        """
        self._repo_path: Path = Path(repo_path).expanduser().resolve()
        self._default_branch: str = default_branch

    @property
    def repo_path(self) -> Path:
        """Absolute path of the managed repository."""
        return self._repo_path

    @property
    def default_branch(self) -> str:
        """Initial branch name used by :meth:`init`."""
        return self._default_branch

    @property
    def is_repository(self) -> bool:
        """Whether a Git repository currently exists at :attr:`repo_path`."""
        if not self._repo_path.exists():
            return False
        try:
            Repo(self._repo_path, search_parent_directories=False)
            return True
        except (InvalidGitRepositoryError, NoSuchPathError):
            return False

    # -- repository lifecycle ---------------------------------------------
    def init(self, initial_branch: str | None = None) -> Repo:
        """Initialize a new repository (idempotent).

        If a repository already exists at the path, it is returned as-is
        instead of reinitializing.

        Args:
            initial_branch: Name of the initial branch. Defaults to
                :attr:`default_branch`.

        Returns:
            The initialized :class:`git.Repo` instance.

        Raises:
            GitOperationError: When ``git init`` fails (e.g., invalid
                branch name, unsupported Git version, permission error).
        """
        branch = initial_branch or self._default_branch
        if self.is_repository:
            logger.info("Repository already initialized at '%s'; reusing it.", self._repo_path)
            return self._require_repository()
        logger.info(
            "Initializing new Git repository at '%s' (branch '%s').", self._repo_path, branch
        )
        try:
            repo = Repo.init(str(self._repo_path), initial_branch=branch)
        except (GitCommandError, OSError) as exc:
            raise GitOperationError(
                f"Failed to initialize repository at '{self._repo_path}': {exc}",
                details={"repo_path": str(self._repo_path), "initial_branch": branch},
            ) from exc
        return repo

    def is_clean(self, *, include_untracked: bool = True) -> bool:
        """Check whether the working tree has pending changes.

        Args:
            include_untracked: Whether untracked files count as dirty.

        Returns:
            ``True`` when the working tree is clean.

        Raises:
            RepositoryNotFoundError: When no repository exists at the path.
            GitOperationError: When the status cannot be determined.
        """
        repo = self._require_repository()
        try:
            return not repo.is_dirty(untracked_files=include_untracked)
        except GitCommandError as exc:
            raise GitOperationError(
                f"Unable to determine repository status at '{self._repo_path}': {exc}"
            ) from exc

    def current_branch(self) -> str:
        """Return the name of the active branch.

        Returns:
            The branch name, or an empty string in detached-HEAD state.

        Raises:
            RepositoryNotFoundError: When no repository exists at the path.
        """
        repo = self._require_repository()
        try:
            return repo.active_branch.name
        except TypeError:  # detached HEAD
            return ""

    # -- branches -----------------------------------------------------------
    def create_branch(self, name: str, *, start_point: str | None = None, checkout: bool = True) -> None:
        """Create a new local branch.

        Args:
            name: Name of the branch to create.
            start_point: Git reference from which the branch starts
                (e.g., ``main`` or a commit SHA). Defaults to the current
                HEAD.
            checkout: Switch to the new branch once created.

        Raises:
            GitLocalError: When the branch name is invalid.
            GitOperationError: When the branch already exists or Git fails.
        """
        self._validate_branch_name(name)
        repo = self._require_repository()
        existing = {head.name for head in repo.heads}
        if name in existing:
            raise GitOperationError(
                f"Branch '{name}' already exists in '{self._repo_path}'.",
                details={"branch": name},
            )
        try:
            head = repo.create_head(name, start_point)
            if checkout:
                head.checkout()
        except (GitCommandError, OSError, ValueError) as exc:
            raise GitOperationError(f"Could not create branch '{name}': {exc}") from exc
        logger.info(
            "Created branch '%s'%s.", name, " and checked it out" if checkout else ""
        )

    def checkout(self, name: str) -> None:
        """Switch to an existing local branch.

        Args:
            name: Name of the local branch to activate.

        Raises:
            RepositoryNotFoundError: When no repository exists at the path.
            GitOperationError: When the branch does not exist or Git fails.
        """
        repo = self._require_repository()
        try:
            repo.heads[name].checkout()
        except (KeyError, GitCommandError, OSError, ValueError) as exc:
            raise GitOperationError(
                f"Could not checkout branch '{name}' in '{self._repo_path}': {exc}"
            ) from exc
        logger.info("Checked out branch '%s'.", name)

    # -- index & commits -----------------------------------------------------
    def stage(self, paths: Sequence[str | Path] = (".",)) -> None:
        """Stage file changes in the index.

        Honours ``.gitignore`` rules, exactly like ``git add``. (GitPython's
        ``index.add`` is deliberately avoided here: it walks the working
        tree itself and writes index entries directly, bypassing ignore
        rules.)

        Args:
            paths: Paths to stage, relative to the repository root (or
                absolute paths inside the repository). Defaults to the
                entire working tree.

        Raises:
            GitLocalError: When a path lies outside the repository.
            GitOperationError: When the underlying Git operation fails
                (including when an explicitly requested path is ignored).
        """
        repo = self._require_repository()
        normalized = [self._to_repo_relative(path) for path in paths]
        try:
            repo.git.add(*normalized)
        except (GitCommandError, OSError) as exc:
            raise GitOperationError(
                f"Failed to stage {normalized} in '{self._repo_path}': {exc}"
            ) from exc
        logger.info("Staged %d path(s) in '%s'.", len(normalized), self._repo_path)

    def commit(self, message: str, *, author: str | None = None, committer: str | None = None) -> str:
        """Create a commit from the current index.

        Args:
            message: Commit message. The first line is the subject; the
                remainder (after a blank line) becomes the body.
            author: Optional author identity in ``Name <email>`` format.
                Defaults to the local Git configuration.
            committer: Optional committer identity in ``Name <email>``
                format. Defaults to the local Git configuration.

        Returns:
            The hex SHA of the created commit.

        Raises:
            GitLocalError: When the message is empty or an identity is
                malformed.
            DirtyWorktreeError: When there is nothing staged to commit.
            GitOperationError: When the underlying Git operation fails.
        """
        if not message or not message.strip():
            raise GitLocalError("Commit message must not be empty.")
        repo = self._require_repository()
        if not self._has_staged_changes(repo):
            raise DirtyWorktreeError(
                "Nothing staged to commit.", details={"repo_path": str(self._repo_path)}
            )
        commit_kwargs: dict[str, Actor] = {}
        if author is not None:
            commit_kwargs["author"] = self._parse_actor(author, "author")
        if committer is not None:
            commit_kwargs["committer"] = self._parse_actor(committer, "committer")
        try:
            commit = repo.index.commit(message, **commit_kwargs)
        except GitCommandError as exc:
            stderr_text = str(getattr(exc, "stderr", "")).strip()
            raise GitOperationError(
                f"Commit failed in '{self._repo_path}': {exc}" + (f" [{stderr_text}]" if stderr_text else ""),
                details={"repo_path": str(self._repo_path)},
            ) from exc
        subject = message.splitlines()[0]
        logger.info("Created commit %s ('%s').", commit.hexsha[:12], subject)
        return commit.hexsha

    # -- remotes & publishing --------------------------------------------------
    def remotes(self) -> dict[str, str]:
        """Return the configured remotes as a ``name -> URL`` mapping.

        Returns:
            A dict mapping each remote name to its (first) fetch URL.

        Raises:
            RepositoryNotFoundError: When no repository exists at the path.
        """
        repo = self._require_repository()
        return {remote.name: next(iter(remote.urls), "") for remote in repo.remotes}

    def add_remote(self, name: str, url: str) -> None:
        """Register a remote (idempotent).

        When a remote with the same name already exists, this is a no-op if
        it points to the same URL and an error if it points elsewhere.

        Args:
            name: Remote name (e.g., ``origin``). A single namespace
                segment: no slashes allowed.
            url: URL of the remote.

        Raises:
            GitLocalError: When the name or URL is invalid.
            RepositoryNotFoundError: When no repository exists at the path.
            GitOperationError: When the remote exists with a different URL
                or when creating it fails.
        """
        self._validate_remote_name(name)
        url = url.strip()
        if not url:
            raise GitLocalError("Remote URL must not be empty.")
        repo = self._require_repository()
        existing = {r.name: next(iter(r.urls), "") for r in repo.remotes}
        if name in existing:
            if existing[name] == url:
                logger.info("Remote '%s' already points to '%s'; nothing to do.", name, url)
                return
            raise GitOperationError(
                f"Remote '{name}' already exists in '{self._repo_path}' pointing to "
                f"'{existing[name]}' (requested '{url}').",
                details={"remote": name, "existing_url": existing[name], "requested_url": url},
            )
        try:
            repo.create_remote(name, url)
        except (GitCommandError, OSError, ValueError) as exc:
            raise GitOperationError(
                f"Could not create remote '{name}' in '{self._repo_path}': {exc}"
            ) from exc
        logger.info("Created remote '%s' -> '%s'.", name, url)

    def push(self, *, remote: str = "origin", refspec: str | None = None, force: bool = False) -> list[PushInfo]:
        """Push local refs to a remote.

        Args:
            remote: Name of the remote to push to (default ``origin``).
            refspec: Refspec to push (e.g., ``main`` or
                ``refs/tags/v1.0.0:refs/tags/v1.0.0``). Defaults to the
                active branch.
            force: Allow non-fast-forward pushes.

        Returns:
            The :class:`git.PushInfo` results. Each result carries a
            ``flags`` bitmask (``git.PushInfo.UP_TO_DATE``, ``NEW_BRANCH``,
            ``FAST_FORWARD``, ...) plus a human-readable ``summary`` line

        Raises:
            RepositoryNotFoundError: When no repository exists at the path.
            GitLocalError: When no refspec can be determined (detached
                HEAD) or the refspec is invalid.
            GitOperationError: When the remote does not exist or the push
                fails (authentication, network, non-fast-forward, ...).
        """
        repo = self._require_repository()
        if refspec is None:
            refspec = self.current_branch()
            if not refspec:
                raise GitLocalError(
                    "Cannot determine refspec: repository is in detached-HEAD state; "
                    "pass refspec explicitly.",
                    details={"repo_path": str(self._repo_path)},
                )
        if not refspec or ".." in refspec or refspec.startswith(("-", "@")):
            raise GitLocalError(
                f"Invalid refspec '{refspec}'.", details={"refspec": refspec}
            )
        try:
            remote_obj = repo.remotes[remote]
        except (KeyError, IndexError) as exc:
            raise GitOperationError(
                f"Remote '{remote}' not found in '{self._repo_path}'. Call add_remote() first.",
                details={"remote": remote},
            ) from exc
        try:
            results = remote_obj.push(refspec, force=force)
        except (GitCommandError, OSError) as exc:
            stderr_text = str(getattr(exc, "stderr", "")).strip()
            raise GitOperationError(
                f"Push to '{remote}' failed in '{self._repo_path}': {exc}"
                + (f" [{stderr_text}]" if stderr_text else ""),
                details={"remote": remote, "refspec": refspec},
            ) from exc
        for info in results:
            flags = str(getattr(info, "flags", "") or "")
            summary = (getattr(info, "summary", "") or "").strip()
            logger.info("Push '%s' to '%s': %s (%s).", refspec, remote, summary, flags or "ok")
        return results

    # -- internals ------------------------------------------------------------
    def _require_repository(self) -> Repo:
        """Return an open repository or raise a descriptive error.

        Returns:
            The open :class:`git.Repo` instance.

        Raises:
            RepositoryNotFoundError: When no repository exists at the path.
        """
        try:
            return Repo(self._repo_path)
        except InvalidGitRepositoryError as exc:
            raise RepositoryNotFoundError(
                f"No Git repository found at '{self._repo_path}'. Call init() first.",
                details={"repo_path": str(self._repo_path)},
            ) from exc

    @staticmethod
    def _has_staged_changes(repo: Repo) -> bool:
        """Determine whether the index holds changes relative to HEAD.

        Args:
            repo: Open repository (may have no commits yet, e.g. right
                after ``init``).

        Returns:
            ``True`` when committing would produce a non-empty commit.
        """
        if not repo.head.is_valid():
            return bool(repo.index.entries)
        return bool(repo.index.diff("HEAD"))

    @staticmethod
    def _parse_actor(identity: str, role: str) -> Actor:
        """Parse a ``Name <email>`` identity string.

        Args:
            identity: Identity string, e.g. ``Adborgon Agent <agent@adborgon.local>``.
            role: Role name used in error messages.

        Returns:
            The parsed :class:`git.Actor`.

        Raises:
            GitLocalError: When the string is not a valid identity.
        """
        match = _ACTOR_PATTERN.match(identity.strip())
        if not match or "@" not in match.group("email"):
            raise GitLocalError(
                f"Invalid {role} identity '{identity}': expected 'Name <email>'."
            )
        try:
            return Actor.from_string(identity.strip())  # GitPython >= 3.2
        except AttributeError:
            return Actor.commit(identity.strip())  # GitPython 3.1.x

    def _to_repo_relative(self, path: str | Path) -> str:
        """Normalize a path to a repo-relative, forward-slash string.

        Args:
            path: Path relative to the repository root, or an absolute
                path inside the repository.

        Returns:
            The path as Git expects it (e.g., ``src/main.py`` or ``.``).

        Raises:
            GitLocalError: When the path resolves outside the repository.
        """
        candidate = Path(path)
        if candidate.is_absolute():
            base = candidate
        else:
            base = self._repo_path / candidate
        try:
            relative = base.resolve().relative_to(self._repo_path)
        except ValueError as exc:
            raise GitLocalError(
                f"Path '{path}' is outside the repository at '{self._repo_path}'.",
                details={"path": str(path)},
            ) from exc
        return relative.as_posix()

    @staticmethod
    def _validate_remote_name(name: str) -> None:
        """Validate a remote name (single namespace segment).

        Args:
            name: Candidate remote name.

        Raises:
            GitLocalError: When the name is not a valid remote reference.
        """
        if not name or not _REMOTE_NAME_PATTERN.match(name):
            raise GitLocalError(
                f"Invalid remote name '{name}': use letters, digits, '.', '_', '-' "
                "without slashes, '..', a trailing dot or a '.lock' suffix."
            )

    @staticmethod
    def _validate_branch_name(name: str) -> None:
        """Validate a branch name against Git's reference rules.

        Args:
            name: Candidate branch name.

        Raises:
            GitLocalError: When the name is not a valid branch reference.
        """
        if not name or not _BRANCH_NAME_PATTERN.match(name):
            raise GitLocalError(
                f"Invalid branch name '{name}': use letters, digits, '.', '_', '/' "
                "without leading dot/slash, '..', '@{' or a '.lock' suffix."
            )
