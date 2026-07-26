"""toorow MCP server entrypoint.

Instantiates the FastMCP app, mounts auto-discovered modules (AD-2), registers the
core-owned tools (``health``, ``list_modules``, ``get_daily_report``,
``add_context_event``) and the daily-report widget resource, and serves over
streamable HTTP (``build_asgi_app`` -> uvicorn), binding ``0.0.0.0:$PORT``.

Since Story 5.1 (AI-27 decomposition) the heavier concerns live in dedicated
modules and are re-exported here for backward compatibility:
  * envelope health enrichment  -> core.health_enrichment
  * context-event helpers       -> core.context_events
  * conversions dedup + priority -> core.metrics
  * completeness confidence      -> core.confidence
  * ASGI routing + Host guard    -> core.routing
  * OTel/Langfuse tracing        -> core.tracing (universal TracingMiddleware)

The Host-header 421 guard and the /admin + /api dispatcher now live in
``core.routing``; see that module and ``server/core/README.md`` for the
``HOST_HEADER_VALIDATION`` / ``FASTMCP_HTTP_ALLOWED_HOSTS`` details.

Run locally:
    PORT=8000 uv run python -m core.main   (or: make dev)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core import branding as branding_module
from core import confidence as confidence_module
from core import context_events as context_events_module
from core import envelope as envelope_builder
from core import health_enrichment, narrative, routing, summarizer, tracing, warehouse
from core import metrics as metrics_module
from core import rollup as rollup_module
from core.auth_config import build_auth_provider
from core.constants import AUTH_EXPIRED_CODE
from core.loader import scan_and_load_modules

# ---------------------------------------------------------------------------
# AD-1 canonical structuredContent envelope.
# Every data-returning tool wraps its payload in this envelope so the shared
# ui/shell can consume `meta` (freshness, provenance, alerts) uniformly.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"  # review-1-5 F-02: single canonical value across all tools


def _envelope(data: dict, *, provenance=None, freshness: str | None = None, alerts=None) -> dict:
    """Build the canonical AD-1 structuredContent envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "freshness": freshness,
            "provenance": provenance,
            "alerts": alerts or [],
        },
        "data": data,
    }


def _error(code: str, message: str, provenance=None) -> dict:
    """Canonical MCP error shape (ARCHITECTURE-SPINE §Consistency / Error shape).

    Tools raise this (via ToolError) so the wire response carries ``isError: true``
    together with ``{code, message, provenance}``.
    """
    return {"code": code, "message": message, "provenance": provenance}


# ---------------------------------------------------------------------------
# The MCP app. Named "connector"; mount-ready for modules (Story 1.3, AD-2).
# ---------------------------------------------------------------------------
_auth = build_auth_provider()
mcp = FastMCP("toorow", auth=_auth)

# ---------------------------------------------------------------------------
# Story 5.1 (AC2, AC3, T3): universal tool-call tracing middleware.
# Registered on the FastMCP app so EVERY tool call -- core AND module-namespaced
# -- produces a root span (tool.name / sanitised params / latency). No-op & never
# raises when TRACING_ENABLED=false or the OTel SDK is absent. The tracer provider
# is lazily built on first span (init_tracing) and again explicitly in
# build_asgi_app(); registering the middleware unconditionally is cheap and safe.
# ---------------------------------------------------------------------------
_tracing_mw = tracing.build_middleware()
if _tracing_mw is not None:
    mcp.add_middleware(_tracing_mw)

# ---------------------------------------------------------------------------
# Module auto-discovery (Story 1.3 — T3.1, T3.2).
#
# Scan server/modules/ at import time, validate each manifest, mount each
# valid module's mcp_app under its kebab-case name. The core is source-agnostic
# (AD-2) — no module-specific names appear here.
# ---------------------------------------------------------------------------
_MODULES_DIR = Path(__file__).parent.parent / "modules"
_loaded_modules = scan_and_load_modules(_MODULES_DIR)

for _loaded in _loaded_modules:
    # AD-2: mount under the module's declared name (kebab-case).
    # FastMCP 3.4.4 namespace transform: tool "get_report" becomes
    # "<module-name>_get_report" when namespace="<module-name>".
    mcp.mount(_loaded.connector_module.mcp_app, namespace=_loaded.name)

# Story 27.6: register the metric-semantics MCP tools (read + curation) on the core mcp.
from core.metric_semantics_mcp import register as _register_metric_semantics  # noqa: E402

_register_metric_semantics(mcp)

# Story 36.12: register the sanitized pull-history / diagnosis MCP tools (Insights reads).
from core.datastream_diagnosis import register as _register_datastream_diagnosis  # noqa: E402

_register_datastream_diagnosis(mcp)

# Story 36.13: register the Operations-profile MCP tools (run/readiness/diagnostic reads
# + prepare/confirm bounded recovery). First real consumer of the profile system.
from core.operations_mcp import register as _register_operations_mcp  # noqa: E402

_register_operations_mcp(mcp)

# Story 36.17: register the agentic mapping-proposal MCP tools (Operations profile).
from core.mapping_proposal_mcp import register as _register_mapping_proposal  # noqa: E402

_register_mapping_proposal(mcp)

# Story 36.18: register the Governance-profile MCP tools (review/confirm/rollback publication).
from core.governance_mcp import register as _register_governance_mcp  # noqa: E402

_register_governance_mcp(mcp)

# Story 36.16: register the recovery-proposal Operations tool (bridge from safe diagnosis
# to the 36.13/36.18 prepare-confirm-commit seams; PREPARE only, no direct write).
from core.recovery_mcp import register as _register_recovery_mcp  # noqa: E402

_register_recovery_mcp(mcp)

# Story 36.15: register the read-only starter-report render/reproduce tools (Insights).
from core.first_report_render_mcp import register as _register_first_report_render  # noqa: E402

_register_first_report_render(mcp)

# Story 40.5: register the brand-registry admin MCP tools (Governance profile) -- the LLM
# surface over the 40.1-40.3 stores (create/curate/wire/confirm/approve), fail-closed +
# human-confirmed. Registered BEFORE validate_catalog() so the boot validator sees them.
from core.brand_registry_mcp import register as _register_brand_registry_mcp  # noqa: E402

_register_brand_registry_mcp(mcp)

# Story 22.22: register the adaptation author-and-self-test MCP tool (Operations, READ).
# Ships an LLM-authored .py + a bounded sample to the isolated sandbox worker (AD-3) and
# returns the mapping result; holds no DB write and leaks no credential to the worker.
# Registered BEFORE validate_catalog() so the boot validator sees its declaration.
from core.adaptation_executor_mcp import register as _register_adaptation_executor  # noqa: E402

_register_adaptation_executor(mcp)

# Story 36.11: validate the capability catalog (fail closed at boot on any undeclared
# or contradictory profiled tool) and install the capability-profile middleware so every
# list_tools/call_tool is filtered by the caller's authenticated profile scope. Runs
# AFTER all register() hooks so the profiled registry is fully populated. Undeclared
# legacy tools default to Insights (stay visible); high-risk profiles are opt-in only.
from core.mcp_profiles import build_middleware as _build_capability_middleware  # noqa: E402
from core.mcp_profiles import validate_catalog as _validate_capability_catalog  # noqa: E402

_validate_capability_catalog()
_capability_mw = _build_capability_middleware()
if _capability_mw is not None:
    mcp.add_middleware(_capability_mw)


# ---------------------------------------------------------------------------
# Story 2.7 — AD-2 dispatch helper: pull function registry access.
#
# Core must NEVER import from modules (AD-2). This helper lets admin_api.py
# call a module's pull function via the loader registry, without the core
# importing from any specific module package.
#
# The convention: a module exposes a callable named 'pull' (at P2).
# Epic 3 will formalize this as a required manifest contract.
# ---------------------------------------------------------------------------


def get_loaded_modules() -> list:
    """Public accessor for the loaded-modules registry (AD-2-safe dispatch)."""
    return _loaded_modules


def get_module_pull_fn(module_name: str, profile_id: str | None = None):
    """Resolve legacy default or strict manifest-declared profile dispatch.

    ``profile_id=None`` preserves the pre-12.1 default ``pull`` path. An explicit
    profile resolves only an available capability report's declared callable;
    unknown, unavailable, missing, or non-callable declarations fail closed.
    """
    for loaded in _loaded_modules:
        if loaded.name == module_name:
            if profile_id is not None:
                descriptor = getattr(loaded, "manifest", {}).get(
                    "source_capabilities", {}
                )
                report = next(
                    (
                        item
                        for item in descriptor.get("reports", [])
                        if item.get("id") == profile_id
                    ),
                    None,
                )
                if (
                    report is None
                    or report.get("availability", {}).get("status") != "selectable"
                    or not isinstance(report.get("dispatch"), dict)
                ):
                    return None
                callable_name = report["dispatch"].get("callable")
                if not isinstance(callable_name, str):
                    return None
                profile_fn = getattr(loaded.connector_module, callable_name, None)
                return profile_fn if callable(profile_fn) else None
            fn = getattr(loaded.connector_module, "pull", None)
            if callable(fn):
                return fn
    return None

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
    """Resolve the daily-report widget's built HTML path — source-agnostic (AD-2).

    Precedence:
      1. ``TOOROW_DAILY_REPORT_WIDGET_DIST`` env override (explicit path).
      2. The build artifact of whichever loaded module declares
         ``widget_ref == DAILY_REPORT_WIDGET_URI`` in its manifest — its bundle
         lives at ``ui/widgets/<module-name>/dist/index.html``. Core never names
         a module; it follows the manifest contract.
    """
    override = os.environ.get("TOOROW_DAILY_REPORT_WIDGET_DIST")
    if override:
        return Path(override)
    for loaded in _loaded_modules:
        if loaded.manifest.get("widget_ref") == DAILY_REPORT_WIDGET_URI:
            return _WIDGETS_DIR / loaded.name / "dist" / "index.html"
    # No module claims the URI yet — return a path that simply won't exist,
    # triggering the graceful "not built" placeholder below.
    return _WIDGETS_DIR / "_unresolved" / "dist" / "index.html"


_WIDGET_PATH = _resolve_widget_dist()


# Story 9.10 (AC1): no explicit mime_type on ui:// resources -- FastMCP auto-serves
# them as "text/html;profile=mcp-app" (fastmcp.apps.UI_MIME_TYPE), the MCP Apps
# profile hosts key off. Forcing mime_type="text/html" would drop the profile.
@mcp.resource(DAILY_REPORT_WIDGET_URI)
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
        return (
            "<!doctype html><html><body>"
            "Widget not built. Build the daily-report widget bundle first."
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
# Story 9.10 (AC1): like daily_report_widget above, no mime_type is passed so the
# resources serve the MCP Apps profile mime (fastmcp.apps.UI_MIME_TYPE).
# ---------------------------------------------------------------------------


@mcp.resource("ui://core/card-kpi")
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


@mcp.resource("ui://core/card-keywords")
def card_keywords_widget() -> str:
    """Serve the Keywords card single-file widget HTML (Story 9.3)."""
    return _serve_card_widget("keywords", "ui://core/card-keywords")


@mcp.resource("ui://core/card-conversions")
def card_conversions_widget() -> str:
    """Serve the Conversions card single-file widget HTML (Story 9.4)."""
    return _serve_card_widget("conversions", "ui://core/card-conversions")


@mcp.resource("ui://core/card-usertypes")
def card_usertypes_widget() -> str:
    """Serve the User types card single-file widget HTML (Story 9.5)."""
    return _serve_card_widget("usertypes", "ui://core/card-usertypes")


@mcp.resource("ui://core/card-journey")
def card_journey_widget() -> str:
    """Serve the User journey / funnel card single-file widget HTML (Story 9.6)."""
    return _serve_card_widget("journey", "ui://core/card-journey")


@mcp.resource("ui://core/card-connectors")
def card_connectors_widget() -> str:
    """Serve the Connecteurs context card single-file widget HTML (Story 9.8)."""
    return _serve_card_widget("connectors", "ui://core/card-connectors")


@mcp.resource("ui://core/card-attribution")
def card_attribution_widget() -> str:
    """Serve the Attribution card single-file widget HTML (Story 16.3)."""
    return _serve_card_widget("attribution", "ui://core/card-attribution")


@mcp.resource("ui://core/card-dedup")
def card_dedup_widget() -> str:
    """Serve the Deduplication card single-file widget HTML (Story 17.3)."""
    return _serve_card_widget("dedup", "ui://core/card-dedup")


# ---------------------------------------------------------------------------
# Core-owned cross-module tool: list_modules (AD-2, T3.3).
#
# Cross-connector tools are registered directly on the core mcp (no namespace
# prefix). Only the core may join across modules.
# ---------------------------------------------------------------------------
def _public_profile_summaries(manifest: dict) -> list[dict]:
    """Return sanitized, backward-compatible legacy profile summaries."""

    reports_by_id = {
        report.get("id"): report
        for report in manifest.get("source_capabilities", {}).get("reports", [])
        if isinstance(report, dict)
    }
    summaries = []
    for profile in manifest.get("report_profiles", []):
        if not isinstance(profile, dict):
            continue
        summary = {
            key: profile[key]
            for key in (
                "id",
                "display_name",
                "metrics",
                "dimensions",
                "extraction_path",
                "verification_expected_rows_per_day",
            )
            if key in profile
        }
        extraction = profile.get("extraction_capabilities")
        if isinstance(extraction, dict):
            summary["extraction_capabilities"] = {
                key: extraction[key]
                for key in (
                    "row_limit",
                    "row_limit_min",
                    "row_limit_max",
                    "filters_supported",
                    "regex_filters",
                    "realtime",
                )
                if key in extraction
            }
        capability_report = reports_by_id.get(profile.get("id"))
        if capability_report is not None:
            summary["availability"] = {
                key: capability_report["availability"][key]
                for key in ("status", "reason_code", "follow_up")
                if key in capability_report["availability"]
            }
        summaries.append(summary)
    return summaries


def list_modules(project_id: str = "default") -> dict:
    """List connector modules enabled for the project, with their report profiles.

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    This is a core-owned tool (not namespaced) that surfaces the module
    registry to LLM clients. Story 7.2 (AC5): filters to only the modules
    enabled for the requesting project (default-enabled when no row in
    app.project_modules). No module-specific logic lives here (AD-2).
    """
    from core import db as _core_db  # noqa: PLC0415
    from core.module_enablement import is_module_enabled  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    resolved_project_id = _resolve_project(project_id)

    modules = []
    try:
        with _core_db.get_connection() as _conn:
            for loaded in _loaded_modules:
                manifest = loaded.manifest
                if not is_module_enabled(loaded.name, resolved_project_id, _conn):
                    continue
                modules.append(
                    {
                        "name": loaded.name,
                        "display_name": manifest.get("display_name"),
                        "auth_type": manifest.get("auth_type"),
                        "report_profiles": _public_profile_summaries(manifest),
                    }
                )
    except Exception as _exc:
        # DB unavailable: fall back to all discovered modules (resilience path).
        logger.debug("list_modules: enablement_check_skipped (db unavailable): %s", _exc)
        for loaded in _loaded_modules:
            manifest = loaded.manifest
            modules.append(
                {
                    "name": loaded.name,
                    "display_name": manifest.get("display_name"),
                    "auth_type": manifest.get("auth_type"),
                    "report_profiles": _public_profile_summaries(manifest),
                }
            )

    return _envelope(
        {
            "modules": modules,
            "count": len(modules),
            "project_id": resolved_project_id,
            "identity": identity,
            "capability_catalog": {
                "tool": "get_source_capabilities",
                "endpoint": "/api/source-capabilities",
                "required_scope": ["project_id", "connection_ref_id"],
            },
        },
        provenance={
            "source_system": "connector-core",
            "source_field": "list_modules",
            "pull_id": None,
        },
        freshness="live",
    )


mcp.tool(list_modules)


def get_source_capabilities(
    project_id: str, connection_ref_id: str
) -> ToolResult:
    """Describe valid fields and report combinations for one owned connection."""

    from core import db as _core_db  # noqa: PLC0415
    from core.source_capabilities import (  # noqa: PLC0415
        SourceCapabilitiesNotFound,
        SourceCapabilitiesUnavailable,
        get_scoped_source_capabilities,
    )

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    try:
        with _core_db.get_connection() as conn:
            catalog = get_scoped_source_capabilities(
                project_id=project_id,
                connection_ref_id=connection_ref_id,
                identity=identity,
                loaded_modules=list(_loaded_modules),
                conn=conn,
            )
    except ValueError:
        error = _error(
            "invalid_input", "project_id and connection_ref_id are required"
        )
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(error))],
            structured_content=error,
            is_error=True,
        )
    except SourceCapabilitiesNotFound:
        error = _error("source_capabilities_not_found", "Capability catalog not found")
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(error))],
            structured_content=error,
            is_error=True,
        )
    except SourceCapabilitiesUnavailable:
        error = _error(
            "source_capabilities_unavailable", "Capability catalog is unavailable"
        )
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(error))],
            structured_content=error,
            is_error=True,
        )
    except Exception as exc:  # noqa: BLE001 -- stable public failure contract
        logger.warning("get_source_capabilities: unavailable: %s", type(exc).__name__)
        error = _error(
            "source_capabilities_unavailable", "Capability catalog is unavailable"
        )
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(error))],
            structured_content=error,
            is_error=True,
        )

    reports = catalog["reports"]
    selectable = sum(
        report["availability"]["status"] == "selectable"
        for report in reports
    )
    report_label = "report" if len(reports) == 1 else "reports"
    summary = (
        f"{catalog['module']['display_name']}: {len(reports)} {report_label}, "
        f"{selectable} selectable."
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=catalog,
    )


