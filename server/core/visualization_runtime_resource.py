"""Story 50.5 AC16(c) -- the shared Visualization runtime, served as ONE core-owned
MCP App resource.

WHAT THIS REPLACES, and it is not a tidy-up.
`server/core/main.py` resolved the core-scoped `ui://core/daily-report` resource by
SCANNING the loaded connectors for whichever one declared
`widget_ref == "ui://core/daily-report"` in its manifest, and serving
`ui/widgets/<that connector's name>/dist/index.html`. Twenty-six connector
manifests declare that ref (`grep -rl '"widget_ref"' server/modules/*/manifest.json`),
so **whichever connector happened to load first decided how the standard report
looked**. That is the connector-owned standard rendering `ARCHITECTURE-SPINE.md:51`
(AD-2) names by phrase, and it is deleted rather than reconfigured.

WHAT REPLACES IT. `ui://core/visualization-runtime` -- one core-owned resource
whose bytes are the built shared runtime, identical for every connector, every
organization and every host. The URI literal is exported so Story 50.6's render tool
imports it instead of retyping it; the same URI is never registered from two
modules.

THE HONEST STATE THIS OPENS, written down rather than discovered at runtime.
With the connector scan gone, `ui://core/daily-report` has no bundle to resolve and
serves its graceful not-built placeholder to EVERY host until Story 50.6's render
tool attaches `ui://core/visualization-runtime` instead. That is intended: a
placeholder saying the widget is not built is a TRUE statement, whereas whichever
connector loaded first deciding how the standard report looks is a false one. If
50.6 slips, the honest placeholder is what ships -- not a re-enabled scan.

NO `mime_type` ON A `ui://` RESOURCE. FastMCP auto-serves it as
`text/html;profile=mcp-app` (`fastmcp.apps.UI_MIME_TYPE`), the profile MCP Apps
hosts key off. Forcing `mime_type="text/html"` would drop the profile
(`server/core/main.py`, Story 9.10 AC1).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

#: THE literal. Story 50.6's render tool imports this constant; it never retypes
#: the string, and no other module registers this URI.
VISUALIZATION_RUNTIME_URI = "ui://core/visualization-runtime"

_REPO_ROOT = Path(__file__).parent.parent.parent

#: `ui/cards/shell/dist/viz/mcp-app.html`, produced by `pnpm --filter
#: @toorow/card-shell build` (`ui/cards/shell/vite.config.ts`) and verified by
#: `node ui/scripts/bundle-check.mjs`. Overridable for a deployment that builds
#: elsewhere; core never names a connector.
_DEFAULT_DIST = _REPO_ROOT / "ui" / "cards" / "shell" / "dist" / "viz" / "mcp-app.html"


def runtime_bundle_path() -> Path:
    override = os.environ.get("TOOROW_VISUALIZATION_RUNTIME_DIST")
    return Path(override) if override else _DEFAULT_DIST


#: Served when the bundle has not been built. It names what to run, so a reader
#: meets an instruction rather than a dead end.
NOT_BUILT_PLACEHOLDER = (
    '<!doctype html><html lang="en"><body>'
    "<p>The shared Visualization runtime bundle is not built.</p>"
    "<p>Run <code>pnpm --filter @toorow/card-shell build</code>, which emits "
    "<code>ui/cards/shell/dist/viz/mcp-app.html</code>.</p>"
    "</body></html>"
)


def read_runtime_bundle() -> str:
    """Return the built runtime, or a placeholder that says what to run.

    It never raises: a missing bundle must not take the MCP server down at startup
    or on a read. It also never falls back to another bundle -- serving *some*
    HTML because the right one is absent is how a connector ends up owning the
    standard renderer again.
    """
    path = runtime_bundle_path()
    if not path.exists():
        logging.warning(
            "Shared Visualization runtime not built: %s -- run "
            "`pnpm --filter @toorow/card-shell build`",
            path,
        )
        return NOT_BUILT_PLACEHOLDER
    return path.read_text(encoding="utf-8")


def register(mcp) -> None:
    """Attach the resource to a FastMCP server.

    Called once, from `server/core/main.py`. A module-level `@mcp.resource`
    decorator here would bind at import time to whichever server object happened
    to be importable, which is how the same URI ends up registered twice.
    """

    @mcp.resource(VISUALIZATION_RUNTIME_URI)
    def visualization_runtime() -> str:  # pragma: no cover - thin FastMCP binding
        """Serve the shared Visualization runtime (Story 50.5)."""
        return read_runtime_bundle()
