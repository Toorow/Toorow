"""toorow MCP server entrypoint.

Instantiates the FastMCP app, mounts auto-discovered modules (AD-2), registers the
core-owned tools (``health``, ``list_connectors``, ``get_daily_report``,
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

import logging
import os
import re
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token

# THESE MODULE OBJECTS ARE PART OF THE SURFACE, not leftovers. The suites patch
# ATTRIBUTES ON THEM through this file -- `core.main.warehouse.query_daily_report`
# (28 sites), `core.main.tracing.is_enabled` (8), `core.main.tracing.current_trace_id_hex`
# (5) -- and patching an attribute of a module object reaches every caller, wherever
# the caller now lives. Removing an import here because THIS file no longer calls it
# breaks those suites without touching a line of behaviour.
from core import branding as branding_module  # noqa: F401 -- patched through `core.main`
from core import confidence as confidence_module  # noqa: F401 -- patched through `core.main`
from core import context_events as context_events_module
from core import envelope as envelope_builder  # noqa: F401 -- patched through `core.main`
from core import (  # noqa: F401 -- patched through `core.main`
    health_enrichment,
    narrative,
    routing,
    summarizer,
    tracing,
    warehouse,
)
from core import mcp_scope as _mcp_scope
from core import metrics as metrics_module
from core import rollup as rollup_module  # noqa: F401 -- patched through `core.main`
from core.auth_config import build_auth_provider
from core.loader import scan_and_load_modules
from core.skill_tool_catalog import configure_skill_tool_provider

# ---------------------------------------------------------------------------
# AD-1 canonical structuredContent envelope.
# Every data-returning tool wraps its payload in this envelope so the shared
# ui/shell can consume `meta` (freshness, provenance, alerts) uniformly.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"  # review-1-5 F-02: single canonical value across all tools


def _envelope(
    data: dict,
    *,
    provenance=None,
    freshness: str | None = None,
    alerts=None,
    ai_settings=None,
) -> dict:
    """Build the canonical AD-1 structuredContent envelope."""
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "freshness": freshness,
            "provenance": provenance,
            "alerts": alerts or [],
        },
        "data": data,
    }
    # Story 75-4: the resolved AI settings AND the scope each value came from.
    # Additive (AD-1): absent rather than defaulted when the store cannot be read.
    if ai_settings:
        envelope["meta"]["ai_settings"] = ai_settings
    return envelope


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
# Scan server/modules/ at import time and validate each manifest. The core is
# source-agnostic (AD-2) — no module-specific names appear here.
#
# THE NAMESPACED MOUNT IS GONE — AD-42, docs/product-architecture/mcp-tool-surface.md.
#
# Until 2026-08-12 this loop ran `mcp.mount(_loaded.connector_module.mcp_app,
# namespace=_loaded.name)`, and FastMCP's namespace transform turned each
# connector's own `get_<provider>_report` into a catalog entry named after the
# provider: `google-ads_get_google_ads_report`, `adobe-analytics_get_adobe_
# analytics_report`, thirty-nine of them. Measured on the assembled catalog they
# were 39 of the 92 tools a default host saw and 13 933 bytes (~3 500 tokens) of
# the 45 106 it paid for before asking anything — and the connector catalogue is
# bounded by nothing, so the count grew with every folder dropped in `modules/`.
#
# What a project collects is now answered by what it HAS: `list_datastreams` and
# `get_datastream_report` (core-owned, parameterized by the Datastream, see
# `core.datastream_report_mcp`). The connectors keep their own `mcp_app` and their
# own report function — verified, `mcp_app` carries `.tool` and nothing else, no
# resource and no prompt — they simply no longer occupy the model's catalog.
#
# Everything else in AD-2 stands: discovery stays global at startup, enablement
# stays per project and stays data, dropping a conforming folder is still the only
# way to add a connector, and only the core joins across modules.
# ---------------------------------------------------------------------------
_MODULES_DIR = Path(__file__).parent.parent / "modules"
_loaded_modules = scan_and_load_modules(_MODULES_DIR)

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

# AI-106: WHEN a Datastream runs is a product setting, not a cron file. The moment
# lives in `app.datastream_schedule_state.next_run_at`, and these two tools are the
# model's door onto it -- the same row the console writes through
# `PATCH /api/datastreams/{id}`. One schedule, three doors, no third truth.
from core.schedule_mcp import register as _register_schedule_mcp  # noqa: E402

_register_schedule_mcp(mcp)

# Story 63.6: a run that can only be stopped from one surface is a run the model
# cannot stop. Same write as the console and the REST route -- one function,
# three doors, one schedule of consequences.
from core.stop_run_mcp import register as _register_stop_run_mcp  # noqa: E402

_register_stop_run_mcp(mcp)

# AI-142: a Datastream can be fully armed -- credential, plan version, mapping
# version, schedule-state row -- and have NO surface that makes it pull.
# `lifecycle_state` has exactly one writer (`publish_activate_mutation`) and it
# requires a Ready candidate; nothing created one for a Datastream born outside
# the setup wizard. These three tools open that one door and write no lifecycle
# field themselves. Registered BEFORE validate_catalog() so the boot validator
# sees the one read and the two confirmed_write declarations.
from core.datastream_first_candidate_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    register as _register_first_candidate,
)

_register_first_candidate(mcp)

# AD-36: the PLATFORM clocks (Cloud Scheduler), as opposed to the per-Datastream
# schedule above. Declared cadence vs what Cloud Scheduler actually holds, the
# drift verdict, and the three writes. Platform allow-list only.
from core.platform_clocks_mcp import register as _register_platform_clocks  # noqa: E402

_register_platform_clocks(mcp)

# Story 36.16: register the recovery-proposal Operations tool (bridge from safe diagnosis
# to the 36.13/36.18 prepare-confirm-commit seams; PREPARE only, no direct write).
from core.recovery_mcp import register as _register_recovery_mcp  # noqa: E402

_register_recovery_mcp(mcp)

# Story 27.9 -- the client label of a canonical dimension, from the model.
# Traced 2026-08-03: four REST routes served it and NOBODY could reach it -- no
# screen called `/api/dimension-lineage/*` and no tool named the verb. This is the
# model's door onto the same two functions the REST handlers call; the console
# door landed 2026-08-24 (`ui/admin/src/governance/DimensionLabelPanel.tsx`).
from core.dimension_labels_mcp import register as _register_dimension_labels  # noqa: E402

_register_dimension_labels(mcp)

# 2026-08-12 -- the matching context, from the model's side. Which Datastream
# NAMES the values of a dimension, and the declaration that makes it so. The
# reference is derived from `data_role` + the canonical target, and until this
# door existed the role was writable only at creation: a join nobody could make.
from core.dimension_reference_mcp import register as _register_dimension_reference  # noqa: E402

_register_dimension_reference(mcp)

# Story 36.15: register the read-only starter-report render/reproduce tools (Insights).
from core.first_report_render_mcp import register as _register_first_report_render  # noqa: E402

_register_first_report_render(mcp)

# Story 40.5: register the brand-registry admin MCP tools (Governance profile) -- the LLM
# surface over the 40.1-40.3 stores (create/curate/wire/confirm/approve), fail-closed +
# human-confirmed. Registered BEFORE validate_catalog() so the boot validator sees them.


# Story 22.22: register the adaptation author-and-self-test MCP tool (Operations, READ).
# Ships an LLM-authored .py + a bounded sample to the isolated sandbox worker (AD-3) and
# returns the mapping result; holds no DB write and leaks no credential to the worker.
# Registered BEFORE validate_catalog() so the boot validator sees its declaration.
from core.adaptation_executor_mcp import register as _register_adaptation_executor  # noqa: E402

_register_adaptation_executor(mcp)

# Story 48.1: register the Project capability MCP surface (Governance profile) -- read,
# prepare, impact preview and confirmed write over the SAME application handlers and
# serializer the Console and REST use, so equivalent requests hash identically. No
# MCP-owned store and no MCP-owned coverage logic. Registered BEFORE validate_catalog()
# so the boot validator sees the read | prepare | confirmed_write declarations.
from core.project_capabilities_mcp import register as _register_project_capabilities  # noqa: E402

_register_project_capabilities(mcp)

# Story 48.2 (tache 7): the SECOND door onto Country master data. Until 2026-08-04
# `run_country_workspace_command` had exactly one caller -- the REST route -- so the
# story's REST/MCP hash-parity proof had nothing to compare against. The tools below
# call that same runner rather than re-deriving anything, which is what makes the
# parity structural instead of asserted.
from core.master_data_mcp import register as _register_master_data  # noqa: E402

_register_master_data(mcp)

# Story 64.6 (AI-232): the same door for a CLIENT-declared object kind. The three
# tools above are Country's verbs -- apply_preset, save_hierarchy, publish -- and
# the generic store they sit on had exactly one caller until Story 64.1. These
# three carry the client's own verbs (declare, release, describe) onto
# `core.object_kind_registry`, with the neighbour's authorization copied verbatim:
# a second door with a softer guard is a way around the first one.
from core.object_kind_mcp import register as _register_object_kind  # noqa: E402

_register_object_kind(mcp)

# Story 68.1: the feeder-less half of the declaration above. The 64.6 tools
# declare a kind BY binding its source; these two declare the type itself --
# name, canonical key, label -- as governed configuration, with the replay and
# the real duplicate told apart (`entity_type_exists`). Same registry, same
# guard, registered BEFORE validate_catalog() for the same reason as above.
from core.entity_types_mcp import register as _register_entity_types  # noqa: E402

_register_entity_types(mcp)

# Story 68.7: the discovery read over those declarations. ONE function serves
# this tool and the console REST route (`object_kind_registry.
# describe_entity_reconciliation_context`): what the Project declares, what
# designates it, which rule-set versions derive it, and -- named unavailable
# until Story 68.3 lands, never zeroed -- the matching coverage.
from core.entity_context_mcp import register as _register_entity_context  # noqa: E402

_register_entity_context(mcp)

# Amendment of 2026-08-17 to alignment-register.md: a fixed FX value is a
# first-class method. The same door shape for a rate a person POSES -- master
# data, versioned and published, not a rule set, so it takes a tool rather than
# the generic capability commands. MCP first and no screen, on the Tax & Fees
# precedent.
from core.fx_fixed_rate_mcp import register as _register_fx_fixed_rate  # noqa: E402

_register_fx_fixed_rate(mcp)

# Story 41.8: register the Tax & Fee MCP surface (Governance profile) -- read the
# governed ladder at its exact version, propose from prequalified presets, and
# prepare a rule change as a Controls & Quality change set. No second control
# authority, no activation flip, no publication. Registered BEFORE
# validate_catalog() so the boot validator sees the two read and two prepare
# declarations.
# Story 50.6: register the Analyze MCP surface -- the data tool (compact answer,
# Result identity, bounded evidence, NO widget), the two app-only bounded slice
# readers, and the render tool that is the ONLY tool allowed to advertise the
# shared Visualization runtime resource. Registered BEFORE validate_catalog() so
# the boot validator sees the app-only and widget-binding declarations; that is
# what makes the data/render split a closed contract instead of a policy.
from core.analyze_render_mcp import register as _register_analyze_render  # noqa: E402

_register_analyze_render(mcp)

# Story 55.2: the `mcp_app` surface of `app.evidence_inspections` gets its writer.
# App-only and `effect="read"` -- an append-only observation is the AD-28 audit row
# every read must leave, not a domain mutation; migration 177 records why, and
# tests/conformance/test_app_only_observation_writer.py proves the slot accepts it.
# Registered here, BEFORE validate_catalog(), for the same reason as the line above.
from core.evidence_inspection_mcp import register as _register_evidence_inspection  # noqa: E402

_register_evidence_inspection(mcp)

# Amendment of 2026-08-17 to `first-figure-path.md`: the first-publication gesture
# lands on the web share AND the MCP app, "so an agent can read the same
# first-publication state the Overview shows". One Insights read-only tool,
# composed by the SAME function the console's overview endpoint calls.
# Registered here, BEFORE validate_catalog(), for the same reason as the lines above.
from core.project_posture_mcp import register as _register_project_posture  # noqa: E402

_register_project_posture(mcp)

# Chantier 67-23 -- the four MCP plans the audit of 2026-08-17 found missing. Each
# one is a door onto a surface the console already composed and an agent could not
# read, and each composes through the SAME function the screen calls rather than a
# second derivation. Registered here, BEFORE validate_catalog(), for the same
# reason as the lines above.
#
#   * the Data lenses (audit 04): nothing called `compose_data_surface`, so
#     Imports, Sources and Connectors had no projection at all -- Operations
#     profile, the family that already answers the state of collection;
#   * the Test door (audit 09): "le plan MCP du Test est vide" -- two Insights
#     reads over the evaluation runs and over context adherence. TRIGGERING a run
#     stays console/API-only and is named as such in the module;
#   * the metric_grain total authority (audit 03): the resolver's refusal named a
#     declaration no tool could even read. The READ lands; the declaration stays
#     the governed console gesture;
#   * the Context Hub remark (audit 08): an agent read `open_remarks` and could
#     not deposit one, while `context-hub.md` says a model that finds a Skill
#     stale can flag it.
# Story 75-1: the PROMOTION door -- an agent proposes an exploration calculation
# in the same review rail a human uses; a human resolution prepares the change-set.
from core.calculated_field_proposals_mcp import register as _register_calculated_field  # noqa: E402
from core.context_remark_mcp import register as _register_context_remark  # noqa: E402
from core.data_surface_mcp import register as _register_data_surface  # noqa: E402
from core.evaluation_mcp import register as _register_evaluation  # noqa: E402

# Les deux portes que l'audit de gap du 2026-09-05 a ouvertes sur le Test.
# DEFINITION : aucun outil ne creait, ne versionnait ni ne retirait une Golden
# Question, donc un modele pouvait etre juge par une et jamais en ecrire une.
# REVIEW : un modele pouvait laisser une reaction sur une figure qu'il avait
# produite et n'en jamais relire une, donc la boucle que l'ecran Widget Feedback
# existe pour fermer restait ouverte sur le plan pour lequel le produit est bati.
from core.evidence_chain_mcp import register as _register_evidence_chain  # noqa: E402
from core.feedback_review_mcp import register as _register_feedback_review  # noqa: E402
from core.golden_question_mcp import register as _register_golden_question  # noqa: E402
from core.metric_grain_mcp import register as _register_metric_grain  # noqa: E402
from core.project_access_mcp import register as _register_project_access  # noqa: E402

_register_data_surface(mcp)
_register_evaluation(mcp)
_register_golden_question(mcp)
_register_feedback_review(mcp)
_register_evidence_chain(mcp)
_register_project_access(mcp)
_register_metric_grain(mcp)
_register_context_remark(mcp)
_register_calculated_field(mcp)

# Chantier 67-24 -- the language family gets its reader. `language_dimensions`
# declares three dimensions sharing one word (observed / asset property /
# declared intent) that must never be summed or compared with each other, and its
# whole binding lifecycle had ZERO production callers when the audit measured it:
# an agent could not learn which of the three a column carries, and would narrate
# the gap between an intent and an observation as an error to fix. Insights and
# read-only -- declaring a binding is a human act on the project REST surface.
# Registered here, BEFORE validate_catalog(), for the same reason as the lines above.
from core.language_bindings_mcp import register as _register_language_bindings  # noqa: E402

_register_language_bindings(mcp)

# Story 45.2: expose the assembled MCP declarations to the Context Hub skill
# editor without importing this application module from the REST layer.
configure_skill_tool_provider(mcp.list_tools)


# Story 36.11: install the capability-profile middleware so every list_tools/call_tool
# is filtered by the caller's authenticated profile scope. High-risk profiles are opt-in
# only; since AD-43 an undeclared tool is visible to nobody rather than defaulting to
# Insights.
#
# THE CATALOG VALIDATOR NO LONGER RUNS HERE. It ran on this line, and nine `register()`
# hooks run BELOW it — reporting, report, cards, daily insight, inbound, flows, context
# hub, feedback, notebook, agent surface, data quality. So the boot validator, and with
# it the Story 50.6 data/render split it was passed `mcp` to enforce, only ever saw a
# PARTIAL catalog: the tools registered after this line were validated by nothing. The
# call now sits after the last registration site; the middleware install stays here,
# because what matters for it is its position in the middleware chain (before the AI
# Path recorder), not the state of the registry at the moment it is constructed.
from core.mcp_profiles import build_middleware as _build_capability_middleware  # noqa: E402
from core.mcp_profiles import validate_catalog as _validate_capability_catalog  # noqa: E402

_capability_mw = _build_capability_middleware()
if _capability_mw is not None:
    mcp.add_middleware(_capability_mw)

# Story 49.6 — AI Path recording. `core.ai_paths` and migration 150 shipped the
# whole owner and nothing ever called it, so `context-hub.md`'s last criterion
# ("AI usage paths and their evidence cannot be inspected or evaluated") stayed
# open behind a complete implementation. This is its caller.
#
# Registered AFTER the capability middleware on purpose: a call the profile gate
# refuses never reaches here, so a refused tool does not become a step of a path
# it was never allowed to join.
from core.ai_path_recorder import build_middleware as _build_ai_path_middleware  # noqa: E402

_ai_path_mw = _build_ai_path_middleware()
if _ai_path_mw is not None:
    mcp.add_middleware(_ai_path_mw)


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
# Core-owned `ui://core/*` resources (Story 1.6, Epic 9, Story 48.1).
#
# The eleven widget resources were declared here with `@mcp.resource`; their
# bodies now live in `core.widget_resources`, which binds them all in one
# `register(mcp)` -- the shape `inbound_mcp` and the twenty `*_mcp` modules
# above already use. The names are re-exported below because `core.main` is the
# address the tests and the admin API have always imported them from.
# ---------------------------------------------------------------------------
from core import widget_resources as _widget_resources  # noqa: E402
from core.widget_resources import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _WIDGET_PATH,
    _WIDGETS_DIR,
    DAILY_REPORT_WIDGET_URI,
    _resolve_widget_dist,
    _serve_card_widget,
    card_attribution_widget,
    card_connectors_widget,
    card_conversions_widget,
    card_dedup_widget,
    card_journey_widget,
    card_keywords_widget,
    card_kpi_widget,
    card_usertypes_widget,
    daily_report_widget,
    project_capability_impact_app,
)

_widget_resources.register(mcp)


# ---------------------------------------------------------------------------
# Shared Visualization runtime resource (Story 50.5, AD-2 / AD-11).
#
# One core-owned bundle, identical for every connector, organization and host.
# The URI literal and the bytes live in `visualization_runtime_resource`; this is
# the single registration site, so the same URI is never bound twice.
# ---------------------------------------------------------------------------
from core import visualization_runtime_resource as _visualization_runtime  # noqa: E402

_visualization_runtime.register(mcp)

# ---------------------------------------------------------------------------
# Core-owned cross-connector tools: `list_connectors` and
# `get_source_capabilities` (AD-2, T3.3). The two bodies and the profile
# sanitizer live in `core.connectors_mcp`; this is the single registration site.
# ---------------------------------------------------------------------------
from core import connectors_mcp as _connectors_mcp  # noqa: E402
from core.connectors_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _public_profile_summaries,
    get_source_capabilities,
    list_connectors,
)

_connectors_mcp.register(mcp)


def health(project_id: str = "default") -> dict:
    """Liveness/readiness probe for the connector MCP server.

    AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3). The
    ``project_id`` parameter scaffold is intentional — the identity parameter
    flows through every tool path from P0, it is never bolted on at P6.

    THE ONLY PROJECT-SHAPED TOOL THAT DELIBERATELY RESOLVES NO ACCESS, and this
    line is the decision rather than the omission (Story 53.1). Two reasons, and
    both have to hold: the ``project_id`` here is a parameter scaffold that is
    echoed back and never used to read a row -- so there is nothing to leak --
    and a readiness probe that asked the database whether the caller may see a
    project would report ``unavailable`` for the exact outage it exists to
    report. Guarding it would make the probe depend on what it probes. If this
    tool ever reads project-scoped data, the guard comes with that change.

    Returns the canonical AD-1 envelope as ``structuredContent``. NOTE:
    FastMCP does NOT synthesize a text channel from this docstring — a dict
    return surfaces as ``structuredContent`` only. That is fine for a
    liveness probe, but data-returning tools (Story 1.5+) MUST explicitly
    return BOTH channels (lean text summary + envelope) — see the
    dual-channel pattern in ``server/core/README.md``. Copying this
    single-channel shape for a data tool would push the full dataset into
    the LLM context and violate NFR1.

    Story 3.3 (AC5): ``data.quota`` is an array of per-platform breaker states,
    one entry per registered platform. Empty array when no connectors with quota
    blocks are loaded.

    Story 4.4 (AC9): ``data.mirror_sync`` is the last sync result from
    mirror_sync.py. Null if the process has never synced (just started).
    Shape: {"last_synced_at": str, "lag_seconds": float,
            "tables": {"context_events": N, ...}} or null.
    """
    from core import mirror_sync as _mirror_sync  # noqa: PLC0415
    from core import quota as _quota  # noqa: PLC0415
    from core.ai_path_recorder import recording_failures  # noqa: PLC0415

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
            # How many observations this instance failed to record, and why it
            # belongs HERE rather than only beside the path list.
            #
            # `ai_path_recorder` catches every failure by design -- evidence
            # collection must not take down the observed. The cost of that
            # correct refusal was measured on 2026-08-04: two structural
            # defects made `search_context` record NOTHING for a day while the
            # owner, the migration, the routes and 128 tests were green. The
            # only witness was a log line nobody reads.
            #
            # The path list already carries this count, because that is where a
            # reader confuses "nothing happened" with "nothing could be
            # written". It is repeated here because a screen nobody opens is
            # not a signal: `quota` and `mirror_sync` are the two other
            # process-local operational facts this envelope exists to carry,
            # and a recorder that has stopped recording is exactly one of those.
            #
            # Not persisted, deliberately: a counter that needed a write would
            # fail exactly when recording fails. It counts per INSTANCE and its
            # key says so.
            "ai_path_recording": {"failures_this_instance": recording_failures()},
        },
        provenance={"source_system": "connector-core", "source_field": "health", "pull_id": None},
        freshness="live",
    )


# Register `health` as an MCP tool. Defined as a plain function above (so tests
# and other core code can call it directly) and registered here — @mcp.tool
# would otherwise replace the name with a Tool object. Declared (AD-43): a
# process-local liveness read, `public` because it carries no tenant datum.
#
# Imported under its own name, not an alias: `test_mcp_tools_resolve_project_scope`
# recognises a registration by the CALLED NAME `register_profiled`, and an alias
# here would make `health` look like a tool nobody registers -- which reads as a
# repaired guard rather than a blind spot.
from core.mcp_profiles import register_profiled  # noqa: E402

register_profiled(
    mcp,
    health,
    profile="insights",
    effect="read",
    data_class="public",
    confirmation_mode="none",
)


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


def _resolve_project(project_id: str | None, identity: str | None = None) -> str:
    """Resolve *project_id* for the caller who named it — existence AND access.

    Story 7.1 AC5 for the fallback + validation half; the arbitration ratified by
    Jean on 2026-08-25 for the access half, written in
    `docs/product-architecture/mcp-tool-surface.md`, "naming a project you may
    not see answers `project_not_found`".

    WHAT THIS FUNCTION USED TO ANSWER, AND WHY THAT WAS THE ORACLE. It decided
    "does this project exist" on an UNARMED connection and took **no identity at
    all**. So it answered "it exists" to anyone who could spell the id, and the
    access decision — when the tool made one — arrived afterwards, separately.
    Comparing two refusals therefore taught a caller which project ids are real:
    an enumeration oracle, one function above every guarded tool.

    THE ANSWER IS ONE ENVELOPE, AND THE ORDER IS DELIBERATE.

    1. ``identity`` is the caller. The tool passes the subject its business read
       will use, so the armed connection and the resolved access cannot be about
       two different people; when it passes nothing, the identity is read from
       the call by ``mcp_scope.caller_identity()``. There is no third way to name
       a caller here.
    2. Existence is resolved on an ARMED connection
       (``core.db.request_connection``), so the Epic-36 floor is under the very
       question being asked instead of beside it.
    3. Access is resolved through the ONE seam, ``refuse_unless_project_scope``,
       at the ``view`` floor. It fails CLOSED, and denied / absent / unavailable
       raise the identical ``project_not_found`` envelope.

    THE TOOL'S OWN GUARD IS NOT NOW REDUNDANT. This function can only demand
    ``view``: it does not know whether its caller is about to read or to write.
    The RANK belongs to the tool — a write demands ``edit`` — so the downstream
    ``refuse_unless_project_scope(..., minimum_capability=...)`` answers a
    different question, and removing it would let a ``view`` holder write in the
    neighbour's project.

    The resilience posture of the EXISTENCE half is unchanged and still
    deliberate: a DB-less unit-test mock, or an unreachable database, passes the
    value through rather than failing the hot path. It is not a way in — the
    access half below runs whatever happened above, and it fails closed.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import mcp_scope as _scope  # noqa: PLC0415
    from core.project_resolver import (  # noqa: PLC0415
        _LEGACY_DEFAULT_SENTINEL,
        resolve_project_id,
    )

    raw = (project_id or "").strip()
    is_default_bind = raw == "" or raw == _LEGACY_DEFAULT_SENTINEL
    who = (identity or "").strip() or _scope.caller_identity()

    try:
        with _core_db.request_connection(who) as _conn:
            resolved = resolve_project_id(project_id, _conn)
    except ToolError:
        # AC5: an EXPLICIT, named project that is missing/archived must surface
        # the error to the caller. The default auto-bind ('' / 'default') is the
        # resilient path: at P3-dev the seeded 'default' row always exists, so a
        # raise here means the connection is a DB-less unit-test mock that does
        # not model app.projects -> pass the sentinel through rather than fail.
        if not is_default_bind:
            raise
        resolved = _LEGACY_DEFAULT_SENTINEL
    except Exception as _exc:  # pragma: no cover - resilience path
        logger.debug("resolve_project: passthrough (db unavailable): %s", _exc)
        resolved = raw or _LEGACY_DEFAULT_SENTINEL

    # THE ACCESS HALF, AND IT RUNS WHATEVER HAPPENED ABOVE. Putting it inside the
    # `try` would have made "the database is down" the cheapest way to reach the
    # neighbour's project -- the exact fail-open `core/mcp_scope.py` refuses in
    # its property 2. The seam is called rather than copied: a second access
    # decision in this file would be a second answer to "who may see this".
    _scope.refuse_unless_project_scope(resolved, who)
    return resolved