mcp.tool(get_source_capabilities)


def health(project_id: str = "default") -> dict:
    """Liveness/readiness probe for the connector MCP server.

    AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3). The
    ``project_id`` parameter scaffold is intentional — the identity parameter
    flows through every tool path from P0, it is never bolted on at P6.

    Returns the canonical AD-1 envelope as ``structuredContent``. NOTE:
    FastMCP does NOT synthesize a text channel from this docstring — a dict
    return surfaces as ``structuredContent`` only. That is fine for a
    liveness probe, but data-returning tools (Story 1.5+) MUST explicitly
    return BOTH channels (lean text summary + envelope) — see the
    dual-channel pattern in ``server/core/README.md``. Copying this
    single-channel shape for a data tool would push the full dataset into
    the LLM context and violate NFR1.

    Story 3.3 (AC5): ``data.quota`` is an array of per-platform breaker states,
    one entry per registered platform. Empty array when no modules with quota
    blocks are loaded.

    Story 4.4 (AC9): ``data.mirror_sync`` is the last sync result from
    mirror_sync.py. Null if the process has never synced (just started).
    Shape: {"last_synced_at": str, "lag_seconds": float,
            "tables": {"context_events": N, ...}} or null.
    """
    from core import mirror_sync as _mirror_sync  # noqa: PLC0415
    from core import quota as _quota  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    # AC9: expose last sync result. If never synced, field is null.
    _raw = _mirror_sync._last_sync_result
    if _raw is not None and "error" not in _raw:
        mirror_sync_health = {
            "last_synced_at": _raw.get("synced_at"),
            "lag_seconds": _raw.get("lag_seconds"),
            "tables": _raw.get("synced", {}),
        }
    else:
        mirror_sync_health = None

    return _envelope(
        {
            "status": "ok",
            "project_id": project_id,
            "identity": identity,
            "quota": _quota.all_breaker_states(),
            "mirror_sync": mirror_sync_health,
        },
        provenance={"source_system": "connector-core", "source_field": "health", "pull_id": None},
        freshness="live",
    )


# Register `health` as an MCP tool. Defined as a plain function above (so tests
# and other core code can call it directly) and registered here — @mcp.tool
# would otherwise replace the name with a Tool object.
mcp.tool(health)


# ---------------------------------------------------------------------------
# Core-owned cross-connector tool: get_daily_report (AD-2, Story 1.5).
#
# This is the token-burn split tool: ≤30-line plain-text summary on the LLM
# channel, full dataset envelope on structuredContent (AD-1 / NFR1 / CAP-3).
# Only the core may join across modules — no module logic here (AD-2).
# ---------------------------------------------------------------------------

# ISO-8601 date pattern (YYYY-MM-DD)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---------------------------------------------------------------------------
# AI-27 decomposition (Story 5.1): the conversions dedup rule + declarative
# priority loader now live in core.metrics; health enrichment lives in
# core.health_enrichment; context-event helpers live in core.context_events.
# The names below are re-exported here for backward compatibility with existing
# imports/tests (from core.main import _apply_conversions_dedup, etc.).
# ---------------------------------------------------------------------------
_apply_conversions_dedup = metrics_module.apply_conversions_dedup
_load_metric_source_priorities = metrics_module.load_metric_source_priorities
_METRIC_PRIORITIES = metrics_module._METRIC_PRIORITIES
_enrich_envelope_with_health = health_enrichment.enrich_envelope_with_health


