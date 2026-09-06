"""Core-owned ``ui://core/*`` resource surface — every widget the core serves.

ONE SURFACE, ONE FILE. These eleven resources were declared inline in
``core.main`` with ``@mcp.resource`` decorators, which made the entrypoint the
home of both the FastMCP composition and the bytes of every widget. They share a
single concern — serving a built single-file bundle, or an honest placeholder
when it has not been built — so they are one module with one registration site.

The registration follows the ``register(mcp)`` shape already used by
``inbound_mcp`` and the twenty ``*_mcp`` modules ``core.main`` composes: the
functions are plain callables here, and ``register`` is the only place that
binds a URI. ``core.main`` re-exports the names it used to define, so
``from core.main import DAILY_REPORT_WIDGET_URI`` keeps meaning what it meant.

Core stays source-agnostic (AD-2): a widget path is a build constant or an
explicit environment override — never a connector name, and never a scan of the
loaded modules.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Daily-report widget resource (Story 1.6, AD-11 / AD-1).
#
# The get_daily_report tool is CORE-owned (AD-2), so its widget URI is
# core-scoped: "ui://core/daily-report". The built single-file HTML is served as
# a FastMCP resource; the MCP client (Claude) loads it into the widget iframe via
# _meta.ui.resourceUri.
#
# Core stays source-agnostic (AD-2): the widget artifact location is a build-path
# constant (overridable via TOOROW_DAILY_REPORT_WIDGET_DIST) — no module
# branching or module-specific logic lives here.
# ---------------------------------------------------------------------------
DAILY_REPORT_WIDGET_URI = "ui://core/daily-report"

_REPO_ROOT = Path(__file__).parent.parent.parent
_WIDGETS_DIR = _REPO_ROOT / "ui" / "widgets"


def _resolve_widget_dist() -> Path:
    """Resolve the daily-report widget's built HTML path.

    Story 50.5 AC16(c) DELETED the connector scan that used to live here.

    What it did: it walked ``_loaded_modules`` for whichever connector declared
    ``widget_ref == DAILY_REPORT_WIDGET_URI`` in its manifest and served
    ``ui/widgets/<that connector's name>/dist/index.html``. Twenty-six connector
    manifests declare that ref, so **whichever connector loaded first decided how
    the standard report looked** — the connector-owned standard rendering
    ``ARCHITECTURE-SPINE.md:51`` (AD-2) names by phrase. A core resource whose
    bytes depend on connector load order is not source-agnostic; it is
    source-dependent in a way nobody can see.

    What remains: the explicit ``TOOROW_DAILY_REPORT_WIDGET_DIST`` override, and
    otherwise a path that does not exist, which serves the graceful placeholder
    below.

    THE CONSEQUENCE, STATED RATHER THAN DISCOVERED. Without the scan, this
    resolves under ``_unresolved`` on every deployment that does not set the
    override, so ``ui://core/daily-report`` serves the not-built placeholder to
    every host until Story 50.6's render tool attaches
    ``ui://core/visualization-runtime`` (registered by ``core.main``) in its
    place. That is the intended honest state: a placeholder saying the widget is
    not built is a true statement; whichever connector happened to load first
    deciding how the standard report looks is a false one. The window closes when
    50.6 lands. If 50.6 slips, the honest placeholder is what ships — not a
    re-enabled scan.
    """
    override = os.environ.get("TOOROW_DAILY_REPORT_WIDGET_DIST")
    if override:
        return Path(override)
    return _WIDGETS_DIR / "_unresolved" / "dist" / "index.html"


_WIDGET_PATH = _resolve_widget_dist()


def daily_report_widget() -> str:
    """Serve the daily-report signature heatmap widget HTML (Story 1.6).

    Returns the built single-file bundle. If the widget has not been built yet
    (dist/index.html missing), returns a graceful placeholder instead of raising
    so the server never crashes at startup or on read.
    """
    if not _WIDGET_PATH.exists():
        logging.warning(
            "Daily-report widget not built: %s — run the UI widget build first",
            _WIDGET_PATH,
        )
        # Story 50.5 AC16(c): the placeholder NAMES its replacement, so a reader
        # who meets it learns what is happening rather than only that something
        # is absent.
        return (
            "<!doctype html><html lang=\"en\"><body>"
            "<p>This widget is not built.</p>"
            "<p>The standard rendering path is being replaced by the shared "
            "Visualization runtime, served as "
            "<code>ui://core/visualization-runtime</code>. Story 50.6 attaches it "
            "to this tool; until then this placeholder is served rather than a "
            "bundle chosen by whichever connector loaded first.</p>"
            "</body></html>"
        )
    return _WIDGET_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Card widget resources (Story 9.1, Epic 9 -- AD-11 / AD-1 / AD-2).
#
# Each card template is served as a core-scoped FastMCP resource
# ("ui://core/card-<id>"), resolved with the SAME source-agnostic pattern as the
# daily-report widget: an env override, else the single-file build under the
# core-owned ui/cards/<id>/dist/index.html. No module names appear here (AD-2).
# get_card sets _meta.ui.resourceUri to the chosen template's widget_uri.
# Story 9.10 (AC1): no mime_type is passed on any ui:// resource below, so they
# serve the MCP Apps profile mime (fastmcp.apps.UI_MIME_TYPE). Forcing
# mime_type="text/html" would drop the profile the hosts key off.
# ---------------------------------------------------------------------------


def card_kpi_widget() -> str:
    """Serve the KPI card single-file widget HTML (Story 9.2 reference card).

    Returns the built single-file bundle (ui/cards/kpi/dist/index.html). When the
    card has not been built yet, returns a graceful placeholder rather than raising
    so the server never crashes at startup or on read (mirrors daily_report_widget).
    """
    from core import cards as _cards  # noqa: PLC0415

    path = _cards.resolve_card_widget_dist("kpi")
    if not path.exists():
        logging.warning(
            "KPI card widget not built: %s -- run the ui/cards/kpi build first", path
        )
        return (
            "<!doctype html><html><body>"
            "Card not built. Build the KPI card bundle first."
            "</body></html>"
        )
    return path.read_text(encoding="utf-8")


def _serve_card_widget(card_id: str, mcp_uri: str) -> str:
    """Serve a business-card single-file widget HTML by card id (Stories 9.3-9.6).

    Source-agnostic (AD-2): the card id is data; the dist path is resolved by
    core.cards.resolve_card_widget_dist. When the card has not been built yet, returns
    a graceful placeholder rather than raising so the server never crashes at startup or
    on read (mirrors daily_report_widget / card_kpi_widget). The follow-up UI agent
    builds the bundles under ui/cards/<id>/dist/index.html.
    """
    from core import cards as _cards  # noqa: PLC0415

    path = _cards.resolve_card_widget_dist(card_id)
    if not path.exists():
        logging.warning(
            "%s card widget not built: %s -- run the ui/cards/%s build first",
            card_id,
            path,
            card_id,
        )
        return (
            "<!doctype html><html><body>"
            f"Card not built. Build the {card_id} card bundle first."
            "</body></html>"
        )
    return path.read_text(encoding="utf-8")


def card_keywords_widget() -> str:
    """Serve the Keywords card single-file widget HTML (Story 9.3)."""
    return _serve_card_widget("keywords", "ui://core/card-keywords")


def card_conversions_widget() -> str:
    """Serve the Conversions card single-file widget HTML (Story 9.4)."""
    return _serve_card_widget("conversions", "ui://core/card-conversions")


def card_usertypes_widget() -> str:
    """Serve the User types card single-file widget HTML (Story 9.5)."""
    return _serve_card_widget("usertypes", "ui://core/card-usertypes")


def card_journey_widget() -> str:
    """Serve the User journey / funnel card single-file widget HTML (Story 9.6)."""
    return _serve_card_widget("journey", "ui://core/card-journey")


def card_connectors_widget() -> str:
    """Serve the Connecteurs context card single-file widget HTML (Story 9.8)."""
    return _serve_card_widget("connectors", "ui://core/card-connectors")


def card_attribution_widget() -> str:
    """Serve the Attribution card single-file widget HTML (Story 16.3)."""
    return _serve_card_widget("attribution", "ui://core/card-attribution")


def card_dedup_widget() -> str:
    """Serve the Deduplication card single-file widget HTML (Story 17.3)."""
    return _serve_card_widget("dedup", "ui://core/card-dedup")


# Story 48.1: ONE generic capability-impact app, shared by EVERY capability --
# it reads `capability_key` from the payload and declares no key of its own, which
# is why the sixth (story 61.5) reached it without a line of change here.
# It visualizes the same bounded payload the Console impact matrix renders and
# performs no mutation and no authorization of its own.
def project_capability_impact_app() -> str:
    """Serve the shared Project capability impact review app (single file HTML)."""
    path = (
        _WIDGETS_DIR / "project-capability-impact" / "dist" / "index.html"
    )
    if not path.exists():
        logging.warning(
            "Capability impact app not built: %s -- run its ui build first", path
        )
        return (
            "<!doctype html><html><body>"
            "<p>The capability impact app is not built. "
            "Open Project settings in the console to review this change.</p>"
            "</body></html>"
        )
    return path.read_text(encoding="utf-8")


# ONE URI, ONE LINE, ONE PLACE. The table is the registration: a resource added
# to this module without an entry here is not served, and an entry pointing at a
# URI already bound raises at import — which is the property the decorators
# scattered through the entrypoint could not offer.
_RESOURCES: tuple[tuple[str, object], ...] = (
    (DAILY_REPORT_WIDGET_URI, daily_report_widget),
    ("ui://core/card-kpi", card_kpi_widget),
    ("ui://core/card-keywords", card_keywords_widget),
    ("ui://core/card-conversions", card_conversions_widget),
    ("ui://core/card-usertypes", card_usertypes_widget),
    ("ui://core/card-journey", card_journey_widget),
    ("ui://core/card-connectors", card_connectors_widget),
    ("ui://core/card-attribution", card_attribution_widget),
    ("ui://core/card-dedup", card_dedup_widget),
    ("ui://core/project-capability-impact", project_capability_impact_app),
)


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind every core-owned ``ui://core/*`` resource on the given FastMCP app."""
    for uri, fn in _RESOURCES:
        mcp.resource(uri)(fn)