# ---------------------------------------------------------------------------
# Stories 1.4 / 6.1 -- `get_daily_report` and `get_report`. The two bodies and
# their three private helpers live in `core.reporting_mcp`; this is the single
# registration site.
#
# `_load_project_geographic_posture` and `_apply_pre_query_gate` are re-exported
# because `core.notebook_mcp` and the suites read them from here (the card tools
# import the gate from `core.reporting_mcp` directly since 2026-09-01, so the
# append census can see their adherence write).
# ---------------------------------------------------------------------------
from core import report_mcp as _report_mcp  # noqa: E402
from core import reporting_mcp as _reporting_mcp  # noqa: E402
from core.report_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _load_project_geographic_posture,
    get_report,
)
from core.reporting_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _apply_pre_query_gate,
    _validate_date_range,
    get_daily_report,
)

_reporting_mcp.register(mcp)
_report_mcp.register(mcp)


# ---------------------------------------------------------------------------
# Epic 9 -- the card library: `list_card_templates` and `get_card`. The two
# bodies and the project topic catalogue live in `core.cards_mcp`; this is the
# single registration site. `_project_topic_catalog` is re-exported because
# `core.daily_insight_mcp` reads it from here.
# ---------------------------------------------------------------------------
from core import cards_mcp as _cards_mcp  # noqa: E402
from core.cards_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _project_topic_catalog,
    get_card,
    list_card_templates,
)