def _resolve_project(project_id: str | None) -> str:
    """Resolve *project_id* against app.projects (Story 7.1, AC5).

    Centralised replacement for the old inline ``project_id or 'default'`` bind.
    Opens a short-lived platform-db connection and delegates to
    ``core.project_resolver.resolve_project_id`` (the single source of the
    fallback + validation logic — no inline 'default' literal lives in tool
    bodies anymore).

    Behaviour:
      - Project missing or archived (DB reachable) -> ToolError propagates
        (AC5 contract: code=project_not_found).
      - DB unreachable -> log at debug and PASS THROUGH the original value so
        the hot path (and DB-less unit tests) stay resilient, mirroring the
        best-effort pattern already used for the briefing fetch above.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core.project_resolver import (  # noqa: PLC0415
        _LEGACY_DEFAULT_SENTINEL,
        resolve_project_id,
    )

    raw = (project_id or "").strip()
    is_default_bind = raw == "" or raw == _LEGACY_DEFAULT_SENTINEL

    try:
        with _core_db.get_connection() as _conn:
            return resolve_project_id(project_id, _conn)
    except ToolError:
        # AC5: an EXPLICIT, named project that is missing/archived must surface
        # the error to the caller. The default auto-bind ('' / 'default') is the
        # resilient path: at P3-dev the seeded 'default' row always exists, so a
        # raise here means the connection is a DB-less unit-test mock that does
        # not model app.projects -> pass the sentinel through rather than fail.
        if is_default_bind:
            return _LEGACY_DEFAULT_SENTINEL
        raise
    except Exception as _exc:  # pragma: no cover - resilience path
        logger.debug("resolve_project: passthrough (db unavailable): %s", _exc)
        return raw or _LEGACY_DEFAULT_SENTINEL


def _validate_date_range(date_range: dict | None) -> str | None:
    """Return an error message string if date_range is invalid, else None."""
    if not isinstance(date_range, dict):
        return "date_range must be a dict with 'start' and 'end' keys"
    start = date_range.get("start")
    end = date_range.get("end")
    if not start or not end:
        return "date_range must contain 'start' and 'end' keys"
    if not _ISO_DATE_RE.match(str(start)):
        return f"date_range.start must be ISO-8601 (YYYY-MM-DD), got: {start!r}"
    if not _ISO_DATE_RE.match(str(end)):
        return f"date_range.end must be ISO-8601 (YYYY-MM-DD), got: {end!r}"
    if str(start) > str(end):
        return f"date_range.start ({start}) must be ≤ date_range.end ({end})"
    return None


# ---------------------------------------------------------------------------
# Story 11.6 (AD-18) — measured pre-query gate, shared by the three data tools.
#
# The gate lives ONLY in surfaces we control (tool descriptions above + tool
# responses here); it is MEASURED, never enforced (a non-adherent query still
# returns a normal envelope). record_data_query writes the adherence verdict to
# a Langfuse trace attribute (best-effort) with a Postgres fallback; the ~1-line
# pointer nudges the agent to search_context when definitions were served (AI-50)
# but context was not consulted. AD-1: the pointer is one line and the summary
# stays <=30 lines (append_pointer_within_cap trims the tail, never the pointer).
# AD-2: source-agnostic — no module strings. Never raises into the tool path.
# ---------------------------------------------------------------------------


def _apply_pre_query_gate(
    summary: str,
    data_tool: str,
    *,
    project_id: str,
    metric_definitions: dict | None,
    envelope: dict | None = None,
) -> str:
    """Record adherence + optionally append the ~1-line search_context pointer.

    Returns the (possibly pointer-augmented) summary and, when *envelope* is given,
    stamps the adherence verdict on ``meta.gate`` (additive key -- observable by the
    widget and Epic 14 without polluting the LLM channel). Best-effort: any failure
    degrades to the original summary (AD-18: measuring adherence must never break a
    data query).
    """
    try:
        from core import adherence as _adherence  # noqa: PLC0415

        trace_id = tracing.current_trace_id_hex() or None
        verdict = _adherence.record_data_query(
            data_tool, project_id=project_id, trace_id=trace_id
        )
        if isinstance(envelope, dict):
            envelope.setdefault("meta", {})["gate"] = {
                "adherent": bool(verdict.get("adherent")),
                "context_tool": verdict.get("context_tool"),
                "session_kind": verdict.get("session_kind"),
            }
        pointer = _adherence.build_context_pointer(
            metric_definitions, adherent=bool(verdict.get("adherent"))
        )
        return _adherence.append_pointer_within_cap(summary, pointer)
    except Exception as _gate_exc:  # noqa: BLE001 -- gate must never break the tool
        logger.debug("%s: pre_query_gate_skipped: %s", data_tool, _gate_exc)
        return summary


def get_daily_report(
    project_id: str = "default",
    date_range: dict = None,
    connectors: list[str] | None = None,
    as_of: str | None = None,
) -> ToolResult:
    """Daily report -- lean LLM summary + full dataset in structuredContent.

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    CONSULT CONTEXT FIRST (AD-18): before interpreting these figures, consult the
    governed context layer -- call ``search_context`` (a metric/concept) or
    ``get_procedure`` (a named playbook). Definitions, procedures and schema docs
    live there. This is a recommendation, not a gate: the query still succeeds if
    you skip it, but adherence is measured per session (Epic 14).

    Returns a <=30-line plain-text summary on the LLM channel and the full
    canonical AD-1 envelope on structuredContent (token-burn split, AD-1 /
    NFR1 / CAP-3). Heavy JSON never enters the LLM context.

    Parameters:
        project_id: Project identifier (default: 'default')
        date_range: Dict with 'start' and 'end' ISO-8601 date strings
        connectors: Connector filter (None = all enabled connectors for project)
        as_of: Optional ISO-8601 datetime string (e.g. "2026-07-08T23:59:59Z").
               When set, returns KPI values as they were known at that timestamp
               (as-of replay — Story 4.6, FR13). When None: current view (unchanged).
    """
    # NFR1 P1 gate: target ~500 tokens LLM channel vs multi-MB widget payload.
    # NFR2: target ~30s latency.
    t0 = time.perf_counter()
    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"
    # Validate pure request inputs before any project or briefing database access.
    # T1.3 — Validate date_range
    validation_error = _validate_date_range(date_range)
    if validation_error:
        err = _error("invalid_date_range", validation_error)
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(err))],
            is_error=True,
        )

    # Story 7.1 (AC5): resolve + validate the project via the shared resolver
    # (replaces the old inline 'default' auto-bind). Archived/missing -> ToolError.
    project_id = _resolve_project(project_id)

    # Story 5.1 (AC2/AC3): the per-tool root span is created by the universal
    # TracingMiddleware (tool.name / sanitised params / latency / traceparent
    # continuation). The P1 gate data (AC7) is attached to that same span below via
    # metrics.log_tool_metrics -> tracing.record_current_span_attributes.

    # Story 6.7 (AC5): fetch today's cached briefing for this project.
    # ONE SELECT from app.morning_briefings — zero warehouse calls on the hot path.
    # PERFORMANCE GUARANTEE: this is the ONLY DB call for the briefing (AC7 / hot-path guarantee).
    _briefing_row: dict | None = None
    try:
        from core.db import get_connection as _get_pg_for_briefing  # noqa: PLC0415

        _today_date = datetime.now(timezone.utc).date().isoformat()
        with _get_pg_for_briefing() as _pg_conn_for_briefing:
            with _pg_conn_for_briefing.cursor() as _cur_briefing:
                _cur_briefing.execute(
                    """
                    SELECT id, insights, built_at
                    FROM app.morning_briefings
                    WHERE project_id = %s AND briefing_date = %s
                    """,
                    (project_id, _today_date),
                )
                _briefing_db_row = _cur_briefing.fetchone()
                if _briefing_db_row is not None:
                    _cols_briefing = [d[0] for d in _cur_briefing.description]
                    _briefing_row = dict(zip(_cols_briefing, _briefing_db_row))
    except Exception as _briefing_exc:
        logger.debug("get_daily_report: briefing_fetch_skipped: %s", _briefing_exc)

    # Story 4.6 (AC3): validate as_of if provided — must be ISO-8601 datetime.
    # as_of_ts holds the normalised string for SQL binding (timezone-aware ISO).
    as_of_ts: str | None = None
    if as_of is not None:
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        try:
            _dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
            as_of_ts = _dt.isoformat()
        except (ValueError, AttributeError):
            raise ToolError(
                json.dumps(
                    {
                        "code": "invalid_input",
                        "message": "as_of doit être un datetime ISO-8601 valide"
                        f" (ex: 2026-07-08T23:59:59Z), reçu: {as_of!r}",
                    }
                )
            )

    # Resolve connectors: None = all enabled modules for this project (Story 7.2, AC4).
    # Filter by module enablement so disabled modules are excluded from the daily report.
    effective_connectors: list[str] | None = connectors
    if effective_connectors is None:
        from core import db as _core_db_dr  # noqa: PLC0415
        from core.module_enablement import is_module_enabled as _is_mod_enabled  # noqa: PLC0415

        try:
            with _core_db_dr.get_connection() as _conn_dr:
                effective_connectors = [
                    m.name
                    for m in _loaded_modules
                    if _is_mod_enabled(m.name, project_id, _conn_dr)
                ] or None
        except Exception as _dr_exc:
            logger.debug(
                "get_daily_report: module_enablement_filter_skipped: %s", _dr_exc
            )
            effective_connectors = [m.name for m in _loaded_modules] or None

    # T2 — Query warehouse (Story 4.6: route to as-of path when as_of is set)
    try:
        if as_of_ts is not None:
            # Story 4.6 (AC3, AC4): as-of replay path.
            # Queries fact_daily_kpi_all_pulls with loaded_at <= as_of_ts.
            # Health enrichment is still applied below (connection state is current,
            # not as-of; we note this in the meta.as_of field).
            rows = warehouse.get_daily_report_asof(
                project_id,
                date_range["start"],
                date_range["end"],
                effective_connectors,
                as_of_ts,
            )
        else:
            rows = warehouse.query_daily_report(
                project_id,
                date_range["start"],
                date_range["end"],
                effective_connectors,
            )
    except Exception as exc:
        err = _error("warehouse_query_error", f"Warehouse query failed: {exc}")
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(err))],
            is_error=True,
        )

    # AD-4-conversions: dedup rule applied — see dbt/models/marts/cross_source_conversions.sql
    # Rule P: when multiple connectors are in scope, apply priority-source dedup for conversions
    # so the cross-source total is never a raw sum of overlapping source values (double-count).
    # Priority order is declarative: dbt/seeds/metric_source_priority.csv (AD-2 compliant).
    # Single-connector queries are unaffected (per-source values read directly from fact_daily_kpi).
    if effective_connectors is None or len(effective_connectors) != 1:
        rows = _apply_conversions_dedup(rows)

    # T3 — Fetch context events for the reporting window (AC7, Story 4.3).
    # Returns [] gracefully when Postgres is unavailable (no crash on missing DB).
    # AD-2: _fetch_context_events is generic; no module-specific strings.
    context_events = _fetch_context_events(
        project_id, date_range["start"], date_range["end"]
    )

    # T3 — Build LLM summary (≤30 lines, from rollup aggregates — never raw rows)
    # cost values from fact_daily_kpi are always in canonical_currency (EUR by default).
    # AD-6: normalization happened once, at dbt staging. No conversion here.
    connector_list = effective_connectors or []

    # G-06 fix: query_daily_report now fetches current + prior window rows so that
    # rollup._split_periods can compute delta/delta_pct. Split here so the rollup
    # gets the full set (for delta), while envelope/summary only see current rows.
    _current_rows, _prior_rows = rollup_module._split_periods(
        rows, date_range["start"], date_range["end"]
    )
    # Use current_rows everywhere data.rows / summary tables are built (below),
    # but pass all rows to compute_rollup so _split_periods inside can find prior.
    # Note: compute_rollup calls _split_periods internally, so passing all rows
    # gives it both buckets; replace `rows` with current_rows for envelope data.
    rows = _current_rows

    # Story 6.4 (AC9): compute the canonical rollup dict (totals + deltas vs the
    # previous period, with provenance) via the shared rollup helper. This is the
    # input to the deterministic what+why narrative builder (AD-1). Metrics and
    # pull_ids are derived from the returned rows (source-agnostic, AD-2).
    _rollup_rows = _current_rows + _prior_rows
    rollup_metrics = sorted({r.get("metric") for r in _rollup_rows if r.get("metric")})
    rollup_pull_ids = sorted({r.get("pull_id") for r in _rollup_rows if r.get("pull_id")})
    rollup = rollup_module.compute_rollup(
        _current_rows + _prior_rows,
        rollup_metrics,
        date_range["start"],
        date_range["end"],
        project_id,
        rollup_pull_ids,
    )

    # Story 6.4 (AC5, AC10): the LLM summary is the deterministic what+why narrative
    # with inline citations (build_narrative), replacing the legacy rollup summary.
    # It is assembled from the rollup dict + context events only — never raw rows
    # (AD-1). Alert firings are appended to the text channel further below (the
    # existing business/anomaly append path), so alerts=[] is passed here.
    # When there are no rows (mart not populated), fall back to the legacy empty-state
    # summarizer so the empty-state French messaging + as-of caveat are preserved.
    if rows:
        summary = narrative.build_narrative(
            project_id=project_id,
            report_id=None,
            rollup=rollup,
            context_events=context_events,
            alerts=[],
            as_of=as_of_ts,
            narrative_prompt=None,
        )
    else:
        summary = summarizer.build_daily_report_summary(
            rows, date_range, connector_list, project_id,
            context_events=context_events, as_of=as_of_ts,
        )

    # T4 — Build canonical envelope (structuredContent channel)
    meta_freshness, meta_provenance = envelope_builder.derive_meta_from_rows(
        rows, connector_list
    )
    envelope = envelope_builder.build_canonical_envelope(
        rows=rows,
        meta_freshness=meta_freshness,
        meta_provenance=meta_provenance,
        date_range=date_range,
        connectors=connector_list,
        report_profile="standard_daily",
        # Story 23.1 (AC1): org branding of the project's organization, additive
        # meta key (AI-31). None (no org / no colors / DB error) -> key omitted,
        # the widget shell renders the default toorow theme.
        branding=branding_module.resolve_org_branding(project_id),
    )
    # AD-14: inject resolved identity into the envelope data (Story 2.3)
    envelope["data"]["identity"] = identity

    # Story 5.3 (AC7): add recent business threshold alert firings to meta.alerts[].
    # Additive -- appends to existing alerts[] (infra alerts already use this field).
    # Best-effort: DB errors return [] silently (graceful degradation).
    _business_alert_firings: list[dict] = []
    try:
        from core import business_alerts as _business_alerts_module  # noqa: PLC0415
        from core.db import get_connection as _get_pg_conn  # noqa: PLC0415

        with _get_pg_conn() as _pg_conn_for_alerts:
            _business_alert_firings = _business_alerts_module.fetch_recent_alert_firings(
                project_id, _pg_conn_for_alerts, hours=24
            )
            # AI-32 (Story 6.1): meta_alert scheduler-health rows share the alert
            # delivery path -- surface them in meta.alerts[] the next morning.
            _business_alert_firings = _business_alert_firings + (
                _business_alerts_module.fetch_recent_meta_alerts(
                    project_id, _pg_conn_for_alerts, hours=24
                )
            )
    except Exception as _ba_exc:
        logger.debug(
            "get_daily_report: business_alerts_fetch_skipped: %s", _ba_exc
        )

    # Story 5.4 (AC8): add recent anomaly firings to meta.alerts[].
    # type='anomaly' rows from app.alert_firings, fetched alongside business threshold rows.
    # The fetch_recent_alert_firings in business_alerts filters type='business_threshold';
    # anomaly firings are fetched separately via anomaly_alerts.fetch_recent_anomaly_firings.
    # Both types surface in meta.alerts[] (AC8: both channels wired).
    _anomaly_firings: list[dict] = []
    try:
        from core import anomaly_alerts as _anomaly_alerts_module  # noqa: PLC0415
        from core.db import get_connection as _get_pg_conn_anon  # noqa: PLC0415

        with _get_pg_conn_anon() as _pg_conn_for_anomalies:
            _anomaly_firings = _anomaly_alerts_module.fetch_recent_anomaly_firings(
                project_id, _pg_conn_for_anomalies, hours=24
            )
    except Exception as _an_exc:
        logger.debug(
            "get_daily_report: anomaly_alerts_fetch_skipped: %s", _an_exc
        )

    # Story 22.6 (review F-2): media-plan pacing firings surface in the SAME live
    # meta.alerts[] channel as business/anomaly ones -- not only in the briefing.
    _mediaplan_firings: list[dict] = []
    try:
        from core import mediaplan_alerts as _mediaplan_alerts_module  # noqa: PLC0415
        from core.db import get_connection as _get_pg_conn_mp  # noqa: PLC0415

        with _get_pg_conn_mp() as _pg_conn_for_mediaplan:
            _mediaplan_firings = _mediaplan_alerts_module.fetch_recent_mediaplan_firings(
                project_id, _pg_conn_for_mediaplan, hours=24
            )
    except Exception as _mp_exc:
        logger.debug(
            "get_daily_report: mediaplan_alerts_fetch_skipped: %s", _mp_exc
        )

    # Append ALL alert types to existing meta.alerts[] (created by envelope builder).
    _all_alert_firings = _business_alert_firings + _anomaly_firings + _mediaplan_firings
    if _all_alert_firings:
        existing_alerts = envelope.get("meta", {}).get("alerts", [])
        envelope.setdefault("meta", {})["alerts"] = existing_alerts + _all_alert_firings

    # Story 4.3 (AC7): add context_events to meta as an additive key.
    # Story 31.5: extended with platform/source/value (MMM regressors) + category/default_marker
    # (dim_event_type join via enrich_events_with_dim) so the overlay widget can style markers
    # by type and filter by category/platform. Additive — schema_version stays "1".
    from core.report_dictionary import enrich_events_with_dim as _enrich  # noqa: PLC0415
    _enriched_events = _enrich(context_events)
    envelope.setdefault("meta", {})["context_events"] = [
        {
            "id": e.get("id"),
            "event_date": e.get("event_date"),
            "type": e.get("type"),
            "label": e.get("label"),
            "platform": e.get("platform"),
            "source": e.get("source"),
            "value": e.get("value"),
            "category": e.get("category", ""),
            "default_marker": e.get("default_marker", "pin"),
        }
        for e in _enriched_events
    ]

    # Story 4.6 (AC5): add meta.as_of when as-of replay is active.
    # Null when not set (current view) — explicit null so widget can test for it.
    envelope.setdefault("meta", {})["as_of"] = as_of_ts

    # Story 5.5 (AC2): expose current OTel trace_id in meta so the widget can
    # echo it back in submit_feedback (AD-13 — Postgres audit + Langfuse score on
    # the same trace). When TRACING_ENABLED=false, current_trace_id_hex() returns
    # None — meta carries "trace_id": null so feedback still works (trace row is
    # written with trace_id=NULL; Langfuse score is skipped). Additive key.
    envelope.setdefault("meta", {})["trace_id"] = tracing.current_trace_id_hex() or None

    # Story 7.1 (AC7): echo the resolved project_id in meta so the widget shell
    # can scope callServerTool (e.g. submit_feedback) to the correct project.
    # Additive key (AI-31 policy).
    envelope.setdefault("meta", {})["project_id"] = project_id

    # Story 2.5 (AC5, AC6, AC7, AC8): enrich envelope with connection health.
    # No-op when DB is unreachable or no connection_ref exists (Epic 1 backward compat).
    envelope = _enrich_envelope_with_health(envelope, project_id)

    # Story 3.5 (AC8) + Story 4.2 (AC8 / AI-21 fix): inject completeness confidence
    # from pull_verifications, scoped per connection_ref_id for the requested connectors.
    # AI-27 (Story 5.1): the lookup now lives in core.confidence (behaviour-preserving).
    # Best-effort: DB errors silently produce confidence=None (no confidence key in meta).
    confidence = confidence_module.compute_confidence(
        project_id,
        effective_connectors,
        rows=rows,
        date_to=date_range.get("end"),
    )
    if confidence is not None:
        envelope.setdefault("meta", {})["confidence"] = confidence

    # Story 6.7 (AC5, AC6): inject briefing into summary and meta.
    # If a cached briefing exists for today: prepend "Briefing matinal" section (≤5 lines)
    # to the summary, and set meta.briefing. Otherwise meta.briefing = null.
    _briefing_insights: list[dict] = []
    _briefing_meta: dict | None = None

    if _briefing_row is not None:
        _raw_insights = _briefing_row.get("insights") or {}
        _briefing_insights = (
            _raw_insights.get("insights", []) if isinstance(_raw_insights, dict) else []
        )
        _briefing_date_str = (
            _raw_insights.get("briefing_date", _today_date)
            if isinstance(_raw_insights, dict)
            else _today_date
        )
        _built_at = _briefing_row.get("built_at")
        _built_at_str = (
            _built_at.isoformat() if hasattr(_built_at, "isoformat") else str(_built_at or "")
        )

        # Build briefing summary header (AC5: ≤5 lines = 1 header + top 3 insights + 1 blank)
        _icons = {"business_alert": "⚠", "anomaly": "⚠", "notable_delta": "✓"}
        _brief_lines = [f"[Briefing matinal — {_briefing_date_str}]"]
        for _ins in _briefing_insights[:3]:
            _icon = _icons.get(_ins.get("type", ""), "•")
            _headline = _ins.get("headline", "")
            _citation = _ins.get("citation", "")
            _brief_lines.append(f"{_icon} {_headline} {_citation}")
        _brief_lines.append("")  # blank separator

        # Prepend briefing section to summary (briefing uses ≤5 lines of the 30-line cap)
        _brief_section = "\n".join(_brief_lines)
        _combined = _brief_section + "\n" + summary
        # NFR1 re-enforcement: prepend may push total past 30 lines. Trim the
        # summary tail (not the briefing block) to keep the cap. The briefing
        # header occupies the first len(_brief_lines) lines; everything after that
        # is the former summary -- truncate it so total stays <= 30 lines.
        # _MAX_LINES is defined below at the alert-append section; use literal 30 here.
        _nfr1_cap = 30
        _combined_lines = _combined.split("\n")
        if len(_combined_lines) > _nfr1_cap:
            _brief_line_count = len(_brief_lines)
            # Reserve one line for the truncation marker so the total stays at the cap.
            _allowed_summary_lines = _nfr1_cap - _brief_line_count - 1
            if _allowed_summary_lines > 0:
                _combined_lines = (
                    _combined_lines[:_brief_line_count]
                    + _combined_lines[
                        _brief_line_count : _brief_line_count + _allowed_summary_lines
                    ]
                    + ["[tronque]"]
                )
            else:
                _combined_lines = _combined_lines[:_nfr1_cap]
        summary = "\n".join(_combined_lines)

        # Build meta.briefing (AC6)
        _briefing_meta = {
            "briefing_date": _briefing_date_str,
            "insights": _briefing_insights,
            "built_at": _built_at_str,
        }

    # meta.briefing = null when no briefing (AC6: explicit null for backward compat)
    envelope.setdefault("meta", {})["briefing"] = _briefing_meta

    # Story 2.5 AC8(a): if auth_expired alert was injected, append to LLM summary.
    # This keeps the text channel honest without exposing structured JSON to the LLM.
    _health_alerts = envelope.get("meta", {}).get("alerts", [])
    if any(a.get("code") == AUTH_EXPIRED_CODE for a in _health_alerts):
        summary = (
            summary.rstrip()
            + "\n\nToken de connexion expire -- donnees indisponibles."
        )

    # Story 5.3 (AC7, LLM channel): append one-liner per business alert firing.
    # Story 5.4 (AC8, LLM channel): append one-liner per anomaly firing after business alerts.
    # NFR1: enforce ≤30 lines total -- if many firings, truncate to fit.
    _current_lines = summary.count("\n") + 1
    _MAX_LINES = 30

    if _business_alert_firings:
        from core import business_alerts as _ba_llm  # noqa: PLC0415

        for _firing in _business_alert_firings:
            if _current_lines >= _MAX_LINES:
                break
            _alert_line = _ba_llm.format_alert_line(_firing)
            summary = summary.rstrip() + "\n" + _alert_line
            _current_lines += 1

    if _anomaly_firings:
        from core import anomaly_alerts as _an_llm  # noqa: PLC0415

        for _an_firing in _anomaly_firings:
            if _current_lines >= _MAX_LINES:
                break
            _anomaly_line = _an_llm.format_anomaly_line(_an_firing)
            summary = summary.rstrip() + "\n" + _anomaly_line
            _current_lines += 1

    # AI-50: inject metric_definitions (+ llm_commentary_guidelines if present) from
    # the module pack's DEFAULT report doc, exactly as get_card does on the ad-hoc path
    # via cards._fetch_r6_adhoc. Best-effort: no DB / no pack -> omit cleanly.
    # The injected keys go on envelope["data"] so the widget can consume them alongside
    # the rows -- same placement as get_report (story 8.8 R6) and get_card.
    try:
        from core import cards as _cards_module  # noqa: PLC0415

        _dr_metrics = sorted({r.get("metric") for r in rows if r.get("metric")})
        _r6_metric_defs, _r6_guidelines = _cards_module._fetch_r6_adhoc(
            project_id, _dr_metrics, list(_loaded_modules)
        )
        if _r6_metric_defs is not None:
            envelope.setdefault("data", {})["metric_definitions"] = _r6_metric_defs
        if _r6_guidelines is not None:
            envelope.setdefault("data", {})["llm_commentary_guidelines"] = _r6_guidelines
    except Exception as _r6_exc:
        logger.debug("get_daily_report: r6_metric_defs_skipped: %s", _r6_exc)

    # Story 11.6 (AD-18): measure the pre-query gate (never enforce it). Record
    # whether a context call preceded this data query in the same session, and --
    # when definitions are served (AI-50) but context was NOT consulted -- add a
    # ONE-line pointer to search_context (AD-1: ~1 line, never a dump). The <=30-line
    # summary cap is preserved (adherence.append_pointer_within_cap).
    summary = _apply_pre_query_gate(
        summary,
        "get_daily_report",
        project_id=project_id,
        metric_definitions=envelope.get("data", {}).get("metric_definitions"),
        envelope=envelope,
    )

    # T5 — Measure and log metrics (P1 gate evidence)
    # Story 5.1 (AC2/AC7): the same P1 gate figures (summary_token_estimate,
    # payload_bytes, latency_ms) are attached to the tool span so the token-burn
    # split is verifiable per call in Langfuse — no duplicated measurement logic.
    latency_ms = int((time.perf_counter() - t0) * 1000)
    payload_bytes = len(json.dumps(envelope).encode("utf-8"))
    metrics_module.log_tool_metrics(
        "get_daily_report", summary, payload_bytes, latency_ms
    )

    # T4.3 — _meta.ui.resourceUri binding (MCP Apps spec 2026-01-26 / ext-apps 1.7)
    # Story 1.6: real widget registered as a FastMCP resource (see below).
    # Story 9.10 (AC3): this result-level binding is KEPT on all three widget tools
    # (get_daily_report / get_report / get_card) even though get_daily_report also
    # carries the declaration-level AppConfig(resource_uri=...) at registration.
    # Per-result _meta.ui is spec-conformant (same "ui" key as the declaration meta)
    # and is the ONLY way to express get_report/get_card's dynamic LLM template
    # selection, so all three tools keep emitting it uniformly.
    _meta = {"ui": {"resourceUri": DAILY_REPORT_WIDGET_URI}}

    # T6 — Return both channels per AD-1 dual-channel pattern
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
        meta=_meta,
    )


# Story 9.10 (AC3): get_daily_report always renders the SAME widget, so the
# canonical declaration-level MCP Apps binding (AppConfig.resource_uri) rides the
# tool registration; hosts see it in tools/list meta.ui.resourceUri before any
# call. The result-level _meta.ui stays too (rationale at the _meta build above).
mcp.tool(get_daily_report, app=AppConfig(resource_uri=DAILY_REPORT_WIDGET_URI))


# ---------------------------------------------------------------------------
# Core-owned cross-connector tool: get_report (AD-2, Story 6.1, AC3).
#
# Renders a named, expert-designed report from a module's reports/ pack. This is
# a SEPARATE tool from get_daily_report (design decision, Dev Notes): get_report
# serves fixed metric/dimension report definitions with a narrative_prompt, while
# get_daily_report serves ad-hoc multi-connector KPI queries. Keeping them
# separate keeps each tool single-responsibility (AD-2).
#
# The rendering logic lives in core.reports (main.py stays lean — the thin tool
# def here parses args, enriches meta with context_events/alerts, and returns the
# dual-channel ToolResult).
# ---------------------------------------------------------------------------


def _load_project_geographic_posture(project_id: str):
    """Best-effort posture + live coverage for every named-report execution path.

    Story 37.8: the posture carries the client-defined MARKETS (stable id,
    label, member country codes), and ``render_report`` projects them into
    ``data.geography.markets`` / ``data.geography.buckets`` so every tool path
    exposes the market split rather than raw ISO codes. A posture read failure
    degrades to Global + an honest ``unavailable`` coverage, never to a silent
    consolidated answer presented as a market split.
    """

    from core import db as _core_db  # noqa: PLC0415
    from core.geographic_reporting import (  # noqa: PLC0415
        GeographicPosture,
        GeographicReportContext,
        fetch_project_geographic_coverage,
        fetch_project_geographic_posture,
    )

    posture = GeographicPosture()
    coverage: dict[str, object] = {"status": "consolidated", "datastreams": []}
    try:
        with _core_db.get_connection() as conn:
            posture = fetch_project_geographic_posture(project_id, conn)
            try:
                coverage = fetch_project_geographic_coverage(project_id, posture, conn)
            except Exception as exc:
                logger.debug(
                    "report geographic coverage read skipped project=%s: %s", project_id, exc
                )
                coverage = {
                    "status": "unavailable" if posture.mode == "local_markets" else "consolidated",
                    "datastreams": [],
                }
    except Exception as exc:
        logger.debug("report geographic posture read skipped project=%s: %s", project_id, exc)
    return GeographicReportContext(posture=posture, coverage=coverage)

def get_report(
    project_id: str,
    report_id: str,
    date_from: str = "",
    date_to: str = "",
) -> ToolResult:
    """Named expert report -- lean LLM summary + full dataset in structuredContent.

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    CONSULT CONTEXT FIRST (AD-18): before interpreting this report, consult the
    governed context layer -- ``search_context`` (a metric/concept) or
    ``get_procedure`` (a named playbook). Not a gate: the query still succeeds if
    you skip it, but adherence is measured per session (Epic 14).

    Renders a module-shipped report definition ("{module_name}/{report_def_id}")
    from warehouse data alone (FR6). The report's narrative_prompt sets the expert
    domain 'voice' prefixed to the ≤30-line LLM summary (AD-1); the full dataset +
    provenance rides on the canonical envelope for the widget channel.

    Non-additive metrics (declared aggregation_rule in the dbt dictionary) route
    to their semantic view rather than being SUM()'d (AD-4).

    Parameters:
        project_id: Project identifier.
        report_id:  "{module_name}/{report_def_id}".
        date_from:  ISO-8601 start; empty -> today - date_window.default_days.
        date_to:    ISO-8601 end; empty -> today.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import reports as reports_module  # noqa: PLC0415
    from core.module_enablement import is_module_enabled  # noqa: PLC0415

    t0 = time.perf_counter()
    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    # Story 7.1 (AC5): resolve + validate project via the shared resolver.
    project_id = _resolve_project(project_id)

    # Story 7.2 (AC4): check module enablement before rendering.
    # report_id format: "{module_name}/{report_def_id}"
    _report_module_name = report_id.split("/")[0] if "/" in report_id else None
    if _report_module_name:
        try:
            with _core_db.get_connection() as _conn_mod:
                if not is_module_enabled(_report_module_name, project_id, _conn_mod):
                    raise ToolError(
                        json.dumps(
                            {
                                "code": "module_disabled",
                                "message": (
                                    f"Module {_report_module_name} is not enabled"
                                    " for this project."
                                ),
                            }
                        )
                    )
        except ToolError:
            raise
        except Exception as _me_exc:
            logger.debug("get_report: module_enablement_check_skipped: %s", _me_exc)

    trace_id = tracing.current_trace_id_hex() or None

    # R6 (Epic 8): resolve the merged report doc (base pack + project override)
    # to extract metric_definitions (widget tooltips/direction-tint) and
    # llm_commentary_guidelines (narrative grounding). Best-effort: DB failure
    # degrades gracefully (no definitions — widget and narrative unchanged).
    _r6_metric_defs: dict | None = None
    _r6_guidelines: str | None = None
    _merged: dict | None = None  # Story 9.8: also carries the preferred card_template.
    try:
        from core import flows as _flows_module  # noqa: PLC0415

        _base_doc = _flows_module._base_report_doc(report_id, _loaded_modules)
        with _core_db.get_connection() as _conn_r6:
            _override = _flows_module._fetch_report_override(project_id, report_id, _conn_r6)
        _merged = _flows_module._merge_report(_base_doc, _override, report_id, project_id)
        _r6_metric_defs = _merged.get("metric_definitions") or None
        _r6_guidelines = (_merged.get("llm_commentary_guidelines") or "").strip() or None
    except Exception as _r6_exc:
        logger.debug("get_report: r6_override_fetch_skipped: %s", _r6_exc)

    try:
        summary, envelope, widget_uri = reports_module.render_report(
            _loaded_modules,
            project_id,
            report_id,
            date_from,
            date_to,
            trace_id=trace_id,
            metric_definitions=_r6_metric_defs,
            llm_commentary_guidelines=_r6_guidelines,
            geographic_posture=_load_project_geographic_posture(project_id),
        )
    except reports_module.ReportNotFound:
        raise ToolError(
            json.dumps(
                {"code": "not_found", "message": f"Report not found: {report_id}"}
            )
        )
    except Exception as exc:
        raise ToolError(
            json.dumps(
                {"code": "warehouse_query_error", "message": f"Report render failed: {exc}"}
            )
        )

    # AD-14: inject resolved identity into the envelope data.
    envelope["data"]["identity"] = identity

    # Enrich meta.context_events for the report window (best-effort; [] on DB down).
    # Story 31.5: expose platform/source/value + dim_event_type join (category/default_marker).
    start = envelope["data"]["date_range"]["start"]
    end = envelope["data"]["date_range"]["end"]
    context_events = _fetch_context_events(project_id, start, end)
    from core.report_dictionary import enrich_events_with_dim as _enrich2  # noqa: PLC0415
    _enriched2 = _enrich2(context_events)
    envelope.setdefault("meta", {})["context_events"] = [
        {
            "id": e.get("id"),
            "event_date": e.get("event_date"),
            "type": e.get("type"),
            "label": e.get("label"),
            "platform": e.get("platform"),
            "source": e.get("source"),
            "value": e.get("value"),
            "category": e.get("category", ""),
            "default_marker": e.get("default_marker", "pin"),
        }
        for e in _enriched2
    ]

    # Append recent alert firings (business + anomaly + meta_alert) to meta.alerts[].
    # Same graceful-degradation pattern as get_daily_report (best-effort).
    _firings: list[dict] = []
    try:
        from core import business_alerts as _ba  # noqa: PLC0415
        from core.db import get_connection as _pg  # noqa: PLC0415

        with _pg() as _conn:
            _firings = _ba.fetch_recent_alert_firings(project_id, _conn, hours=24)
            _firings = _firings + _ba.fetch_recent_meta_alerts(project_id, _conn, hours=24)
    except Exception as _exc:  # noqa: BLE001
        logger.debug("get_report: alerts_fetch_skipped: %s", _exc)
    if _firings:
        existing = envelope.get("meta", {}).get("alerts", [])
        envelope.setdefault("meta", {})["alerts"] = existing + _firings

    # Story 9.8: honor the flow.report preferred card_template. When the merged report
    # doc pins a card_template, serve that card (its widget + card-shaped envelope) via
    # the SAME card path get_card uses. Best-effort: any failure keeps the default report
    # widget/envelope (never breaks the report). An unknown template id is handled inside
    # cards.get_card (structured warning + falls back to the report's default rendering).
    _card_template = ""
    if isinstance(_merged, dict):
        _card_template = (_merged.get("card_template") or "").strip()
    if _card_template:
        try:
            from core import cards as _cards  # noqa: PLC0415

            if _cards.get_template(_card_template) is not None:
                c_summary, c_envelope, c_widget_uri = _cards.get_card(
                    _loaded_modules,
                    project_id,
                    template=_card_template,
                    report_ref=report_id,
                    date_from=date_from,
                    date_to=date_to,
                    identity=identity,
                    context_events=context_events,
                    alerts=_firings,
                    trace_id=trace_id,
                )
                c_envelope["data"]["identity"] = identity
                summary, envelope, widget_uri = c_summary, c_envelope, c_widget_uri
            else:
                logger.warning(
                    "get_report: flow_card_template_ignored report=%s unknown_template=%s",
                    report_id,
                    _card_template,
                )
        except Exception as _card_exc:  # noqa: BLE001 -- never break the report
            logger.warning(
                "get_report: card_template_render_failed report=%s template=%s: %s",
                report_id,
                _card_template,
                _card_exc,
            )

    # Story 11.6 (AD-18): measure the pre-query gate + optional search_context
    # pointer (never enforce). metric_definitions on the envelope come from the R6
    # override (AI-50); when present and context was not consulted, the ~1-line
    # pointer is appended within the <=30-line cap.
    summary = _apply_pre_query_gate(
        summary,
        "get_report",
        project_id=project_id,
        metric_definitions=envelope.get("data", {}).get("metric_definitions"),
        envelope=envelope,
    )

    # P1 gate metrics (token-burn split evidence).
    latency_ms = int((time.perf_counter() - t0) * 1000)
    payload_bytes = len(json.dumps(envelope).encode("utf-8"))
    metrics_module.log_tool_metrics("get_report", summary, payload_bytes, latency_ms)

    # Story 13.5 volet (a) -- persister le snapshot de l'envelope gelee.
    # Best-effort : ne JAMAIS casser le rendu si la persistance echoue.
    # AD-9 : l'envelope est stockee telle quelle (fraicheur/provenance intactes).
    try:
        from core import db as _snap_db  # noqa: PLC0415
        from core.snapshots import persist_render_envelope as _persist_snap  # noqa: PLC0415

        with _snap_db.get_connection() as _snap_conn:
            _persist_snap(
                project_id=project_id,
                tool_name="get_report",
                envelope=envelope,
                widget_uri=widget_uri,
                summary=summary,
                tool_args={"report_id": report_id, "date_from": date_from, "date_to": date_to},
                identity=identity,
                trace_id=trace_id,
                conn=_snap_conn,
            )
    except Exception as _snap_exc:  # noqa: BLE001
        logger.debug("get_report: snapshot_persist_skipped: %s", _snap_exc)

    # AD-1 dual-channel: lean text + full envelope, bound to the module's widget.
    # Story 9.10 (AC3): widget_uri is DYNAMIC here (module report widget or a
    # flow-pinned card template), so the binding can only ride the result-level
    # _meta.ui -- no declaration-level AppConfig (rationale in get_daily_report).
    _meta = {"ui": {"resourceUri": widget_uri}}
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
        meta=_meta,
    )


