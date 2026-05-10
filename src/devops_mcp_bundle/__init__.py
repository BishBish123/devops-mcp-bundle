"""DevOps MCP Bundle: 3 Model Context Protocol servers + Claude Code Skills pack."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version

# `requires-python = ">=3.11,<3.13"` in pyproject.toml gates installs done
# through tools that respect markers (pip, uv). It does NOT protect users
# who import the package out of a checkout, an editable install made with
# the wrong interpreter, or a sys.path injection. Surface a clear error
# at import time rather than letting a 3.9 syntax error or a 3.13-only
# behaviour difference surface deep inside fastmcp / asyncpg.
if sys.version_info < (3, 11) or sys.version_info >= (3, 13):
    _found = ".".join(str(p) for p in sys.version_info[:3])
    raise RuntimeError(
        f"devops-mcp-bundle requires Python 3.11 or 3.12, found {_found}. "
        "Install a supported interpreter (uv python install 3.12, "
        "pyenv install 3.12, or brew install python@3.12) and re-run with it."
    )

try:
    __version__ = version("devops-mcp-bundle")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