_cards_mcp.register(mcp)


# ---------------------------------------------------------------------------
# Epic 10 -- the Daily Insight surface: readiness, capabilities, preview and
# publish. The four bodies and their five helpers live in
# `core.daily_insight_mcp`; this is the single registration site.
# ---------------------------------------------------------------------------
from core import daily_insight_mcp as _daily_insight_mcp  # noqa: E402
from core.daily_insight_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _daily_insight_scope,
    _publishable_card_templates,
    _render_daily_insight_snapshot,
    _resolve_daily_insight_inputs,
    _resolve_evidence_refs,
    get_card_capabilities,
    get_daily_insight_readiness,
    preview_daily_insight,
    publish_daily_insights,
)

_daily_insight_mcp.register(mcp)


# ---------------------------------------------------------------------------
# Story 38.15 -- inbound delivery operations from MCP. Four tools: three reads
# and one PREPARATION. There is deliberately no commit tool: a reprocess can
# move the published pointer, so it needs a confirmation this channel cannot
# verify, and the safest guard on a commit tool is not having one. Every tool
# calls the same function the REST handler calls (core.inbound_health /
# core.inbound_reprocess), so the two surfaces cannot drift.
# ---------------------------------------------------------------------------
from core.inbound_mcp import register_inbound_tools as _register_inbound_tools  # noqa: E402