mcp.tool(get_report)


# ---------------------------------------------------------------------------
# Card library MCP tools (Story 9.1, Epic 9): list_card_templates + get_card.
#
# These are the LLM-agent channel of the card selection contract; core.cards_api
# mirrors them over REST calling the SAME core.cards functions. The tool bodies here
# resolve identity/project (AD-14 / AD-5), enrich meta with context_events/alerts, and
# return the dual-channel ToolResult (AD-1). All selection + render logic lives in
# core.cards -- main.py stays lean.
# ---------------------------------------------------------------------------


_MAX_TEMPLATE_LINES = 28


def list_card_templates() -> ToolResult:
    """Discoverable card catalog -- the LLM reads this to CHOOSE a template.

    Returns the AD-1 envelope whose ``data.templates`` lists every registered card
    (id, the FR question it answers, required canonical inputs, fallback_rank). The
    <=30-line summary enumerates them so Claude can pick the best card for the user's
    question and then call ``get_card(template=<id>)``.
    """
    from core import cards as _cards  # noqa: PLC0415

    templates = _cards.list_templates()
    envelope = _envelope({"templates": templates})

    lines = ["Cartes disponibles (choisissez selon la question) :"]
    for tpl in templates[: _MAX_TEMPLATE_LINES]:
        # Story 9.8: a context card (kind=context) answers an inventory question from
        # app-level sources (no fact metrics). Label it as such -- it is explicit-only
        # (never auto-suggested), so the LLM must request it by id.
        if tpl.get("kind") == _cards.CARD_KIND_CONTEXT:
            lines.append(
                f"- {tpl['id']} : « {tpl['answers_question']} » "
                "[carte contexte, sur demande explicite]"
            )
            continue
        # Humanise the ANY_METRIC "*" sentinel so the LLM reads "any metric" rather than a
        # glob token (review-9-1 F-6). Same phrasing as the empty-list fallback.
        req_names = [
            "(toute metrique)" if m == _cards.ANY_METRIC else m
            for m in tpl["required_metrics"]
        ]
        reqs = ", ".join(req_names or ["(toute metrique)"])
        lines.append(f"- {tpl['id']} : « {tpl['answers_question']} » [requiert : {reqs}]")
    summary = "\n".join(lines[:30])

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


mcp.tool(list_card_templates)


def get_card(
    project_id: str,
    template: str | None = None,
    metrics: list[str] | None = None,
    report_ref: str | None = None,
    date_from: str = "",
    date_to: str = "",
    plan_id: str | None = None,
) -> ToolResult:
    """Resolve a question-answering card -- lean summary + full envelope + widget.

    # AD-14: identity resolved from OAuth 2.1 + PKCE. AD-5: project scoping enforced.

    CONSULT CONTEXT FIRST (AD-18): before interpreting this card, consult the
    governed context layer -- ``search_context`` (a metric/concept) or
    ``get_procedure`` (a named playbook). Not a gate: the card still renders if you
    skip it, but adherence is measured per session (Epic 14).

    Row resolution is source-agnostic (AD-2): pass ``report_ref`` ("{module}/{report}")
    to render a named report as a card, or ``metrics=[...]`` (canonical names) for an
    ad-hoc cross-connector card. When ``template`` is omitted the server SUGGESTS the
    best card whose required inputs are satisfied (highest fallback_rank) and reports
    the choice + alternatives in ``meta.card_selection`` so you can re-request a
    different one. The chosen card's widget is bound via ``_meta.ui.resourceUri``.

    The envelope carries the deterministic cited comment (``data.rendered_comment``,
    AD-9) AND -- when a report override exists -- R6 ``metric_definitions`` +
    ``llm_commentary_guidelines`` so you can deepen the analysis per the user's question.

    Parameters:
        project_id: Project identifier.
        template:   Optional card id ("kpi"). Omitted => server suggestion.
        metrics:    Optional canonical metric names (ad-hoc mode).
        report_ref: Optional "{module_name}/{report_id}" (report-backed mode).
        date_from:  ISO-8601 start; empty => today - 30d.
        date_to:    ISO-8601 end; empty => today.
        plan_id:    Media plan id -- required by the explicit ``mediaplan_pacing``
                    context card (Story 22.5), ignored by every other template.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import cards as _cards  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415
    from core.project_access import identity_has_project_access  # noqa: PLC0415

    t0 = time.perf_counter()
    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id)

    # AD-5: enforce project scope before any warehouse read (best-effort DB open).
    try:
        with _core_db.get_connection() as _conn_scope:
            if not identity_has_project_access(project_id, identity, _conn_scope):
                raise ToolError(
                    json.dumps(
                        {
                            "code": "forbidden",
                            "message": "Acces refuse pour ce projet.",
                        }
                    )
                )
    except ToolError:
        raise
    except Exception as _scope_exc:  # noqa: BLE001 - fail open (resilience path)
        logger.debug("get_card: scope_check_skipped: %s", _scope_exc)

    trace_id = tracing.current_trace_id_hex() or None

    # Context events + alerts for the resolved window (best-effort; [] on DB down).
    start, end = _cards._resolve_window(date_from, date_to)
    context_events = _fetch_context_events(project_id, start, end)
    _alerts: list[dict] = []
    try:
        from core import business_alerts as _ba  # noqa: PLC0415
        from core.db import get_connection as _pg  # noqa: PLC0415

        with _pg() as _conn_a:
            _alerts = _ba.fetch_recent_alert_firings(project_id, _conn_a, hours=24)
            _alerts = _alerts + _ba.fetch_recent_meta_alerts(project_id, _conn_a, hours=24)
    except Exception as _exc:  # noqa: BLE001
        logger.debug("get_card: alerts_fetch_skipped: %s", _exc)

    try:
        summary, envelope, widget_uri = _cards.get_card(
            _loaded_modules,
            project_id,
            template=template,
            metrics=metrics,
            report_ref=report_ref,
            date_from=date_from,
            date_to=date_to,
            plan_id=plan_id,
            identity=identity,
            context_events=context_events,
            alerts=_alerts,
            trace_id=trace_id,
        )
    except _cards.CardTemplateNotFound as exc:
        raise ToolError(
            json.dumps({"code": "not_found", "message": f"Template inconnu : {exc.args[0]}"})
        )
    except _cards.CardTemplateUnsatisfied as exc:
        raise ToolError(
            json.dumps(
                {
                    "code": "unsatisfiable_template",
                    "message": f"Le template '{exc.template_id}' requiert des donnees absentes.",
                    "missing": exc.missing,
                }
            )
        )
    except Exception as exc:
        raise ToolError(
            json.dumps(
                {"code": "warehouse_query_error", "message": f"Card render failed: {exc}"}
            )
        )

    envelope["data"]["identity"] = identity

    # Story 11.6 (AD-18): measure the pre-query gate + optional search_context
    # pointer (never enforce). get_card carries R6 metric_definitions on the
    # envelope (AI-50) when a report override defines them.
    summary = _apply_pre_query_gate(
        summary,
        "get_card",
        project_id=project_id,
        metric_definitions=envelope.get("data", {}).get("metric_definitions"),
        envelope=envelope,
    )

    latency_ms = int((time.perf_counter() - t0) * 1000)
    payload_bytes = len(json.dumps(envelope).encode("utf-8"))
    metrics_module.log_tool_metrics("get_card", summary, payload_bytes, latency_ms)

    # Story 13.5 volet (a) -- persister le snapshot de l'envelope gelee.
    # Best-effort : ne JAMAIS casser le rendu si la persistance echoue.
    # AD-9 : l'envelope est stockee telle quelle (fraicheur/provenance intactes).
    try:
        from core import db as _snap_db_c  # noqa: PLC0415
        from core.snapshots import persist_render_envelope as _persist_snap_c  # noqa: PLC0415

        with _snap_db_c.get_connection() as _snap_conn_c:
            _persist_snap_c(
                project_id=project_id,
                tool_name="get_card",
                envelope=envelope,
                widget_uri=widget_uri,
                summary=summary,
                tool_args={
                    "template": template,
                    "metrics": metrics,
                    "report_ref": report_ref,
                    "date_from": date_from,
                    "date_to": date_to,
                    "plan_id": plan_id,
                },
                identity=identity,
                trace_id=trace_id,
                conn=_snap_conn_c,
            )
    except Exception as _snap_exc_c:  # noqa: BLE001
        logger.debug("get_card: snapshot_persist_skipped: %s", _snap_exc_c)

    # Story 9.10 (AC3): the template (hence widget_uri) is chosen per call, by the
    # LLM or the fallback ranking -- dynamic selection can only ride the
    # result-level _meta.ui (rationale in get_daily_report).
    _meta = {"ui": {"resourceUri": widget_uri}}
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
        meta=_meta,
    )


mcp.tool(get_card)


# ---------------------------------------------------------------------------
# Epic 35 (Story 35.2) — Agentic daily-insight tools: readiness / capability
# discovery / preview / publish. Gate 35.0 decided A (the LLM SELECTS an existing
# card by get_card keys). All logic lives in core.daily_insights_tools (injectable,
# unit-tested); these wrappers only resolve identity/AD-5/tracing and the project's
# real availability + freshness, then delegate. No new render vocabulary (35.1) and
# no free SQL/HTML. Persistence is core.daily_insights (35.3).
# ---------------------------------------------------------------------------


def _daily_insight_scope(project_id: str, *, strict: bool = False) -> tuple[str, str, bool]:
    """Resolve identity + project and enforce AD-5, mirroring get_card.

    Returns ``(identity, project_id, access_ok)`` where ``access_ok`` is True ONLY when the
    project-access check affirmatively passed. Reads pass ``strict=False`` (fail-open on a DB
    hiccup, like get_card) and simply thread the real ``access_ok`` into the validator's
    project_scope gate. The WRITE path passes ``strict=True``: a scope-resolution error denies
    (fail-CLOSED) rather than publishing a durable cross-project artifact on an unverified check.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core.project_access import identity_has_project_access  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"
    project_id = _resolve_project(project_id)
    access_ok = False
    try:
        with _core_db.get_connection() as _conn_scope:
            if identity_has_project_access(project_id, identity, _conn_scope):
                access_ok = True
            else:
                raise ToolError(
                    json.dumps({"code": "forbidden", "message": "Acces refuse pour ce projet."})
                )
    except ToolError:
        raise
    except Exception as _exc:  # noqa: BLE001
        if strict:
            raise ToolError(
                json.dumps(
                    {
                        "code": "forbidden",
                        "message": "Acces projet non verifiable; publication refusee.",
                    }
                )
            )
        logger.debug("daily_insight: scope_check_skipped: %s", _exc)
    return identity, project_id, access_ok


