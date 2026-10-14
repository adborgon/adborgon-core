"""Logging helpers for Bionic Core.

Every module obtains its logger through :func:`get_logger`, which guarantees
a single, consistent format and timestamp layout across the package while
keeping full traceability (module-level logger names are preserved).

The helper is deliberately non-invasive: if the host application (e.g., the
Bionic orchestrator) has already attached handlers to the root logger, this
package does not add its own and the host configuration wins.
"""
from __future__ import annotations

import logging
import sys

_LOG_FORMAT: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"
_DEFAULT_LEVEL: int = logging.INFO

_default_handler_installed: bool = False


def _install_default_handler_if_needed() -> None:
    """Attach a stream handler to the root logger when none is present.

    This keeps command-line usage informative (stderr, timestamped) without
    overriding a logging configuration provided by the host application.
    """
    global _default_handler_installed
    if _default_handler_installed:
        return
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        root.addHandler(handler)
        root.setLevel(_DEFAULT_LEVEL)
    _default_handler_installed = True


def get_logger(name: str | None = None, *, level: int = _DEFAULT_LEVEL) -> logging.Logger:
    """Return a logger for a Bionic Core module.

    Args:
        name: Dotted logger name, typically the module's ``__name__`` so
            each tool keeps an independent, traceable namespace. When
            omitted, the package-level ``bionic.core`` logger is returned.
        level: Minimum severity for the returned logger when it has no
            explicit level configured.

    Returns:
        A configured :class:`logging.Logger` instance.
    """
    _install_default_handler_if_needed()
    logger = logging.getLogger(name) if name is not None else logging.getLogger("bionic.core")
    if logger.level == logging.NOTSET:
        logger.setLevel(level)
    return logger