_register_inbound_tools(mcp)


# ---------------------------------------------------------------------------
# Story 8.7 — MCP flow interface: flows_list / flows_get / flows_validate /
# flows_upsert. The four bodies live in `core.flows_mcp`; this is the single
# registration site. `core.flows_api` mirrors them over REST calling the SAME
# `core.flows` functions, so the two surfaces cannot drift.
#
# Re-exported below because `from core.main import flows_upsert` is the address
# the suites and the REST seam have always used.
# ---------------------------------------------------------------------------
from core import flows_mcp as _flows_mcp  # noqa: E402
from core.flows_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _flow_identity,
    flows_get,
    flows_list,
    flows_upsert,
    flows_validate,
)

_flows_mcp.register(mcp)


# ---------------------------------------------------------------------------
# Story 4.3 / Epic 11 — the Context Hub surface: events, knowledge, skills and
# the hub read. The seven bodies live in `core.context_hub_mcp`; this is the
# single registration site.
#
# The two context-event aliases STAY here: `core.context_events` owns the
# functions, `core.main` is the address nine suites patch them at, and the tools
# import them from here at call time.
# ---------------------------------------------------------------------------
_validate_event_input = context_events_module.validate_event_input
_fetch_context_events = context_events_module.fetch_context_events

from core import context_hub_mcp as _context_hub_mcp  # noqa: E402
from core.context_hub_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _discovery_scope,
    _discovery_summary,
    add_context_event,
    add_knowledge,
    add_skill,
    get_context_hub,
    get_events,
    get_knowledge,
    get_skills,
)