def _resolve_daily_insight_inputs(
    project_id: str, date_from: str, date_to: str
) -> tuple[set[str], set[str], str | None, list[str]]:
    """Project's warehouse-derived availability + data-date freshness + DQ blockers.

    Best-effort: returns empty availability / None freshness on a warehouse hiccup so the
    fail-closed validator refuses rather than the tool crashing (same posture as get_card).
    """
    from core import cards as _cards  # noqa: PLC0415
    from core import warehouse as _wh  # noqa: PLC0415

    available_metrics: set[str] = set()
    available_dimensions: set[str] = set()
    freshness_date: str | None = None
    try:
        rows = _wh.query_daily_report(
            project_id, date_from, date_to, None, include_prior_period=False
        )
        available_metrics, available_dimensions = _cards._available_inputs(rows)
        dates = [str(r.get("date"))[:10] for r in rows if r.get("date")]
        freshness_date = max(dates) if dates else None
    except Exception as _exc:  # noqa: BLE001
        logger.debug("daily_insight: availability_resolve_skipped: %s", _exc)

    dq_blocking: list[str] = []
    try:
        from core import db as _dqdb  # noqa: PLC0415
        from core.dq_api import fetch_dq_report_data  # noqa: PLC0415

        with _dqdb.get_connection() as _conn_dq:
            report = fetch_dq_report_data(project_id, _conn_dq)
        for issue in (report or {}).get("issues", [])[:5]:
            msg = issue.get("message") or issue.get("type")
            if msg:
                dq_blocking.append(str(msg))
    except Exception as _exc:  # noqa: BLE001
        logger.debug("daily_insight: dq_resolve_skipped: %s", _exc)

    return available_metrics, available_dimensions, freshness_date, dq_blocking


def _resolve_evidence_refs(project_id: str, payload: dict) -> set[str]:
    """Return the evidence refs that RESOLVE in server-produced data (never self-certified).

    A published number/claim must be backed by server data (Epic 35 §8 step 5). There is no
    server-side evidence resolver yet, so this returns an EMPTY set: any evidenceRef the agent
    supplies then fails closed (``evidence_unresolved``), forcing refs to stay empty until a
    real resolver (context/report id lookup) lands. This is the honest fail-closed posture --
    it never echoes the agent's own refs back as "resolved".
    """
    # Future: resolve each ref against app.context / report ids for this project.
    return set()


def _render_daily_insight_snapshot(
    project_id: str, identity: str, trace_id: str | None, payload: dict
) -> str | None:
    """Render a validated insight's card and freeze it as a render snapshot; return its id.

    Best-effort: reuses the EXISTING card render (`core.cards.get_card`) + snapshot store
    (`core.snapshots.persist_render_envelope`) so the published insight gains a
    `render_snapshot_id` lineage and becomes viewable ("View card") and shareable (tokenized
    share). A render failure (e.g. empty warehouse) returns None -- the insight is still
    published, just without the frozen render until data exists.
    """
    try:
        from core import cards as _cards  # noqa: PLC0415
        from core import db as _snap_db  # noqa: PLC0415
        from core.snapshots import persist_render_envelope as _persist  # noqa: PLC0415

        card = (payload or {}).get("card", {}) or {}
        period = (payload or {}).get("period", {}) or {}
        summary, envelope, widget_uri = _cards.get_card(
            _loaded_modules,
            project_id,
            template=card.get("template"),
            metrics=card.get("metrics"),
            report_ref=card.get("reportRef"),
            date_from=period.get("dateFrom", ""),
            date_to=period.get("dateTo", ""),
            plan_id=card.get("planId"),
            identity=identity,
            trace_id=trace_id,
        )
        with _snap_db.get_connection() as _snap_conn:
            return _persist(
                project_id=project_id,
                tool_name="get_card",
                envelope=envelope,
                widget_uri=widget_uri,
                summary=summary,
                tool_args={
                    "template": card.get("template"),
                    "metrics": card.get("metrics"),
                    "report_ref": card.get("reportRef"),
                    "date_from": period.get("dateFrom", ""),
                    "date_to": period.get("dateTo", ""),
                    "plan_id": card.get("planId"),
                },
                identity=identity,
                trace_id=trace_id,
                conn=_snap_conn,
            )
    except Exception as _exc:  # noqa: BLE001 - best-effort; publish proceeds without lineage
        logger.debug("daily_insight: render_snapshot_skipped: %s", _exc)
        return None


def get_daily_insight_readiness(project_id: str, insight_date: str) -> ToolResult:
    """Is J-1 data ready to research for ``insight_date``? -- ready | blocked (+ reasons).

    AD-5 scoped. ``blocked`` is a DISTINCT state from "no insight": it means the task must
    STOP (data not ready), never publish stale data as current.
    """
    from datetime import date as _date  # noqa: PLC0415
    from datetime import timedelta as _timedelta  # noqa: PLC0415

    from core import daily_insights_tools as _dit  # noqa: PLC0415

    identity, project_id, _access = _daily_insight_scope(project_id)
    # Look back a week ending at the target to establish the latest available data date.
    try:
        week_start = (_date.fromisoformat(insight_date) - _timedelta(days=7)).isoformat()
    except (ValueError, TypeError):
        week_start = insight_date
    _m, _d, freshness_date, dq_blocking = _resolve_daily_insight_inputs(
        project_id, week_start, insight_date
    )
    result = _dit.readiness(
        insight_date=insight_date, freshness_date=freshness_date, dq_blocking=dq_blocking
    )
    summary = f"Readiness for {insight_date}: {result['status'].upper()}."
    if result["reasons"]:
        summary += " " + " ".join(result["reasons"][:3])
    return ToolResult(content=[TextContent(type="text", text=summary)], structured_content=result)


def get_card_capabilities(project_id: str, date_from: str = "", date_to: str = "") -> ToolResult:
    """What can this project render right now? -- satisfiable cards + available fields (35.1).

    AD-5 scoped. Zero duplicated vocabulary: the catalogue is derived from the card registry.
    """
    from core import cards as _cards  # noqa: PLC0415
    from core import daily_insights_tools as _dit  # noqa: PLC0415

    identity, project_id, _access = _daily_insight_scope(project_id)
    start, end = _cards._resolve_window(date_from, date_to)
    available_metrics, available_dimensions, _fresh, _dq = _resolve_daily_insight_inputs(
        project_id, start, end
    )
    caps = _dit.capabilities(
        available_metrics=available_metrics, available_dimensions=available_dimensions
    )
    n_ok = sum(1 for e in caps["catalog"] if e["satisfiable"])
    summary = f"{n_ok} renderable card(s) for this project; contract v{caps['contractVersion']}."
    return ToolResult(content=[TextContent(type="text", text=summary)], structured_content=caps)


def preview_daily_insight(project_id: str, payload: dict) -> ToolResult:
    """Validate a candidate insight WITHOUT publishing (35.0 fail-closed validator).

    AD-5 scoped. Never persists, never writes a snapshot. Returns {ok, reasonCode?} so the
    agent can fix the spec before calling publish_daily_insights.
    """
    from core import cards as _cards  # noqa: PLC0415
    from core import daily_insights as _store  # noqa: PLC0415
    from core import daily_insights_tools as _dit  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415

    identity, project_id, access_ok = _daily_insight_scope(project_id)
    period = (payload or {}).get("period", {})
    start, end = _cards._resolve_window(period.get("dateFrom", ""), period.get("dateTo", ""))
    available_metrics, available_dimensions, freshness_date, _dq = _resolve_daily_insight_inputs(
        project_id, start, end
    )
    available_templates = {e["id"] for e in _cards.list_templates()}
    existing_slots: set[int] = set()
    _date_to = (payload or {}).get("period", {}).get("dateTo", "")
    try:
        with _core_db.get_connection() as _conn:
            run = _store.get_run(project_id, _date_to, _conn)
        existing_slots = {int(i["slot"]) for i in (run or {}).get("insights", [])}
    except Exception as _exc:  # noqa: BLE001
        logger.debug("preview_daily_insight: existing_slots_skipped: %s", _exc)

    out = _dit.preview(
        payload=payload or {},
        available_metrics=available_metrics,
        available_dimensions=available_dimensions,
        available_templates=available_templates,
        # Evidence must resolve in SERVER data, not be self-certified by the agent's own
        # refs. No server-side evidence resolver exists yet -> pass an empty set so any
        # evidenceRef fails closed (evidence_unresolved); agents omit refs until a resolver lands.
        resolvable_evidence=_resolve_evidence_refs(project_id, payload or {}),
        freshness_date=freshness_date,
        has_project_access=access_ok,
        existing_slots=existing_slots,
    )
    verdict = "OK" if out["ok"] else f"REJECTED ({out['reasonCode']})"
    return ToolResult(
        content=[TextContent(type="text", text=f"Preview {verdict}.")], structured_content=out
    )


def publish_daily_insights(
    project_id: str,
    insight_date: str,
    items: list[dict] | None = None,
    status: str = "published",
) -> ToolResult:
    """Publish 0..3 insights atomically for ``insight_date`` (AD-5, idempotent per slot).

    Validates EVERY item fail-closed (all-or-nothing: one bad item publishes nothing), then
    persists the run + items in one transaction (35.3). ``status`` other than 'published'
    records a distinct run (no_insight / blocked / failed) with zero items. Returns a short
    write-ack {runId, status, publishedSlots}.
    """
    from datetime import date as _date  # noqa: PLC0415
    from datetime import timedelta as _timedelta  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import cards as _cards  # noqa: PLC0415
    from core import daily_insights as _store  # noqa: PLC0415
    from core import daily_insights_tools as _dit  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415

    # WRITE path: fail CLOSED on an unverifiable scope check (never publish a durable
    # cross-project artifact on an unconfirmed access check).
    identity, project_id, access_ok = _daily_insight_scope(project_id, strict=True)
    trace_id = tracing.current_trace_id_hex() or None
    items = items or []

    # Freshness over a LOOKBACK window (not [insight_date, insight_date], which is
    # self-referential): max(row date) is the true latest available data date, so a pipeline
    # that is behind the claimed date is caught by the validator's stale_data gate.
    try:
        _win_start = (_date.fromisoformat(insight_date) - _timedelta(days=7)).isoformat()
    except (ValueError, TypeError):
        _win_start = insight_date
    available_metrics, available_dimensions, freshness_date, _dq = _resolve_daily_insight_inputs(
        project_id, _win_start, insight_date
    )
    available_templates = {e["id"] for e in _cards.list_templates()}

    # Run-level analyzed window = min/max of the published items' own periods (not a single day).
    _froms = [it.get("period", {}).get("dateFrom") for it in items if it.get("period")]
    _tos = [it.get("period", {}).get("dateTo") for it in items if it.get("period")]
    _froms = [d for d in _froms if d]
    _tos = [d for d in _tos if d]
    period_from = min(_froms) if _froms else insight_date
    period_to = max(_tos) if _tos else insight_date

    try:
        with _core_db.get_connection() as _conn:
            existing = _store.get_run(project_id, insight_date, _conn)
            existing_slots = {int(i["slot"]) for i in (existing or {}).get("insights", [])}
            validate_ctx = {
                "available_metrics": available_metrics,
                "available_dimensions": available_dimensions,
                "available_templates": available_templates,
                "resolvable_evidence": _resolve_evidence_refs(
                    project_id,
                    {"evidenceRefs": [r for it in items for r in (it.get("evidenceRefs") or [])]},
                ),
                "freshness_date": freshness_date,
                "has_project_access": access_ok,
                "existing_slots": existing_slots,
            }
            ack = _dit.publish(
                project_id=project_id,
                insight_date=insight_date,
                status=status,
                item_payloads=items,
                validate_ctx=validate_ctx,
                conn=_conn,
                identity=identity,
                trace_id=trace_id,
                period_from=period_from,
                period_to=period_to,
                # Render each VALIDATED item's card + freeze a snapshot so the insight is
                # viewable/shareable (render_snapshot_id lineage). Best-effort: a render
                # failure (e.g. empty warehouse) leaves the row publishable without lineage.
                render_fn=lambda p: _render_daily_insight_snapshot(
                    project_id, identity, trace_id, p
                ),
            )
    except ToolError:
        raise
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "invalid_request", "message": str(exc)}))
    except Exception as exc:  # noqa: BLE001
        raise ToolError(json.dumps({"code": "publish_failed", "message": str(exc)}))

    if not ack["ok"]:
        raise ToolError(
            json.dumps(
                {
                    "code": "validation_failed",
                    "reason_code": ack.get("reasonCode"),
                    "message": ack.get("message"),
                    "slot": ack.get("slot"),
                }
            )
        )
    summary = (
        f"Published {len(ack['publishedSlots'])} insight(s) for {insight_date} "
        f"(run {ack['runId']}, status {ack['status']})."
    )
    return ToolResult(content=[TextContent(type="text", text=summary)], structured_content=ack)


mcp.tool(get_daily_insight_readiness)
mcp.tool(get_card_capabilities)
mcp.tool(preview_daily_insight)
mcp.tool(publish_daily_insights)


# ---------------------------------------------------------------------------
# Story 8.7 — MCP flow interface: flows_list / flows_get / flows_validate /
# flows_upsert (AD-1 dual-channel, AD-2 source-agnostic, AD-5 scoping, AD-14
# identity-from-token). These are the LLM-agent channel of the COMMON flow
# interface; core.flows_api mirrors them over REST calling the SAME core.flows
# functions. All heavy logic lives in core.flows — the tool bodies here only
# resolve identity/project, open a connection, and return the dual-channel result.
# ---------------------------------------------------------------------------


def _flow_identity() -> str:
    """Resolve caller identity from the access token (AD-14)."""
    token: AccessToken | None = get_access_token()
    return (token.claims.get("sub") or token.client_id) if token else "anonymous"


def flows_list(project_id: str, kind: str | None = None) -> ToolResult:
    """List data-flow summaries for a project (Story 8.7, AC4).

    Returns a lean <=30-line text summary plus the full summary list in the
    canonical AD-1 envelope (structuredContent). ``kind`` filters to
    'datastream' or 'report' when given.

    AD-5: scope-checked via identity_has_project_access; a cross-project access
    surfaces as a not-found error (existence not disclosed).
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import flows as _flows  # noqa: PLC0415

    identity = _flow_identity()
    project_id = _resolve_project(project_id)
    if kind is not None and kind not in ("datastream", "report"):
        raise ToolError(json.dumps(_error(
            "invalid_input", "kind doit etre 'datastream' ou 'report'")))

    try:
        with _core_db.get_connection() as conn:
            items = _flows.list_flows(project_id, identity, conn, kind=kind)
    except _flows.FlowScopeError:
        raise ToolError(json.dumps(_error("not_found", "Projet introuvable")))
    except Exception as exc:
        raise ToolError(json.dumps(_error("db_error", f"Erreur base de donnees : {exc}")))

    lines = [f"Flux du projet {project_id} : {len(items)}"]
    for it in items[:25]:
        lines.append(
            f"- [{it.get('kind')}] {it.get('id')} : "
            f"{it.get('name') or it.get('display_name') or it.get('base_report_id') or ''}"
        )
    summary = "\n".join(lines[:30])

    envelope = _envelope(
        {"project_id": project_id, "kind": kind, "flows": items, "count": len(items),
         "identity": identity},
        provenance={"source_system": "connector-core", "source_field": "flows_list",
                    "pull_id": None},
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


def flows_get(project_id: str, kind: str, id: str) -> ToolResult:
    """Get one full declarative flow document (Story 8.7, AC4).

    datastream -> the datastream row + mappings as a flow doc.
    report     -> the module base pack MERGED with the stored project override.

    Lean text summary + full document in structuredContent (AD-1). AD-5 scoped.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import flows as _flows  # noqa: PLC0415

    identity = _flow_identity()
    project_id = _resolve_project(project_id)
    if kind not in ("datastream", "report"):
        raise ToolError(json.dumps(_error(
            "invalid_input", "kind doit etre 'datastream' ou 'report'")))

    try:
        with _core_db.get_connection() as conn:
            flow = _flows.get_flow(
                project_id, kind, id, identity, conn,
                loaded_modules=_loaded_modules,
            )
    except _flows.FlowScopeError:
        raise ToolError(json.dumps(_error("not_found", f"Flux '{kind}/{id}' introuvable")))
    except Exception as exc:
        raise ToolError(json.dumps(_error("db_error", f"Erreur base de donnees : {exc}")))

    if flow is None:
        raise ToolError(json.dumps(_error("not_found", f"Flux '{kind}/{id}' introuvable")))

    _flow_label = flow.get("name") or flow.get("display_name") or id
    summary = f"Flux {kind}/{id} (projet {project_id}) : {_flow_label}"
    envelope = _envelope(
        {"project_id": project_id, "kind": kind, "id": id, "flow": flow,
         "identity": identity},
        provenance={"source_system": "connector-core", "source_field": "flows_get",
                    "pull_id": None},
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


def flows_validate(definition: dict) -> ToolResult:
    """Dry-run validation of a flow document (Story 8.7, AC4). No DB touch.

    Returns actionable errors (json-path + French message). This is the SAME
    validation path the REST endpoint and flows_upsert use -- one common contract.
    """
    from core import flows as _flows  # noqa: PLC0415

    ok, errors = _flows.validate_flow(definition)
    if ok:
        summary = "Validation OK : le document de flux est conforme au schema."
    else:
        summary = "Validation echouee :\n" + "\n".join(
            f"- {e['path']}: {e['message']}" for e in errors[:20]
        )
    envelope = _envelope(
        {"ok": ok, "errors": errors},
        provenance={"source_system": "connector-core", "source_field": "flows_validate",
                    "pull_id": None},
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text="\n".join(summary.split("\n")[:30]))],
        structured_content=envelope,
    )


