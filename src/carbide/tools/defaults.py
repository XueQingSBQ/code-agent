"""Default tool registry setup."""

from __future__ import annotations

from pathlib import Path

from carbide.tools.registry import ToolRegistry


def create_default_registry(workspace: Path | None = None) -> ToolRegistry:
    """Create a registry with default file system tools."""
    # Tools receive their workspace through ToolContext at invocation time.
    # Accepting the historical argument keeps integrations source-compatible;
    # the registry itself must remain reusable across workspaces.
    del workspace
    from carbide.tools.fs import EditTool, ReadTool, WriteTool
    from carbide.tools.search import GlobTool, GrepTool
    from carbide.tools.shell import BashTool

    registry = ToolRegistry()

    # Register file system tools
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(GrepTool())
    registry.register(GlobTool())

    # Register shell tool (M1: persistent BashSession)
    registry.register(BashTool())

    return registry