_context_hub_mcp.register(mcp)


# ---------------------------------------------------------------------------
# Story 5.5 -- `submit_feedback`. The body and its in-memory rate limit live in
# `core.feedback_mcp`; this is the single registration site. Twelve suites
# import the tool from `core.main`, so it is re-exported.
# ---------------------------------------------------------------------------
from core import feedback_mcp as _feedback_mcp  # noqa: E402
from core.feedback_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _feedback_calls,
    _feedback_rate_limited,
    submit_feedback,
)

_feedback_mcp.register(mcp)

# ---------------------------------------------------------------------------
# Story 6.5 — save_notebook / run_notebook. The bodies live in
# `core.notebook_mcp`; this is the single registration site. Since the AC12
# cutover (857f1580) the scheduler runs canonical notebooks through the
# governed service and imports nothing from here; `run_notebook_direct` is
# re-exported below only because test suites still patch it at this address.
# ---------------------------------------------------------------------------
# Story 53.1 -- le seam de portee vit desormais dans `core.mcp_scope`, parce que
# quatre autres modules de `core/` enregistrent des outils scopes projet et
# n'allaient pas importer un nom prive de ce fichier-ci. L'alias garde les treize
# sites d'appel de `main.py` inchanges, et il reste le nom que la garde
# structurelle reconnait.
_refuse_unless_project_scope = _mcp_scope.refuse_unless_project_scope