def flows_upsert(project_id: str, definition: dict) -> ToolResult:
    """Validate + upsert a flow document (Story 8.7, AC4).

    Single write path: datastream flows go through core.datastreams +
    core.datamodel; report overrides land in app.report_overrides. Validates
    against the schema BEFORE any DB touch; audits 'flow_updated' with a minimal
    before/after diff; idempotent (same doc twice -> changed:false, no audit).

    AD-3: cannot touch authorization secrets (schema rejects unknown fields;
    connection_ref_id is an opaque, project-scoped reference). AD-5 scoped.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import flows as _flows  # noqa: PLC0415

    identity = _flow_identity()
    project_id = _resolve_project(project_id)

    try:
        with _core_db.get_connection() as conn:
            result = _flows.upsert_flow(
                project_id, definition, identity, conn,
                loaded_modules=_loaded_modules,
            )
    except _flows.FlowValidationError as exc:
        raise ToolError(json.dumps({
            "code": "validation_error", "message": str(exc), "errors": exc.errors}))
    except _flows.FlowScopeError:
        raise ToolError(json.dumps(_error("not_found", "Flux introuvable")))
    except _flows.FlowConflictError as exc:
        raise ToolError(json.dumps({"code": "conflict", "message": str(exc)}))
    except Exception as exc:
        raise ToolError(json.dumps(_error("db_error", f"Erreur base de donnees : {exc}")))

    changed = result.get("changed")
    verb = "mis a jour" if changed else "inchange (idempotent)"
    summary = f"Flux {result.get('kind')}/{result.get('id')} {verb}."
    envelope = _envelope(
        {"project_id": project_id, **result, "identity": identity},
        provenance={"source_system": "connector-core", "source_field": "flows_upsert",
                    "pull_id": None},
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


mcp.tool(flows_list)
mcp.tool(flows_get)
mcp.tool(flows_validate)
mcp.tool(flows_upsert)


# ---------------------------------------------------------------------------
# Story 4.3 — context_events helpers and MCP tool (AC3, AC7)
#
# AI-27 decomposition (Story 5.1): _validate_event_input and _fetch_context_events
# now live in core.context_events; re-exported here for backward compatibility.
# ---------------------------------------------------------------------------
_validate_event_input = context_events_module.validate_event_input
_fetch_context_events = context_events_module.fetch_context_events


@mcp.tool()
def add_context_event(
    project_id: str,
    event_date: str,
    type: str,
    label: str,
    description: str = "",
) -> ToolResult:
    """Add a business context event for a project (Story 4.3, AC3).

    Persists a project-scoped annotation (sale, campaign launch, incident, etc.)
    to app.context_events so that analyses can reference known business events.

    AD-2: source-agnostic — no module-specific strings.
    AD-10: this tool is the write path for the widget (callServerTool).
    HG-2: admin console uses the REST API; the widget uses this MCP tool.

    Story 9.10 (AC2): this tool STAYS model-visible (no AppConfig visibility,
    unlike submit_feedback) -- no widget calls it today (grep verified
    2026-07-14: ContextePanel uses the REST API; the HG-2 widget write path
    above describes an intent never built), and the model/chat is its real
    caller for "note this business event" requests.

    Parameters:
        project_id:   Project identifier.
        event_date:   ISO-8601 date string (YYYY-MM-DD).
        type:         Event type — free-form ('business', 'incident', 'deployment',
                      'release', ...). Not validated; Story 4.5 adds new types.
        label:        Short title, max 120 chars.
        description:  Optional free-form detail.
    """
    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    )
    created_by = identity or "anonymous"

    # Reject malformed input before project resolution touches the database.
    # ``persist_context_event`` validates again so direct callers keep the same
    # contract, while the MCP surface remains fail-fast and deterministic.
    context_events_module.validate_event_input(label, event_date)

    # Story 7.1 (AC5): resolve + validate project via the shared resolver.
    project_id = _resolve_project(project_id)

    # Validate + persist + audit (AI-27: logic lives in core.context_events).
    context_events_module.persist_context_event(
        project_id=project_id,
        event_date=event_date,
        type=type,
        label=label,
        description=description,
        created_by=created_by,
    )

    # French-first write ack (UX-DR10). No structuredContent — write ack only (AD-1).
    ack_text = f"Événement ajouté : {label} ({event_date})"
    return ToolResult(
        content=[TextContent(type="text", text=ack_text)],
    )


# ---------------------------------------------------------------------------
# Story 31.5 — get_events: cross-source annotation read surface (AD-2 / AD-8).
#
# Returns project-scoped context events (manual + connector) enriched with
# dim_event_type fields (category, default_marker) for LLM filtering and the
# MMM substrat (events = regressors, value = intensity, platform = split;
# grain jour → pivot semaine/marché en aval).
#
# module_kind is NEVER exposed (epic §2: internal plumbing).
# AD-8: project_id is always resolved + scoped (never cross-project leak).
# AD-9: read-only; no metric value is modified.
# ---------------------------------------------------------------------------


@mcp.tool()
def get_events(
    project_id: str,
    type: str | None = None,
    event_type: str | None = None,
    category: str | None = None,
    platform: str | None = None,
    source: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> ToolResult:
    """Return project-scoped context events with dim_event_type enrichment (Story 31.5).

    Source-agnostic read surface for events from ALL sources (manual + connectors).
    The ``module_kind`` of the connector that emitted each event is NEVER exposed
    (epic §2: internal plumbing). Events are cross-source by design (project-scoped,
    not connection-scoped) so a ``video_upload`` from YouTube and a ``release`` from
    GitHub both appear here for the same project.

    Returned fields per event:
        event_date, event_type (alias: type), label, platform, source, value,
        category (from dim_event_type), default_marker (from dim_event_type).

    Filters (all optional, AND-combined):
        type / event_type  — filter by event_type string (both aliases accepted).
        category           — filter by dim_event_type.category
                             (content/engineering/marketing/commerce/business/operations).
        platform           — filter by platform (e.g. "youtube", "github").
        source             — filter by source ("manual" or connector name).
        from_date          — ISO-8601 start date (inclusive), default: 90 days ago.
        to_date            — ISO-8601 end date (inclusive), default: today.

    MMM substrat contract (grain jour → pivot semaine/marché en aval):
        - ``value`` is the optional event intensity / regressor magnitude (float | null).
          A null value signals a unit-pulse regressor (the event happened, no magnitude).
        - ``platform`` is the MMM market/channel split axis.
        - Combine ``get_events`` + ``fact_daily_kpi`` (via get_daily_report) to build a
          feature matrix: events = exogenous regressors, value = magnitude weight, date
          = join key. Adstock/decay and the model itself are out of scope here.

    AD-2: source-agnostic — no module-specific strings in filters or output.
    AD-8: project_id is always resolved + scoped.
    AD-9: read-only; no metric value is modified.
    """
    from datetime import date, timedelta  # noqa: PLC0415

    from core.report_dictionary import enrich_events_with_dim  # noqa: PLC0415

    project_id = _resolve_project(project_id)

    # Resolve date window.
    today = date.today().isoformat()
    _from = from_date or (date.today() - timedelta(days=90)).isoformat()
    _to = to_date or today

    raw_events = _fetch_context_events(project_id, _from, _to)

    # Resolve the effective type filter (accept both aliases: type and event_type).
    type_filter: str | None = (type or event_type or "").strip() or None

    # Enrich with dim_event_type (category, default_marker).
    enriched = enrich_events_with_dim(raw_events)

    # Apply optional AND filters.
    results: list[dict] = []
    for e in enriched:
        if type_filter and e.get("type") != type_filter:
            continue
        if category and e.get("category") != category:
            continue
        if platform and e.get("platform") != platform:
            continue
        if source and e.get("source") != source:
            continue
        results.append(
            {
                "event_date": e.get("event_date"),
                "event_type": e.get("type"),  # canonical field name (alias exposed)
                "label": e.get("label"),
                "platform": e.get("platform"),
                "source": e.get("source"),
                "value": e.get("value"),
                "category": e.get("category", ""),
                "default_marker": e.get("default_marker", "pin"),
            }
        )

    import json as _json  # noqa: PLC0415

    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=_json.dumps(
                    {
                        "project_id": project_id,
                        "from": _from,
                        "to": _to,
                        "count": len(results),
                        "events": results,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Story 5.5 — submit_feedback: core-owned write-ack tool (AC3, AD-2, AD-10).
#
# Records 👍/👎 user feedback for a report in app.feedback (Migration 012),
# writes an audit row (ACTION_FEEDBACK_SUBMITTED), and optionally sends a
# Langfuse score when TRACING_ENABLED=true and a trace_id is supplied (AD-13).
#
# Returns a one-line French ack text only (write-ack pattern, AD-1 — no dual-
# channel for write tools). Nothing reaches the LLM context beyond the ack (FR10).
# ---------------------------------------------------------------------------


# AI-36 (Story 6.1, AC13): per-project in-memory rate limit on submit_feedback.
# Reuses the sliding-window dict pattern from admin_api._refresh_health_last:
# maps project_id -> list of monotonic timestamps of recent submissions, pruned to
# the last hour on each call. In-memory only (single-replica at P3-dev); Phase-B
# moves this to Postgres for multi-replica safety (same TODO as _refresh_health_last).
_feedback_calls: dict[str, list[float]] = {}


def _feedback_rate_limited(project_id: str, *, clock=time.monotonic) -> bool:
    """Return True if *project_id* has exceeded FEEDBACK_RATE_LIMIT_PER_HOUR (default 60).

    Records the current call when it is allowed. Sliding 1-hour window.
    """
    limit = int(os.environ.get("FEEDBACK_RATE_LIMIT_PER_HOUR", "60"))
    now = clock()
    cutoff = now - 3600.0
    calls = [t for t in _feedback_calls.get(project_id, []) if t >= cutoff]
    if len(calls) >= limit:
        _feedback_calls[project_id] = calls
        return True
    calls.append(now)
    _feedback_calls[project_id] = calls
    return False


# Story 9.10 (AC2): pure UI affordance -- submit_feedback is called only from the
# FeedbackBar / CardFeedbackBar widgets (callServerTool), never useful to the model
# directly. visibility=["app"] rides the wire meta (meta.ui.visibility, MCP Apps
# spec 2026-01-26) so conforming hosts keep it out of the model's tool list while
# widget iframes can still call it.
@mcp.tool(app=AppConfig(visibility=["app"]))
def submit_feedback(
    project_id: str,
    rating: int,
    trace_id: str | None = None,
    comment: str = "",
    report_ref: str = "",
    module: str = "",
) -> ToolResult:
    """Submit 👍/👎 feedback for a report (Story 5.5, AC3).

    Persists a user rating (1=thumbs-up, -1=thumbs-down) and optional comment
    to app.feedback, writes an audit row, and sends a Langfuse score when
    tracing is enabled and a trace_id is available (AD-13).

    AD-2: source-agnostic — no module-specific code here.
    AD-10: widget calls this via callServerTool; no direct DB writes from widget.
    AD-1: write-ack tool — returns minimal French text only; no structuredContent.
    FR10: nothing pollutes the chat thread beyond this one-line ack.

    Parameters:
        project_id:  Project identifier.
        rating:      1 (👍 thumbs-up) or -1 (👎 thumbs-down). Integer only.
        trace_id:    OTel trace_id echoed from meta.trace_id (may be null).
        comment:     Optional qualitative text.
        report_ref:  Report reference, e.g. "get_daily_report:2026-07-11".
        module:      Module name, e.g. "connector-module-name".
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as core_db  # noqa: PLC0415
    from core.audit import ACTION_FEEDBACK_SUBMITTED, write_audit_row  # noqa: PLC0415

    # Validate rating — must be exactly 1 or -1.
    if rating not in (1, -1):
        raise ToolError(
            json.dumps({"code": "invalid_input", "message": "rating must be 1 or -1"})
        )

    # Story 7.1 (AC5): resolve + validate project via the shared resolver.
    project_id = _resolve_project(project_id)

    # AI-36 (AC13): per-project rate limit (default 60/hour). French message.
    if _feedback_rate_limited(project_id):
        raise ToolError(
            json.dumps(
                {
                    "code": "rate_limited",
                    "message": "Feedback rate limit exceeded. Réessayez dans quelques minutes.",
                }
            )
        )

    # Resolve identity from access token (AD-14).
    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    )
    created_by = identity or "anonymous"

    # Mint fb_ ULID (ARCHITECTURE-SPINE §IDs: fb_ is listed explicitly).
    from ulid import ULID  # noqa: PLC0415

    fb_id = f"fb_{ULID()}"

    # Write app.feedback row.
    try:
        with core_db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.feedback
                        (id, project_id, trace_id, report_ref, module,
                         rating, comment, created_by, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        fb_id,
                        project_id,
                        trace_id or None,
                        report_ref or None,
                        module or None,
                        rating,
                        comment or None,
                        created_by,
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("submit_feedback: db_write_failed: %s", exc)
        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Database write failed: {exc}"})
        )

    # Write audit row (never raises — AC6 in audit.py).
    write_audit_row(
        identity=created_by,
        action=ACTION_FEEDBACK_SUBMITTED,
        provider_account="",
        connection_ref="",
        metadata={
            "feedback_id": fb_id,
            "rating": rating,
            "module": module or None,
            "trace_id": trace_id,
        },
    )

    # Langfuse score (AD-13): only when tracing is enabled AND trace_id is present.
    # Wrapped in try/except — Langfuse unreachable must NEVER fail the tool call (AC4).
    # Import is guarded inside the if-block: tool works when [tracing] extra absent.
    #
    # Langfuse SDK v4 note (Dev Agent Record):
    #   In Langfuse v3+ with OTel-native ingestion, the OTel trace_id (32-hex) is
    #   mapped 1:1 to the Langfuse trace_id. lf.score(trace_id=otel_trace_id_hex)
    #   therefore works directly to attach a score to the same trace. If OTel is used
    #   via OTLP (as this project does), Langfuse stores the span under the OTel
    #   trace_id, so the score links correctly. No lf.get_trace_by_otel_id() needed.
    #   Source: Langfuse OTel integration docs + SDK source review (langfuse 4.14+).
    #
    # Per-call instantiation: Langfuse client is instantiated per call (not singleton)
    # to avoid threading issues. This is slightly inefficient (creates an httpx client
    # per feedback submit). TODO: cache a module-level Langfuse client instance with
    # a lock for thread safety when call volume warrants it.
    if tracing.is_enabled() and trace_id:
        try:
            from langfuse import Langfuse  # noqa: PLC0415

            lf = Langfuse()
            lf.score(
                trace_id=trace_id,
                name="user_feedback",
                value=float(rating),
                comment=comment or None,
            )
            lf.flush()
        except Exception as lf_exc:
            # Langfuse unreachable — log but do not fail the tool call.
            logger.warning("submit_feedback: langfuse_score_failed: %s", lf_exc)

    # Write-ack only (AD-1): minimal French confirmation text, no structuredContent.
    # FR10: nothing beyond this one-line ack reaches the LLM context.
    return ToolResult(
        content=[TextContent(type="text", text="Merci pour votre retour.")],
    )


# ---------------------------------------------------------------------------
# Story 6.5 — save_notebook / run_notebook: core-owned notebook tools (AC3, AC4).
#
# Notebooks are "living documents": the window_rule is stored as a relative string
# (e.g. 'last_30d') and resolved against the CURRENT date on every re-run.
# This is what makes them living — each run reflects today's data window.
#
# pull_ids stored in notebook_runs are ALWAYS freshly resolved at run time
# (never copied from a previous run) — this is the provenance re-resolution
# proof required by AC7 / AD-9.
#
# envelope_inline size gate: store JSONB inline when < 512KB; else set
# envelope_ref='deferred' (GCS blob storage deferred to Epic 7).
# ---------------------------------------------------------------------------

_ENVELOPE_INLINE_MAX_BYTES = 512 * 1024  # 512KB


