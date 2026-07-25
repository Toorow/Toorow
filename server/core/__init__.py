"""toorow MCP core (kernel).

The core knows nothing about any specific marketing platform. All platform
knowledge lives in self-contained modules under ``server/modules/<name>/`` and
is composed via FastMCP ``mount()`` (see AD-2). Story 1.1 scaffolds only the
core skeleton and a ``health`` tool.
"""

__version__ = "0.1.0"