from core import notebook_mcp as _notebook_mcp  # noqa: E402
from core.notebook_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _ENVELOPE_INLINE_MAX_BYTES,
    _assert_project_access,
    run_notebook,
    run_notebook_direct,
    save_notebook,
)

_notebook_mcp.register(mcp)

# ---------------------------------------------------------------------------
# Epic 74 (story 74-1) -- `compose_dossier`, the fourth of Jean's five steps:
# a model KEEPS the figures it asked for and the commentary it wrote, as one
# Dossier. Declared like `save_notebook`; the budget of the catalogue moved from
# 130 to 131 for it, by decision (`mcp-tool-surface.md`, note of 2026-09-02).
# ---------------------------------------------------------------------------
from core import dossier_mcp as _dossier_mcp  # noqa: E402

_dossier_mcp.register(mcp)

# ---------------------------------------------------------------------------
# Story 67.23 -- la porte MCP des Reports GOUVERNES. Elle n'existait pas :
# `grep -rl "analysis_report" server/core/*mcp*.py` rendait 0, alors que
# << MCP is the second door of the same product >> est ratifie pour la surface
# Analyze. Un modele pouvait executer un Notebook et pas un Report.
# ---------------------------------------------------------------------------
from core import analysis_report_mcp as _analysis_report_mcp  # noqa: E402