def _assert_project_access(project_id: str, identity: str) -> bool:
    """Return True if identity has access to project_id (Story 7.4, AD-5, FR12).

    Delegates to core.project_access.identity_has_project_access against the
    app.project_members ACL table (migration 021). Default-open semantics: a
    project with zero membership rows is reachable by every identity (single-
    tenant compat, keeps existing tests green); once a project has >= 1 member
    row it is closed to non-members. Identity 'anonymous' (disabled-auth dev
    mode) is always allowed. Never raises: fails OPEN on DB error / DB-less
    unit-test mocks (the hard isolation proof is server/tests/isolation/).
    """
    from core.project_access import (  # noqa: PLC0415
        _ALWAYS_ALLOWED,
        identity_has_project_access,
    )

    # Fast path: the dev/disabled-auth "anonymous" subject is always allowed and
    # needs NO DB round-trip. Short-circuiting here also keeps tool-body
    # connection accounting unchanged for the common single-tenant path (a token
    # with a real subject only appears under multi-tenant auth, exercised by the
    # isolation suite against live Postgres).
    if identity in _ALWAYS_ALLOWED:
        return True

    from core import db as _core_db  # noqa: PLC0415

    try:
        with _core_db.get_connection() as _conn:
            return identity_has_project_access(project_id, identity, _conn)
    except Exception as _exc:  # pragma: no cover - resilience path
        logger.debug("assert_project_access: passthrough (db unavailable): %s", _exc)
        return True


@mcp.tool()
def save_notebook(
    project_id: str,
    title: str,
    report_ref: str,
    window_rule: str,
    narrative_prompt: str = "",
) -> ToolResult:
    """Save a notebook definition (Story 6.5, AC3).

    Creates a living narrative document that can be re-run to get fresh data.
    The ``window_rule`` is stored as a relative string (e.g. 'last_30d') and
    resolved against today on every re-run (FR7).

    Parameters:
        project_id:       Project identifier.
        report_ref:       Report reference (e.g. 'gsc/position_movements' or 'adhoc').
        window_rule:      Relative time window (e.g. 'last_30d', 'last_7d').
        narrative_prompt: Optional override prompt; empty = use report definition's prompt.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415
    from ulid import ULID  # noqa: PLC0415

    from core import db as core_db  # noqa: PLC0415
    from core.audit import ACTION_NOTEBOOK_CREATED, write_audit_row  # noqa: PLC0415
    from core.window_rule import resolve_window_rule  # noqa: PLC0415

    # Resolve identity
    token: AccessToken | None = get_access_token()
    identity = (token.claims.get("sub") or token.client_id) if token else "anonymous"

    # AC3.1 — Validate window_rule
    try:
        resolve_window_rule(window_rule)
    except ValueError as exc:
        raise ToolError(
            json.dumps({"code": "invalid_input", "message": str(exc)})
        )

    # AC3.2 — Validate report_ref: if not 'adhoc', verify it resolves to a known report
    if report_ref != "adhoc":
        if "/" not in report_ref:
            raise ToolError(
                json.dumps(
                    {"code": "not_found", "message": f"Unknown report_ref: {report_ref!r}"}
                )
            )
        module_name, _, report_def_id = report_ref.partition("/")
        from core import reports as reports_module  # noqa: PLC0415

        found = reports_module.find_report(_loaded_modules, module_name, report_def_id)
        if found is None:
            raise ToolError(
                json.dumps(
                    {"code": "not_found", "message": f"Unknown report_ref: {report_ref!r}"}
                )
            )

    # Story 7.1 (AC5): resolve + validate project via the shared resolver.
    project_id = _resolve_project(project_id)

    # AC3.3 — Verify project access (AD-5)
    if not _assert_project_access(project_id, identity):
        raise ToolError(
            json.dumps({"code": "forbidden", "message": "Project not accessible"})
        )

    # AC3.4 — Mint nb_ ULID
    nb_id = f"nb_{ULID()}"

    # AC3.5 — Insert into app.notebooks
    try:
        with core_db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.notebooks
                        (id, project_id, title, report_ref, window_rule,
                         narrative_prompt, created_by, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    """,
                    (
                        nb_id,
                        project_id,
                        title,
                        report_ref,
                        window_rule,
                        narrative_prompt or None,
                        identity,
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("save_notebook: db_write_failed: %s", exc)
        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Database write failed: {exc}"})
        )

    # AC3.7 — Write audit row
    write_audit_row(
        identity=identity,
        action=ACTION_NOTEBOOK_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "notebook_id": nb_id,
            "project_id": project_id,
            "title": title,
            "report_ref": report_ref,
            "window_rule": window_rule,
        },
    )

    # AC3.6 — Return write ack (≤3 lines, no structuredContent)
    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Notebook '{title}' sauvegardé (id: {nb_id}).",
            )
        ],
    )


def run_notebook_direct(notebook_id: str, as_of: str | None = None) -> dict:
    """Run a notebook and return a result dict.  Callable without the MCP layer.

    This is the underlying implementation extracted so the nightly scheduler
    (Story 6.6, AC1) can call it directly without going through the MCP tool
    wrapper.  The MCP ``run_notebook`` tool delegates here.

    Returns:
        {"run_id": str, "summary": str, "pull_ids": list[str]}

    Raises:
        ValueError: notebook not found or invalid window_rule.
        RuntimeError: report render failure or DB write failure.
    """
    import json as _json  # noqa: PLC0415

    from ulid import ULID  # noqa: PLC0415

    from core import db as core_db  # noqa: PLC0415
    from core import reports as reports_module  # noqa: PLC0415
    from core.audit import ACTION_NOTEBOOK_RUN, write_audit_row  # noqa: PLC0415
    from core.window_rule import resolve_window_rule  # noqa: PLC0415

    identity = "scheduler"
    as_of_str = as_of.strip() if as_of else None

    # Fetch notebook row
    notebook = None
    try:
        with core_db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, project_id, title, report_ref, window_rule,
                           narrative_prompt, created_by
                    FROM app.notebooks
                    WHERE id = %s
                    """,
                    (notebook_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    cols = [d[0] for d in cur.description]
                    notebook = dict(zip(cols, row))
    except Exception as exc:
        raise RuntimeError(f"Database error fetching notebook {notebook_id}: {exc}") from exc

    if notebook is None:
        raise ValueError(f"Notebook not found: {notebook_id}")

    project_id = notebook["project_id"]

    # Resolve window_rule to absolute dates
    window_rule = notebook["window_rule"]
    try:
        date_from, date_to = resolve_window_rule(window_rule)
    except ValueError as exc:
        raise ValueError(f"Invalid window_rule '{window_rule}': {exc}") from exc

    report_ref = notebook["report_ref"]

    # Call render_report directly (not via MCP tool layer)
    if report_ref == "adhoc":
        envelope: dict = {
            "schema_version": "1",
            "meta": {
                "provenance": {"pull_ids": [], "pull_id": None,
                               "source_system": "adhoc", "source_field": "adhoc"},
                "alerts": [],
                "freshness": None,
                "as_of": as_of_str,
                "context_events": [],
            },
            "data": {
                "report_id": "adhoc",
                "date_range": {"start": date_from, "end": date_to},
                "connectors": [],
                "metrics": {},
                "rows": [],
            },
        }
        summary = "Aucune donnée disponible (notebook adhoc)."
        pull_ids: list[str] = []
    else:
        try:
            trace_id = tracing.current_trace_id_hex() or None
            summary, envelope, _widget_uri = reports_module.render_report(
                _loaded_modules,
                project_id,
                report_ref,
                date_from,
                date_to,
                trace_id=trace_id,
                geographic_posture=_load_project_geographic_posture(project_id),
            )
        except reports_module.ReportNotFound as exc:
            raise ValueError(f"Report not found: {report_ref}") from exc
        except Exception as exc:
            raise RuntimeError(f"Report render failed: {exc}") from exc

        pull_ids = envelope.get("meta", {}).get("provenance", {}).get("pull_ids", [])

        if as_of_str:
            summary = summary + f"\nDonnées reconstituées au {as_of_str}."

    # Build narrative with the notebook's optional narrative_prompt override
    nb_prompt = (notebook.get("narrative_prompt") or "").strip() or None
    if nb_prompt and report_ref != "adhoc":
        from core import narrative as narrative_module  # noqa: PLC0415
        from core import rollup as rollup_module  # noqa: PLC0415

        rows_data = envelope.get("data", {}).get("rows", [])
        metrics = list(envelope.get("data", {}).get("metrics", {}).keys())
        rollup = rollup_module.compute_rollup(
            rows_data, metrics, date_from, date_to, project_id, pull_ids
        )
        summary = narrative_module.build_narrative(
            project_id=project_id,
            report_id=report_ref,
            rollup=rollup,
            context_events=envelope.get("meta", {}).get("context_events", []),
            alerts=envelope.get("meta", {}).get("alerts", []),
            as_of=as_of_str,
            narrative_prompt=nb_prompt,
        )

    # Determine envelope_inline
    envelope_inline_val = None
    envelope_ref_val = None
    try:
        envelope_bytes = len(_json.dumps(envelope).encode("utf-8"))
        if envelope_bytes < _ENVELOPE_INLINE_MAX_BYTES:
            envelope_inline_val = _json.dumps(envelope)
        else:
            envelope_ref_val = "deferred"
            logger.info(
                "run_notebook_direct: envelope_too_large_for_inline: %d bytes (notebook=%s)",
                envelope_bytes,
                notebook_id,
            )
    except Exception:
        envelope_ref_val = "deferred"

    # Mint nbrun_ ULID and insert into app.notebook_runs
    nbrun_id = f"nbrun_{ULID()}"
    try:
        with core_db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.notebook_runs
                        (id, notebook_id, executed_at, as_of, summary_text,
                         envelope_ref, envelope_inline, pull_ids, status)
                    VALUES (%s, %s, NOW(), %s, %s, %s, %s::jsonb, %s, 'success')
                    """,
                    (
                        nbrun_id,
                        notebook_id,
                        as_of_str or None,
                        summary[:10000],
                        envelope_ref_val,
                        envelope_inline_val,
                        pull_ids,
                    ),
                )
            conn.commit()
    except Exception as exc:
        raise RuntimeError(f"Database write failed: {exc}") from exc

    # Write audit row
    write_audit_row(
        identity=identity,
        action=ACTION_NOTEBOOK_RUN,
        provider_account="",
        connection_ref="",
        metadata={
            "notebook_id": notebook_id,
            "run_id": nbrun_id,
            "project_id": project_id,
            "pull_ids_count": len(pull_ids),
        },
    )

    envelope.setdefault("meta", {})["notebook_id"] = notebook_id
    envelope.setdefault("meta", {})["run_id"] = nbrun_id

    return {"run_id": nbrun_id, "summary": summary, "pull_ids": pull_ids}


