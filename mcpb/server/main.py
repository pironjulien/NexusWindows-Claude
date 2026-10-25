"""Entry point of the Claude Desktop extension (.mcpb): the nexus-windows-claude server from PyPI, pinned in
pyproject.toml next to this file and installed by Claude Desktop with uv."""

from nexus_windows.reloader import main

main()