_analysis_report_mcp.register(mcp)

# ---------------------------------------------------------------------------
# Story 67.23 -- la porte MCP du PARCOURS GOUVERNE de creation d un Datastream.
# `grep -rn "datastream_setup_drafts" server/core/*_mcp.py` rendait vide : dix-neuf
# routes REST, zero outil. Un modele pouvait operer, publier, planifier -- mais pas
# CREER par le parcours gouverne. Cinq verbes, et la ceremonie de confirmation
# reste entiere (le secret ne voyage pas jusqu au modele).
# ---------------------------------------------------------------------------
from core import datastream_wizard_mcp as _datastream_wizard_mcp  # noqa: E402

_datastream_wizard_mcp.register(mcp)

# ---------------------------------------------------------------------------
# Core-owned agent-surface tools: search_context, get_procedure and
# resolve_business_path (Story 11.5). The three bodies and their two private
# helpers live in `core.agent_surface_mcp`; this is the single registration site.
#
# `_current_identity` is re-exported because `health` above reads it.
# ---------------------------------------------------------------------------
from core import agent_surface_mcp as _agent_surface_mcp  # noqa: E402
from core.agent_surface_mcp import (  # noqa: E402,F401 -- re-exported: `core.main` is the address
    _SEARCH_CONTEXT_MAX_LINES,
    _build_search_context_summary,
    _current_identity,
    _mark_context_consulted,
    get_procedure,
    resolve_business_path,
    search_context,
)