@mcp.tool()
def run_notebook(
    notebook_id: str,
    as_of: str = "",
) -> ToolResult:
    """Run a notebook, producing a fresh narrative with current data (Story 6.5, AC4).

    Resolves the stored window_rule against today to get the date window,
    calls the report render function directly (not via MCP layer — avoids
    double-serialization), builds the narrative, and stores the run with
    fresh pull_ids (provenance re-resolution proof, AC7 / AD-9).

    Parameters:
        notebook_id: The nb_ ULID of the notebook to run.
        as_of:       Optional ISO date for as-of replay (Story 4.6). Empty = current data.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415
    from ulid import ULID  # noqa: PLC0415

    from core import db as core_db  # noqa: PLC0415
    from core import reports as reports_module  # noqa: PLC0415
    from core.audit import ACTION_NOTEBOOK_RUN, write_audit_row  # noqa: PLC0415
    from core.window_rule import resolve_window_rule  # noqa: PLC0415

    # Resolve identity
    token: AccessToken | None = get_access_token()
    identity = (token.claims.get("sub") or token.client_id) if token else "anonymous"

    # AC4.1 — Fetch notebook row
    notebook = None
    try:
        with core_db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, project_id, title, report_ref, window_rule,
                           narrative_prompt, created_by
                    FROM app.notebooks
                    WHERE id = %s
                    """,
                    (notebook_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    cols = [d[0] for d in cur.description]
                    notebook = dict(zip(cols, row))
    except Exception as exc:
        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Database error: {exc}"})
        )

    if notebook is None:
        raise ToolError(
            json.dumps({"code": "not_found", "message": f"Notebook not found: {notebook_id}"})
        )

    # Check project access (AD-5)
    project_id = notebook["project_id"]
    if not _assert_project_access(project_id, identity):
        raise ToolError(
            json.dumps({"code": "not_found", "message": f"Notebook not found: {notebook_id}"})
        )

    # AC4.2 — Resolve window_rule to absolute dates
    window_rule = notebook["window_rule"]
    try:
        date_from, date_to = resolve_window_rule(window_rule)
    except ValueError as exc:
        raise ToolError(
            json.dumps({"code": "invalid_input", "message": str(exc)})
        )

    report_ref = notebook["report_ref"]
    as_of_str = as_of.strip() if as_of else None

    # AC4.3 — Call render_report directly (NOT via MCP tool layer — avoid double-serialization)
    if report_ref == "adhoc":
        # adhoc notebooks: use the daily report logic (no specific report def)
        envelope = {
            "schema_version": "1",
            "meta": {
                "provenance": {"pull_ids": [], "pull_id": None,
                               "source_system": "adhoc", "source_field": "adhoc"},
                "alerts": [],
                "freshness": None,
                "as_of": as_of_str,
                "context_events": [],
            },
            "data": {
                "report_id": "adhoc",
                "date_range": {"start": date_from, "end": date_to},
                "connectors": [],
                "metrics": {},
                "rows": [],
            },
        }
        summary = "Aucune donnée disponible (notebook adhoc)."
        pull_ids: list[str] = []
    else:
        try:
            trace_id = tracing.current_trace_id_hex() or None
            summary, envelope, _widget_uri = reports_module.render_report(
                _loaded_modules,
                project_id,
                report_ref,
                date_from,
                date_to,
                trace_id=trace_id,
                geographic_posture=_load_project_geographic_posture(project_id),
            )
        except reports_module.ReportNotFound:
            raise ToolError(
                json.dumps(
                    {"code": "not_found", "message": f"Report not found: {report_ref}"}
                )
            )
        except Exception as exc:
            raise ToolError(
                json.dumps(
                    {"code": "warehouse_query_error", "message": f"Report render failed: {exc}"}
                )
            )

        # Extract pull_ids from envelope provenance (freshly resolved at this run)
        pull_ids = envelope.get("meta", {}).get("provenance", {}).get("pull_ids", [])

        # If as_of provided, rebuild narrative with as_of context
        if as_of_str:
            summary = summary + f"\nDonnées reconstituées au {as_of_str}."

    # AC4.4 — Build narrative with the notebook's optional narrative_prompt override
    # Note: render_report already builds the narrative via build_summary which calls
    # narrative.build_narrative. We use the result directly. If the notebook has a
    # custom narrative_prompt, we rebuild the narrative section.
    nb_prompt = (notebook.get("narrative_prompt") or "").strip() or None
    if nb_prompt and report_ref != "adhoc":
        # Rebuild narrative with the notebook's custom prompt
        from core import narrative as narrative_module  # noqa: PLC0415
        from core import rollup as rollup_module  # noqa: PLC0415

        rows_data = envelope.get("data", {}).get("rows", [])
        metrics = list(envelope.get("data", {}).get("metrics", {}).keys())
        rollup = rollup_module.compute_rollup(
            rows_data, metrics, date_from, date_to, project_id, pull_ids
        )
        summary = narrative_module.build_narrative(
            project_id=project_id,
            report_id=report_ref,
            rollup=rollup,
            context_events=envelope.get("meta", {}).get("context_events", []),
            alerts=envelope.get("meta", {}).get("alerts", []),
            as_of=as_of_str,
            narrative_prompt=nb_prompt,
        )

    # AC4.5 — Determine envelope_inline: store inline if < 512KB; else defer
    envelope_inline_val = None
    envelope_ref_val = None
    try:
        envelope_bytes = len(json.dumps(envelope).encode("utf-8"))
        if envelope_bytes < _ENVELOPE_INLINE_MAX_BYTES:
            envelope_inline_val = json.dumps(envelope)
        else:
            envelope_ref_val = "deferred"
            logger.info(
                "run_notebook: envelope_too_large_for_inline: %d bytes (notebook=%s)",
                envelope_bytes,
                notebook_id,
            )
    except Exception:
        # If serialization fails, defer gracefully
        envelope_ref_val = "deferred"

    # AC4.6 — Mint nbrun_ ULID and insert into app.notebook_runs
    nbrun_id = f"nbrun_{ULID()}"
    try:
        with core_db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.notebook_runs
                        (id, notebook_id, executed_at, as_of, summary_text,
                         envelope_ref, envelope_inline, pull_ids, status)
                    VALUES (%s, %s, NOW(), %s, %s, %s, %s::jsonb, %s, 'success')
                    """,
                    (
                        nbrun_id,
                        notebook_id,
                        as_of_str or None,
                        summary[:10000],  # guard against extremely long summaries
                        envelope_ref_val,
                        envelope_inline_val,
                        pull_ids,
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("run_notebook: db_write_failed: %s", exc)
        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Database write failed: {exc}"})
        )

    # AC4.9 — Write audit row
    write_audit_row(
        identity=identity,
        action=ACTION_NOTEBOOK_RUN,
        provider_account="",
        connection_ref="",
        metadata={
            "notebook_id": notebook_id,
            "run_id": nbrun_id,
            "project_id": project_id,
            "pull_ids_count": len(pull_ids),
        },
    )

    # AC4.8 — Add meta.notebook_id and meta.run_id to envelope
    envelope.setdefault("meta", {})["notebook_id"] = notebook_id
    envelope.setdefault("meta", {})["run_id"] = nbrun_id

    # AC4.7 — Return dual-channel: summary text (LLM) + envelope (data/widget)
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


# ---------------------------------------------------------------------------
# Core-owned agent-surface tools: search_context + get_procedure (Story 11.5).
#
# Both are CORE-owned and registered un-namespaced (AD-2 -- the context layer is
# cross-module knowledge, never provider-prefixed). Retrieval + the documented
# ranking (title > description > graph neighbours) live in core.context_search;
# main.py owns only the AD-1 dual-channel wrapping: a <=30-LINE plain-text summary
# on the LLM channel + the full ranked detail in structuredContent. AD-5 scoping
# is enforced in the SQL (platform + caller's project only); current versions only.
# ---------------------------------------------------------------------------

# NFR1 / AD-1: the search_context summary never exceeds this many lines.
_SEARCH_CONTEXT_MAX_LINES = 30


def _mark_context_consulted(tool_name: str, project_id: str | None = None) -> None:
    """Mark the current session as having consulted context (Story 11.6, AD-18).

    Records that ``search_context``/``get_procedure`` ran under the current session
    key (trace parent + time window, scoped by ``project_id`` in the time-window
    branch) so a subsequent data query in the SAME session is measured as adherent.
    Best-effort; never raises into the tool path.
    """
    try:
        from core import adherence as _adherence  # noqa: PLC0415

        trace_id = tracing.current_trace_id_hex() or None
        _adherence.mark_context_call(
            tool_name, trace_id=trace_id, project_id=project_id
        )
    except Exception as _mark_exc:  # noqa: BLE001
        logger.debug("%s: context_mark_skipped: %s", tool_name, _mark_exc)


def _build_search_context_summary(query: str, hits: list, project_id: str) -> str:
    """Build the <=30-line plain-text summary for search_context (AD-1).

    One header line + one line per ranked hit, hard-capped at 30 lines total so
    the heavy detail never enters the LLM context (NFR1). A trailing "[+N de
    plus]" line is emitted (still within the cap) when hits are truncated.
    """
    if not hits:
        return f"Aucun contexte trouvé pour « {query} »."

    header = f"Contexte pour « {query} » — {len(hits)} résultat(s), les plus pertinents d'abord :"
    lines = [header]
    # Reserve one line for a possible truncation marker.
    budget = _SEARCH_CONTEXT_MAX_LINES - 1
    shown = 0
    for hit in hits:
        if len(lines) >= budget:
            break
        marker = "" if hit.matched else " (lié)"
        # [title cap] Collapse whitespace in the title so an embedded newline cannot
        # expand ONE ranked entry into many physical lines and break the <=30-line
        # cap (AD-1 / NFR1). The snippet is already single-line (_snippet collapses).
        safe_title = " ".join((hit.title or "").split())
        lines.append(f"[{hit.tier}] {hit.kind}: {safe_title}{marker} — {hit.snippet}")
        shown += 1
    remaining = len(hits) - shown
    if remaining > 0:
        lines.append(f"[+{remaining} de plus dans le détail]")
    # Belt-and-suspenders: never exceed the cap even if arithmetic drifts.
    return "\n".join(lines[:_SEARCH_CONTEXT_MAX_LINES])


def search_context(query: str, project_id: str = "default") -> ToolResult:
    """Search the governed context layer -- lean summary + ranked detail (AD-1).

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    Ranks topics, procedures and schema-context docs by relevance to *query*
    (title/name/relation match > body/description match > one-hop graph
    neighbour of a match -- the algorithm is documented in core.context_search).
    Returns a <=30-line plain-text summary on the LLM channel and the full ranked
    list in structuredContent (token-burn split, AD-1 / NFR1). AD-5 scoped: the
    caller sees its own project's context plus platform context, never another
    project's. Current versions only.

    Parameters:
        query:      Free-text search term (a metric name, a concept, a warehouse
                    relation, ...). Blank -> empty result.
        project_id: Project identifier (default: 'default').
    """
    from core import context_search as _ctx_search  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id)

    hits: list = []
    try:
        with _core_db.get_connection() as _conn:
            hits = _ctx_search.search_context(
                _conn, query=query, project_id=project_id
            )
    except Exception as _exc:  # noqa: BLE001 -- resilience path (DB down => empty)
        logger.debug("search_context: search_skipped (db unavailable): %s", _exc)
        hits = []

    # Story 11.6 (AD-18): mark this session as "context consulted" so a later data
    # query (get_report/get_card/get_daily_report) in the SAME session records as
    # adherent. [F-1] The mark fires ONLY after a successful, NON-EMPTY retrieval:
    # a DB failure / empty search leaves the session non-adherent (mark-before-
    # retrieval would have falsely marked a failed search as "context consulted").
    if hits:
        _mark_context_consulted("search_context", project_id)

    summary = _build_search_context_summary(query, hits, project_id)

    envelope = _envelope(
        {
            "query": query,
            "project_id": project_id,
            "identity": identity,
            "ranking": "title > description > graph_neighbor",
            "results": [h.as_dict() for h in hits],
            "count": len(hits),
        },
        provenance={
            "source_system": "connector-core",
            "source_field": "search_context",
            "pull_id": None,
        },
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


mcp.tool(search_context)


def get_procedure(name: str, project_id: str = "default") -> ToolResult:
    """Fetch a Skill-Pack procedure by NAME -- frontmatter + body (Story 11.5).

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    A procedure is citable by name. Returns the current (active) version scoped to
    the caller's project, preferring a project-scoped override over the platform
    procedure of the same name (AD-5). The LLM channel carries the procedure name
    + description; the full frontmatter YAML and Markdown body ride
    structuredContent (AD-1). A not-found name returns a stable, NON-DISCLOSING
    result (never reveals whether the name exists in another project's scope).

    Parameters:
        name:       The procedure's frontmatter ``name`` (citable identifier).
        project_id: Project identifier (default: 'default').
    """
    from core import context_search as _ctx_search  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id)

    proc: dict | None = None
    try:
        with _core_db.get_connection() as _conn:
            proc = _ctx_search.get_procedure_by_name(
                _conn, name=name, project_id=project_id
            )
    except Exception as _exc:  # noqa: BLE001 -- resilience path
        logger.debug("get_procedure: fetch_skipped (db unavailable): %s", _exc)
        proc = None

    # Story 11.6 (AD-18): mark this session as "context consulted" (see search_context).
    # [F-1] Only a genuine, found procedure counts: a not-found / DB-down lookup
    # (proc is None) must NOT mark the session adherent.
    if proc is not None:
        _mark_context_consulted("get_procedure", project_id)

    if proc is None:
        # Stable, non-disclosing not-found. Same message whether the name is
        # unknown or lives in another project's scope (no scope leak, AD-5).
        summary = f"Procédure « {name} » introuvable."
        envelope = _envelope(
            {
                "name": name,
                "project_id": project_id,
                "identity": identity,
                "found": False,
                "procedure": None,
            },
            provenance={
                "source_system": "connector-core",
                "source_field": "get_procedure",
                "pull_id": None,
            },
            freshness="live",
        )
        return ToolResult(
            content=[TextContent(type="text", text=summary)],
            structured_content=envelope,
        )

    # LLM channel: name + one-line description only (the body rides the detail).
    desc = (proc.get("description") or "").strip()
    summary = f"Procédure « {proc['name']} »"
    if desc:
        summary += f" : {desc}"

    envelope = _envelope(
        {
            "name": proc["name"],
            "project_id": project_id,
            "identity": identity,
            "found": True,
            "procedure": {
                "id": proc["id"],
                "name": proc["name"],
                "description": proc.get("description", ""),
                "frontmatter_yaml": proc.get("frontmatter_yaml", ""),
                "body_md": proc.get("body_md", ""),
                "version_number": proc.get("version_number"),
                "scope": "platform" if proc.get("project_id") is None else "project",
            },
        },
        provenance={
            "source_system": "connector-core",
            "source_field": "get_procedure",
            "pull_id": None,
        },
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


mcp.tool(get_procedure)


# ---------------------------------------------------------------------------
# Story 13.4 -- get_data_quality_report (core-owned MCP tool).
#
# AD-1 dual channel: <=30-line text summary on the LLM channel + structured
# envelope (monitors[], issues[], freshness, provenance) on structuredContent.
# AD-5: project scope enforced via identity_has_project_access before any DB read.
# AD-9: every issue in the envelope carries firing_id + pull_ids.
# Degraded: zero monitors evaluated / DB down -> honest summary, never an exception.
# ---------------------------------------------------------------------------


def get_data_quality_report(
    project_id: str = "default",
    module: str | None = None,
) -> ToolResult:
    """Data quality health report for a project -- lean summary + full detail (AD-1).

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)
    # AD-5: project scoping enforced -- caller sees only its own project issues.
    # AD-9: every issue carries firing_id (primary provenance) and pull_ids
    #        (best-effort; typically empty for aggregated DQ monitors because
    #        infra_alerts.write_infra_firing hardcodes pull_ids='{}').

    Returns a <=30-line plain-text summary on the LLM channel (AD-1) with:
      - intro: project + evaluation window
      - one line per DQ monitor (label, % healthy, N unresolved issues)
      - freshness: date of last successful extraction
      - total open issues + top-3 most-recent

    The full canonical AD-1 envelope rides structuredContent:
      data.monitors[]:  [{type, label, healthy_pct, unresolved_count}]
      data.issues[]:    [{firing_id, type, datastream_id, datastream_name,
                          module_name, message, fired_at, severity, pull_ids}]
        Note: firing_id is the primary provenance key (app.alert_firings PK).
              pull_ids reflects the raw TEXT[] from Postgres -- empty [] for
              most DQ firings (aggregated monitors don't track individual pulls).
      meta.freshness:   last_pull_at | "no evaluation"
      meta.provenance:  {source_system, source_field, pull_id}
      meta.alerts:      []  (DQ issues live in data.issues, not meta.alerts)

    Degraded path: if no DQ data exists (project new, DB hiccup) the tool
    returns an honest summary ("aucune evaluation disponible") with zero issues
    and never raises an exception to the agent.  If the firing-count query fails
    but raw issues could be read, the summary explicitly flags monitors as
    unavailable and derives total_unresolved from the issue list (H1 coherence).

    Parameters:
        project_id: Project identifier (default: 'default').
        module:     Optional module name to filter (e.g. "google-ads").
                    When None, all modules for the project are included.
                    AD-2: this is opaque -- no module names are hardcoded here.
    """
    from core import db as _core_db  # noqa: PLC0415
    from core.dq_api import _MONITOR_LABELS, fetch_dq_report_data  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        ProjectAccessUnavailable,
        identity_has_project_access,
    )

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id)

    def _denied_response(reason: str = "denied") -> ToolResult:
        """Return a non-disclosing, non-error response for access denied / unavailable."""
        _summary = f"Projet {project_id!r} introuvable ou acces refuse."
        _env = _envelope(
            {
                "project_id": project_id,
                "module": module,
                "monitors": [],
                "issues": [],
                "total_unresolved": 0,
                "evaluated_days_30d": 0,
            },
            provenance={
                "source_system": "connector-core",
                "source_field": "get_data_quality_report",
                "pull_id": None,
            },
            freshness=reason,
        )
        return ToolResult(
            content=[TextContent(type="text", text=_summary)],
            structured_content=_env,
        )

    # AD-5: enforce project scope before any DB read.
    # fail_closed=True: if the scope DB is unavailable we cannot confirm access
    # for a cross-project governed resource -- return the same non-disclosing
    # response as an explicit denial (M1 fix).
    # Exception: "anonymous" identity short-circuits inside identity_has_project_access
    # (single-tenant dev path) and never touches the DB, so fail_closed is safe.
    try:
        with _core_db.get_connection() as _conn_scope:
            if not identity_has_project_access(
                project_id, identity, _conn_scope, fail_closed=True
            ):
                return _denied_response("denied")
    except ProjectAccessUnavailable as _pau:
        logger.debug(
            "get_data_quality_report: scope_check_unavailable project=%s: %s",
            project_id,
            _pau,
        )
        return _denied_response("scope_unavailable")
    except Exception as _scope_exc:  # noqa: BLE001 -- e.g. get_connection itself fails
        logger.debug(
            "get_data_quality_report: scope_connection_failed project=%s: %s",
            project_id,
            _scope_exc,
        )
        return _denied_response("scope_unavailable")

    # Fetch DQ data (resilient: returns partial/empty on any query failure).
    dq_data: dict = {
        "monitors": [],
        "issues": [],
        "freshness_last_pull_at": None,
        "evaluated_days_30d": 0,
        "total_unresolved": 0,
    }
    try:
        with _core_db.get_connection() as _conn_dq:
            dq_data = fetch_dq_report_data(project_id, _conn_dq, module=module)
    except Exception as _fetch_exc:  # noqa: BLE001 -- degraded path
        logger.debug("get_data_quality_report: fetch_skipped: %s", _fetch_exc)

    monitors = dq_data.get("monitors") or []
    issues = dq_data.get("issues") or []
    freshness_ts = dq_data.get("freshness_last_pull_at")
    evaluated_days = dq_data.get("evaluated_days_30d", 0)
    total_unresolved = dq_data.get("total_unresolved", 0)
    monitors_unavailable = dq_data.get("monitors_unavailable", False)

    # ------------------------------------------------------------------
    # Build the <=30-line LLM-channel summary (AD-1).
    # Line budget: 1 header + 1 window + 5 monitors + 1 freshness + 1 blank
    #              + 1 total + up to 3 top issues + 1 footer = <=14 lines max.
    # ------------------------------------------------------------------
    lines: list[str] = []

    module_suffix = f" (module : {module})" if module else ""
    lines.append(f"Rapport qualite de donnees -- projet {project_id!r}{module_suffix}")
    lines.append(f"Fenetre : 30 derniers jours (jours evalues : {evaluated_days})")

    if not monitors:
        # Degraded: no monitor data at all.
        if monitors_unavailable and issues:
            # H1 coherence: monitors query failed but raw issues were read.
            # Explicitly signal the partial state rather than claiming "0 issues".
            lines.append(
                f"Moniteurs indisponibles (erreur DB partielle) -- "
                f"{len(issues)} issue(s) brute(s) detectee(s)."
            )
        else:
            lines.append("Aucune evaluation de moniteur disponible.")
    else:
        for m in monitors:
            label = m.get("label", m.get("type", "?"))
            healthy_pct = m.get("healthy_pct", 100.0)
            unresolved = m.get("unresolved_count", 0)
            issue_word = "issue" if unresolved <= 1 else "issues"
            lines.append(
                f"  {label}: {healthy_pct:.1f}% sain, {unresolved} {issue_word} non-resolues"
            )

    # Freshness line.
    if freshness_ts:
        lines.append(f"Derniere extraction reussie : {freshness_ts}")
    else:
        lines.append("Derniere extraction reussie : aucune donnee")

    lines.append("")  # blank separator
    lines.append(f"Total issues non-resolues (30 j) : {total_unresolved}")

    # Top-3 most-recent issues.
    if issues:
        lines.append("Issues recentes (top 3) :")
        for issue in issues[:3]:
            ds = issue.get("datastream_name") or issue.get("datastream_id") or "?"
            label = _MONITOR_LABELS.get(issue.get("type", ""), issue.get("type", "?"))
            fired = (issue.get("fired_at") or "")[:10]  # YYYY-MM-DD only
            lines.append(f"  [{label}] {ds} -- {fired} (id: {issue.get('firing_id', '?')})")
    else:
        lines.append("Aucune issue ouverte detectee.")

    # Belt-and-suspenders: clamp to 30 lines (AD-1 NFR1).
    summary = "\n".join(lines[:30])

    # ------------------------------------------------------------------
    # Build the AD-1 structuredContent envelope.
    # ------------------------------------------------------------------
    freshness_label = freshness_ts if freshness_ts else "no evaluation"
    env = _envelope(
        {
            "project_id": project_id,
            "module": module,
            "monitors": monitors,
            "issues": issues,
            "total_unresolved": total_unresolved,
            "evaluated_days_30d": evaluated_days,
        },
        provenance={
            "source_system": "connector-core",
            "source_field": "get_data_quality_report",
            "pull_id": None,
        },
        freshness=freshness_label,
    )

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=env,
    )


mcp.tool(get_data_quality_report)


# ---------------------------------------------------------------------------
# HTTP surface / ASGI routing (Stories 1.1, 2.4, 2.6).
#
# AI-27 decomposition (Story 5.1): HostHeaderValidationMiddleware, the /api/audit
# endpoint, and build_asgi_app() now live in core.routing. They are re-exported
# here for backward compatibility with existing imports/tests. The mcp instance is
# passed into routing.build_asgi_app so core.routing has no import cycle with main.
# ---------------------------------------------------------------------------
HostHeaderValidationMiddleware = routing.HostHeaderValidationMiddleware
_audit_endpoint = routing.audit_endpoint


def build_asgi_app():
    """Return the FastMCP streamable-HTTP ASGI app (delegates to core.routing).

    Thin wrapper preserving the historical ``core.main.build_asgi_app()`` entry
    point; the routing/dispatch construction lives in core.routing (Story 5.1).
    """
    return routing.build_asgi_app(mcp)


def main() -> None:
    """Start the server over streamable HTTP, binding 0.0.0.0:$PORT."""
    import uvicorn

    # Local dev convenience: load a repo-root .env if present so PLATFORM_DB_URL
    # and provider secrets are available without a manual `export`. No-op in
    # managed deploys (Cloud Run injects env directly; there is no .env there).
    try:
        from dotenv import load_dotenv  # noqa: PLC0415

        _env = Path(__file__).resolve().parents[2] / ".env"
        if _env.is_file():
            load_dotenv(_env)
    except ImportError:
        pass  # python-dotenv is a dev extra; absent in slim prod images.

    port = int(os.environ.get("PORT", "8000"))
    # host fixed to 0.0.0.0 per ARCHITECTURE-SPINE §Deployment (Cloud Run).
    uvicorn.run(build_asgi_app(), host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
