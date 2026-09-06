"""The two core-owned cross-connector tools: `list_connectors`, `get_source_capabilities`.

Cross-connector tools are registered un-namespaced on the core app (AD-2): only
the core may join across Connectors. Both read the loaded-module registry and
sanitize what they publish -- a manifest carries more than a caller may see.

`_loaded_modules` is imported from `core.main` inside the bodies: `core.main`
imports this module, so a module-level import back would be a cycle, and
thirty-five suites patch that registry at the `core.main` address. A module that
captured it at import time would ignore the patch in silence.
"""

from __future__ import annotations

import json
import logging

from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

logger = logging.getLogger(__name__)


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


def list_connectors(project_id: str = "default") -> dict:
    """List the Connectors enabled for a project, with their report profiles.

    A CONNECTOR is an installed source adapter (Google Ads, Shopify, GSC...)
    and the contract of what that provider can give: report profiles, fields,
    grain and extraction limits. It is NOT a credential (that is a source
    authorization), NOT a provider account, and NOT a running pipeline (that is
    a Datastream). Use this to answer "what could this project collect?", not
    "what is it collecting?". See docs/product-architecture/glossary.md.

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    This is a core-owned tool (not namespaced) that surfaces the Connector
    registry to LLM clients. Story 7.2 (AC5): filters to only the Connectors
    enabled for the requesting project (default-enabled when no row in
    app.project_modules). No Connector-specific logic lives here (AD-2).
    """
    from core import db as _core_db  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _envelope,
        _loaded_modules,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )
    from core.module_enablement import is_module_enabled  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    resolved_project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(resolved_project_id, identity)

    connectors = []
    try:
        # AC6 -- le plancher monte avec la garde : la connexion qui LIT porte le
        # contexte d'acces, pas seulement celle qui a resolu l'autorisation.
        with _core_db.request_connection(identity) as _conn:
            for loaded in _loaded_modules:
                manifest = loaded.manifest
                if not is_module_enabled(loaded.name, resolved_project_id, _conn):
                    continue
                connectors.append(
                    {
                        "name": loaded.name,
                        "display_name": manifest.get("display_name"),
                        "auth_type": manifest.get("auth_type"),
                        "report_profiles": _public_profile_summaries(manifest),
                        # Audit 2026-08-20 (C18): a connector WITHOUT an expert
                        # report pack is indistinguishable from one that has
                        # some -- `get_report` then has nothing to render and
                        # the caller learns it late. 0 is the honest count.
                        "expert_report_packs": len(getattr(loaded, "reports", None) or []),
                    }
                )
    except Exception as _exc:
        # DB unavailable: fall back to every installed Connector (resilience path).
        logger.debug("list_connectors: enablement_check_skipped (db unavailable): %s", _exc)
        for loaded in _loaded_modules:
            manifest = loaded.manifest
            connectors.append(
                {
                    "name": loaded.name,
                    "display_name": manifest.get("display_name"),
                    "auth_type": manifest.get("auth_type"),
                    "report_profiles": _public_profile_summaries(manifest),
                    "expert_report_packs": len(getattr(loaded, "reports", None) or []),
                }
            )

    return _envelope(
        {
            "connectors": connectors,
            "count": len(connectors),
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
            "source_field": "list_connectors",
            "pull_id": None,
        },
        freshness="live",
    )




def get_source_capabilities(
    project_id: str, connection_ref_id: str
) -> ToolResult:
    """Describe valid fields and report combinations for one owned connection."""

    from core import db as _core_db  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _error,
        _loaded_modules,
        get_access_token,
    )
    from core.source_capabilities import (  # noqa: PLC0415
        SourceCapabilitiesNotFound,
        SourceCapabilitiesUnavailable,
        get_scoped_source_capabilities,
    )

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    try:
        # 67-1 (2026-08-24): the read that answers "what may this connection
        # pull" now acquires ARMED. The authorization lives one frame down, in
        # `get_scoped_source_capabilities`, and it used to run on a connection
        # that carried no access context -- the exact shape `core/db.py:316-319`
        # names as buying nothing.
        with _core_db.request_connection(identity) as conn:
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
        # The CLASS alone is not a diagnosis: a NameError says nothing about which
        # name. The answer stays the stable public contract; the record carries
        # the message and the traceback.
        logger.warning(
            "get_source_capabilities: unavailable: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
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


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the two cross-connector tools on the given FastMCP app (declared -- AD-43).

    These two are the un-namespaced, core-owned pair AD-2 always meant: they join
    ACROSS connectors and carry no provider in their name. Since AD-42 they are
    also the only place a connector's name appears in an MCP answer -- as data, in
    a payload, never as a tool name.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        list_connectors,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_source_capabilities,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