_agent_surface_mcp.register(mcp)

# Story 75-3: approved exemplars (active golden questions, approved observed
# walks) of a metric, a view or a topic -- pinned in version, cut at a budget.
from core import exemplars_mcp as _exemplars_mcp  # noqa: E402

_exemplars_mcp.register(mcp)

# ---------------------------------------------------------------------------
# Story 13.4 -- `get_data_quality_report`. The body lives in
# `core.data_quality_mcp`; this is the single registration site.
# ---------------------------------------------------------------------------
from core import data_quality_mcp as _data_quality_mcp  # noqa: E402
from core.data_quality_mcp import get_data_quality_report  # noqa: E402,F401 -- re-exported

_data_quality_mcp.register(mcp)

# ---------------------------------------------------------------------------
# AD-42 -- what a project collects, named by its Datastreams and by nothing else.
# The replacement for the thirty-nine provider-named tools the namespaced mount
# used to publish (see the module auto-discovery block near the top of this file).
# ---------------------------------------------------------------------------
from core import datastream_report_mcp as _datastream_report_mcp  # noqa: E402

_datastream_report_mcp.register(mcp)


# ---------------------------------------------------------------------------
# THE LAST REGISTRATION SITE IS ABOVE. Validate the assembled catalog here.
#
# Three invariants, all fail-closed at boot, all driven off the LIVE catalog so a
# tool added next week is inside them by construction:
#   * every declaration is valid and internally consistent (Story 36.11);
#   * at most one tool advertises a widget resource, and it is the render tool
#     (Story 50.6, the data/render split);
#   * every assembled tool carries a declaration at all (AD-43).
# Moved here from just after the module-discovery block, where eleven register()
# hooks still ran below it and were therefore validated by nothing.
# ---------------------------------------------------------------------------
_validate_capability_catalog(mcp)


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


def configure_logging() -> int:
    """Attach a handler to the ROOT logger, and return the level it was set to.

    WHY THIS EXISTS. `uvicorn.run(log_level="info")` configures uvicorn's OWN
    three loggers and nothing else: its dictConfig names `uvicorn`,
    `uvicorn.access` and `uvicorn.error`, each with `propagate: False`. The root
    logger keeps no handler, so every `logger.info(...)` in `core.*` is handled by
    `logging.lastResort` -- which emits at WARNING. INFO and DEBUG are dropped
    before they reach stdout, and Cloud Run never sees them.

    Measured on the deployed service, 2026-08-08: over two days of logs, zero
    lines matching `scheduler: ` or `dq_monitors: `, while `toorow-run-dq-monitors`
    answered 200 every fifteen minutes and psycopg tracebacks (ERROR) came through
    intact. The clocks fired, did their work, and left nothing -- which is the
    exact failure `infra/gcp/provision_ad36_substrate.sh:73-75` was written
    against ("a sleeping thread that does not fire leaves NOTHING, and
    missed_run_count stays at 0, which reads exactly like health"), reproduced one
    level up: the tick fires, returns 200, and says nothing about what it did.

    `TOOROW_LOG_LEVEL` names the level; INFO by default, because the question this
    answers -- did the scheduled work run, and on what -- is asked of every
    deployment, not only of a debugging one. An unknown name falls back to INFO
    rather than raising: a malformed level must not stop the server from booting.

    `force=True` so a library that already called `basicConfig` at import time
    cannot leave the root logger pinned to its own choice.
    """
    import sys  # noqa: PLC0415

    requested = os.environ.get("TOOROW_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, requested, None)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(levelname)s %(name)s %(message)s",
        force=True,
    )
    return level


def main() -> None:
    """Start the server over streamable HTTP, binding 0.0.0.0:$PORT."""
    import uvicorn

    configure_logging()

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
