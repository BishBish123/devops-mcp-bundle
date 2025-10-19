"""DevOps MCP Bundle: 3 Model Context Protocol servers + Claude Code Skills pack."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("devops-mcp-bundle")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
