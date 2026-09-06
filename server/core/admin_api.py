"""toorow -- Admin API router (Story 2.4, T5.4; Story 2.5, T3; Story 3.2).

Starlette Router with routes:
  GET  /api/connections              -- list connections (Nango + connection_ref + health join)
  POST /api/connections              -- create a connection_ref row
  POST /api/connections/<id>/refresh-health -- on-demand health refresh for one connection
  POST /api/connections/<id>/pull    -- enqueue a pull job (Story 3.2, returns 202)
  GET  /api/jobs/<id>                -- get pull job status (Story 3.2, AC5)

Design decisions (recorded per Dev Notes):
  - Postgres client: psycopg v3 (sync) via core.db.get_connection().
    Chosen over asyncpg: sync is simpler in Starlette sync handlers and
    consistent with audit.py. No asyncio.run() nesting risk.
  - ULID: python-ulid (already in venv). Prefix: conn_ per ARCHITECTURE-SPINE.
  - Nango: list_connections_async preferred from async Starlette handlers
    (avoids _run_coro thread overhead in the async path).
  - Auth: follows Story 2.3 pattern -- disabled mode passes through;
    static/oauth modes VERIFY the Bearer token via core.api_auth (review-2-6 F-01).
  - Story 2.6 integration point: identity is extracted from the Bearer token
    header so audit rows can be written with the correct subject.
  - Story 2.5: health is served from the Postgres cache (connection_health table)
    NOT by calling Nango on every GET request (no per-request Nango polling).
  - Story 3.2: _trigger_pull now enqueues via core.queue.enqueue_pull (returns 202).
    The pull_id is minted inside enqueue_pull (AD-7). Audit row written there too.

AD-3: no token columns written to Postgres.
AD-8: admin console communicates exclusively through this API (no direct DB).
Windows/CI note (L-3): all log strings use ASCII-safe characters only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Router

from core.ai_paths_api import AI_PATH_ROUTES

# AD-43 : ici un prefixe EST un sujet -- la regle dit de lire, pas que tout
# prefixe ment.
# AD-43 : six sujets de plus. `/internal` en est un a lui seul -- son
# autorisation n'est pas celle des portes humaines.
# Story 75-4: the AI settings of a Project and of an Organization.
from core.ai_settings_api import AI_SETTINGS_ROUTES as _AI_SETTINGS_ROUTES
from core.alert_definitions_api import (
    ALERT_DEFINITIONS_ROUTES_1,
)
from core.alert_destinations_api import (
    ALERT_DESTINATIONS_ROUTES_1,
)
from core.analyze_artifacts_api import ROUTES as _ANALYZE_ARTIFACT_ROUTES
from core.analyze_workbench_api import ANALYZE_WORKBENCH_ROUTES
from core.answerable_topics_api import answerable_topic_routes
from core.audit import (
    ACTION_CROSS_SCOPE_ATTEMPT,
    ACTION_DATASTREAM_UPDATED,
    write_audit_row,
)
from core.browser_oidc import BROWSER_AUTH_ROUTES as _BROWSER_AUTH_ROUTES
from core.business_taxonomy_api import (
    BUSINESS_TAXONOMY_ROUTES as _BUSINESS_TAXONOMY_ROUTES,
)

# Story 75-1: an exploration calculation proposed, reviewed, prepared as a change-set.
from core.calculated_field_proposals_api import calculated_field_proposal_routes
from core.cards_api import CARDS_ROUTES as _CARDS_ROUTES
from core.catalog_api import (
    CATALOG_ROUTES_1,
    CATALOG_ROUTES_2,
)
from core.chart_templates_api import chart_template_routes
from core.cleanup_rules_api import CLEANUP_RULE_ROUTES as _CLEANUP_RULE_ROUTES
from core.conflict_resolutions_api import CONFLICT_RESOLUTION_ROUTES as _CONFLICT_RESOLUTION_ROUTES
from core.connections_api import (
    CONNECTIONS_ROUTES_1,
    CONNECTIONS_ROUTES_2,
)

# Story 38.5: connector activation/deactivation (org-owner) + health layering.
from core.connector_activation_api import (
    CONNECTOR_ACTIVATION_ROUTES as _CONNECTOR_ACTIVATION_ROUTES,
)

# Story 38.3: connector domain and adapter-route configuration (platform-admin).
from core.connector_domain_api import (
    CONNECTOR_DOMAIN_ROUTES as _CONNECTOR_DOMAIN_ROUTES,
)

# Story 38.2: connector installation state surface (platform-admin + catalog gate).
from core.connector_installation_api import (
    CONNECTOR_INSTALLATION_ROUTES as _CONNECTOR_INSTALLATION_ROUTES,
)

# Story 38.4: connector verification + synthetic test delivery (platform-admin).
from core.connector_verification_api import (
    CONNECTOR_VERIFICATION_ROUTES as _CONNECTOR_VERIFICATION_ROUTES,
)
from core.context_api import CONTEXT_ROUTES as _CONTEXT_ROUTES
from core.context_events_api import CONTEXT_EVENTS_ROUTES_1
from core.controls_quality_api import CONTROLS_QUALITY_ROUTES as _CONTROLS_QUALITY_ROUTES
from core.credential_accounts_api import (
    CREDENTIAL_ACCOUNTS_ROUTES_1,
)
from core.daily_insights_api import DAILY_INSIGHTS_ROUTES as _DAILY_INSIGHTS_ROUTES
from core.data_surface_api import DATA_SURFACE_ROUTES
from core.datamodel_api import DATAMODEL_ROUTES as _DATAMODEL_ROUTES

# AD-43 : les quatre sujets de `/api/organizations` ont leur module.
from core.dataset_access_api import (
    DATASET_ACCESS_ROUTES_1,
    # Story 62.3: the SAME saga, scoped to the published marts of one Project.
    PROJECT_DATASET_ACCESS_ROUTES,
)

# AD-40: the managed-feed import surface left this file. Two collections, in the
# order their routes were declared here -- ledger first, then upload/contract/sync.
from core.datastream_collection_api import (
    DATASTREAM_COLLECTION_ROUTES,
    DATASTREAM_SCHEDULE_ROUTES,
    # RE-EXPORTED ON PURPOSE, and this line is why it is not "an unused import".
    # `REFETCH_ROUTE_PATH` is THE address of the re-collection, declared once so a
    # test asserts the mounted path against the constant the router uses rather
    # than against a second copy typed into the test. Callers already know it at
    # `core.admin_api`; the extraction moved where it lives, not where it is read.
    REFETCH_ROUTE_PATH,  # noqa: F401
)

# Story 58.1: a Datastream read by DAY, project-scoped in the path.
from core.datastream_daily_breakdown_api import datastream_daily_breakdown_routes
from core.datastream_executions_api import DATASTREAM_EXECUTION_ROUTES
from core.datastream_mapping_api import DATASTREAM_MAPPING_ROUTES
from core.datastream_matches_api import (
    DATASTREAM_MATCH_ROUTES as _DATASTREAM_MATCH_ROUTES,
)
from core.datastream_preconfiguration_api import datastream_preconfiguration_routes

# Story 63.2: the one route of the product that is called in a loop.
from core.datastream_progress_api import datastream_progress_routes
from core.datastream_recovery_api import DATASTREAM_RECOVERY_ROUTES

# AD-43 : les quatre sujets de `/api/projects` ont leur module. Un prefixe
# d'URL n'est pas une responsabilite -- l'ordre de resolution, lui, l'est.
from core.datastream_sample_api import (
    DATASTREAM_SAMPLE_ROUTES_1,
)
from core.datastream_setup_templates_api import datastream_setup_templates_routes
from core.datastream_workbench_api import datastream_workbench_routes
from core.datastreams_api import (
    DATASTREAM_OBJECT_ROUTES,
    DATASTREAM_OBJECT_TAIL_ROUTES,
)

# Story 27.9: inverse lineage ("what feeds this dimension?") + client-owned labels.
from core.dimension_lineage_api import (
    DIMENSION_LINEAGE_ROUTES as _DIMENSION_LINEAGE_ROUTES,
)
from core.dossier_api import DOSSIER_ROUTES as _DOSSIER_ROUTES
from core.entity_context_api import ENTITY_CONTEXT_ROUTES as _ENTITY_CONTEXT_ROUTES
from core.entity_detail_gaps_api import entity_detail_gaps_routes
from core.entity_types_api import ENTITY_TYPE_ROUTES as _ENTITY_TYPE_ROUTES
from core.entry_api import (
    ENTRY_ROUTES_1,
    ENTRY_ROUTES_2,
)
from core.evaluation_runs_api import evaluation_run_routes
from core.event_stream_arming_api import event_stream_arming_routes

# Story 38.14: layered connector health + the per-attachment import inbox. Until
# these mounted, inbound receipts and scan verdicts were WRITTEN AND READ BY
# NOBODY -- the only reader anywhere was a join from the import ledger, which by
# construction could only show the deliveries that had already succeeded.
from core.facts_api import FACT_ROUTES as _FACT_ROUTES
from core.feedback_regression_api import feedback_regression_routes
from core.feedback_review_api import feedback_review_routes
from core.file_import_api import FILE_IMPORT_ROUTES
from core.file_source_template_api import file_source_template_routes
from core.first_value_api import (
    FIRST_VALUE_ROUTES_1,
)
from core.flows_api import FLOWS_ROUTES as _FLOWS_ROUTES
from core.flux_projects_api import FLUX_PROJECTS_ROUTES_1
from core.getting_started_api import getting_started_routes

# Epic 51: product evaluation and improvement evidence. Four owners, four route
# lists, mounted here because a handler no route exposes is not delivered.
from core.golden_questions_api import golden_question_routes
from core.google_oauth_api import (
    GOOGLE_OAUTH_ROUTES_1,
)
from core.governance_surface_api import GOVERNANCE_SURFACE_ROUTES

# Story 38.6: import template catalog + inbound managed-feed Datastream creation.
from core.import_templates_api import (
    IMPORT_TEMPLATE_ROUTES as _IMPORT_TEMPLATE_ROUTES,
)

# Story 38.7: inbound delivery credential lifecycle (issue/rotate/revoke/list).
from core.inbound_credentials_api import (
    INBOUND_CREDENTIAL_ROUTES as _INBOUND_CREDENTIAL_ROUTES,
)
from core.inbound_health_api import (
    INBOUND_HEALTH_ROUTES as _INBOUND_HEALTH_ROUTES,
)

# Story 38.18: reprocess a retained raw file without asking the sender to resend.
from core.inbound_reprocess_api import (
    INBOUND_REPROCESS_ROUTES as _INBOUND_REPROCESS_ROUTES,
)
from core.instance_claim_api import (
    # Le nom du cookie d'echange est encore lu par le reste d'admin_api ; son
    # proprietaire est le sujet, et ce module l'importe deja pour ses routes.
    INSTANCE_CLAIM_ROUTES_1,
)
from core.internal_api import (
    INTERNAL_ROUTES_1,
)
from core.invitations_api import (
    INVITATIONS_ARRIVAL_ROUTES,
    INVITATIONS_ROUTES_1,
    INVITATIONS_ROUTES_2,
)
from core.jobs_api import JOBS_ROUTES_1
from core.language_bindings_api import (
    LANGUAGE_BINDINGS_ROUTES as _LANGUAGE_BINDINGS_ROUTES,
)
from core.legacy_evidence_api import LEGACY_EVIDENCE_ROUTES
from core.managed_feed_imports_api import MANAGED_FEED_IMPORT_ROUTES

# Story 62.3: what a published mart promises an outside reader.
from core.mart_contract import MART_CONTRACT_ROUTES as _MART_CONTRACT_ROUTES
from core.mcp_hosts_api import (
    MCP_HOSTS_ROUTES_1,
)
from core.mdm_canonical_fields_api import (
    MDM_CANONICAL_FIELD_ROUTES as _MDM_CANONICAL_FIELD_ROUTES,
)
from core.mdm_common_keys_api import MDM_COMMON_KEY_ROUTES as _MDM_COMMON_KEY_ROUTES
from core.me_api import (
    ME_ROUTES_1,
    ME_ROUTES_2,
)
from core.mediaplan_api import MEDIAPLAN_ROUTES as _MEDIAPLAN_ROUTES
from core.metric_dimensions_api import (
    MDM_METRIC_DIMENSION_ROUTES as _MDM_METRIC_DIMENSION_ROUTES,
)
from core.metric_grain_api import metric_grain_routes
from core.metric_semantics_api import METRIC_SEMANTICS_ROUTES as _METRIC_SEMANTICS_ROUTES
from core.mmm_export_api import MMM_EXPORT_ROUTES as _MMM_EXPORT_ROUTES
from core.multi_source_api import MULTI_SOURCE_ROUTES as _MULTI_SOURCE_ROUTES
from core.notebooks_api import (
    NOTEBOOKS_ROUTES_1,
    NOTEBOOKS_ROUTES_2,
)

# AD-43 : la logique metier a quitte ce fichier de routes (critere 4). Ce qui
# reste ici de non monte est le JOINT d'authentification et de portee, et lui
# seul -- autoriser EST le travail d'un module de routes.
from core.org_lifecycle import (
    _would_orphan_last_owner,
)
from core.org_members_api import (
    # Deux noms encore lus par le reste d'admin_api ; leur proprietaire est le
    # sujet, et ce module l'importe deja pour ses routes -- pas de cycle.
    ORG_MEMBERS_ROUTES_1,
)

# Story 34.3: org-plan control surface routes (isolated import to keep the edit
# surgical -- appended as its own single-line block, not spliced into the sorted
# route-import group above).
from core.org_plan_api import ORG_PLAN_ROUTES as _ORG_PLAN_ROUTES
from core.organizations_api import (
    ORGANIZATIONS_ROUTES_1,
    ORGANIZATIONS_ROUTES_2,
)
from core.pivot_api import PIVOT_ROUTES as _PIVOT_ROUTES
from core.planned_actual_export_api import (
    PLANNED_ACTUAL_EXPORT_ROUTES as _PLANNED_ACTUAL_EXPORT_ROUTES,
)
from core.platform_clocks_api import PLATFORM_CLOCK_ROUTES as _PLATFORM_CLOCK_ROUTES
from core.platform_maintenance_api import (
    PLATFORM_MAINTENANCE_ROUTES_1,
    PLATFORM_MAINTENANCE_ROUTES_2,
    PLATFORM_MAINTENANCE_ROUTES_3,
)

# Story 75-6: presentation extends -- a display block on the scope cascade.
from core.presentation_extends_api import (
    PRESENTATION_EXTENDS_ROUTES as _PRESENTATION_EXTENDS_ROUTES,
)
from core.project_access_api import project_access_routes
from core.project_connections_api import (
    PROJECT_CONNECTIONS_ROUTES_1,
    PROJECT_CONNECTIONS_ROUTES_2,
)
from core.project_overview_api import project_overview_routes
from core.project_settings_api import project_settings_routes
from core.projects_api import (
    # Le motif de slug a suivi son proprietaire ; `_create_org` le lit encore,
    # et ce module importe deja `projects_api` pour ses routes -- pas de cycle.
    PROJECTS_ROUTES_1,
)
from core.publication_reviews_api import PUBLICATION_REVIEWS_ROUTES_1
from core.query_specs_api import query_spec_routes
from core.reference_vocabulary_api import COUNTRY_VOCABULARY_ROUTES
from core.reference_vocabulary_api import (
    REFERENCE_VOCABULARY_ROUTES as _REFERENCE_VOCABULARY_ROUTES,
)
from core.render_shares_api import render_share_routes, share_notebook_gone
from core.render_shares_console_api import render_share_console_routes
from core.rendus_api import RENDUS_ROUTES as _RENDUS_ROUTES
from core.report_chain import REPORT_CHAIN_ROUTES as _REPORT_CHAIN_ROUTES
from core.rule_versions_api import RULE_VERSION_ROUTES as _RULE_VERSION_ROUTES
from core.schema_context_api import SCHEMA_CONTEXT_ROUTES as _SCHEMA_CONTEXT_ROUTES

# Story 49.3: the Semantic Model change-set lifecycle. Reads stay on the
# Governance surface above; this is the only consequential command family.
from core.semantic_model_api import SEMANTIC_MODEL_ROUTES
from core.source_delegations_api import (
    SOURCE_DELEGATIONS_ROUTES_1,
    SOURCE_DELEGATIONS_ROUTES_2,
)
from core.trace_observation_api import trace_observation_routes
from core.unresolved_values_api import (
    UNRESOLVED_VALUES_ROUTES as _UNRESOLVED_VALUES_ROUTES,
)
from core.value_mapping_api import VALUE_MAPPING_ROUTES as _VALUE_MAPPING_ROUTES
from core.visualization_specs_api import visualization_spec_routes

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.


# AD-43 : `org_lifecycle` l'ecrit aussi depuis que l'effacement d'organisation
# a quitte ce fichier. Deux ecrivains -> le centre, comme AD-42 le prescrit.



logger = logging.getLogger(__name__)




# ---------------------------------------------------------------------------
# ULID helper
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Auth helper (reuses Story 2.3 pattern from _audit_endpoint in main.py)
# ---------------------------------------------------------------------------


async def _check_canonical_principal(request: Request):
    # ONE door (2026-08-30): this used to open its own connection and then ask
    # who was calling, so the entry, claim, invitation and project-settings
    # routes kept paying a connection per anonymous probe -- and answered 500
    # with the database down -- after `authenticate_api_request` had stopped.
    from core.api_auth import resolve_request_principal  # noqa: PLC0415

    return await resolve_request_principal(request)


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Real token verification via the shared api_auth layer (review-2-6 F-01).

    ONE DOOR. This forwards to `api_auth.authenticate_api_request` and adds
    nothing, because there is nothing left to add: that function resolves the
    canonical ``person_<ULID>`` itself since the identity flag was removed
    (2026-08-24), which is the only key `app.org_members` and the strict access
    seam are keyed on.

    It did not always forward. While the flag existed this helper tested it
    FIRST and, when set, called `_check_canonical_principal` directly -- jumping
    over `authenticate_api_request` and, with it, over the `TOOROW_AUTH_MODE=
    disabled` short-circuit that function owns. Two consequences, both
    invisible while every environment that mattered ran the legacy path: the
    documented anonymous local-development mode answered 401 through this helper
    while answering "anonymous" through the other, and
    `_strict_project_capability_allowed`'s explicit `disabled` + `anonymous`
    branch below became unreachable. Restoring the single door restores both.
    """
    from core.api_auth import authenticate_api_request  # noqa: PLC0415

    return await authenticate_api_request(request)


def _strict_project_capability_allowed(
    conn,
    *,
    identity: str,
    project_id: str,
    minimum_capability: str,
    hold_access: bool = False,
    include_archived_project: bool = False,
) -> bool:
    """Authorize one app project surface through the org-rooted strict seam.

    Auth-disabled local mode deliberately preserves the historical anonymous
    developer workflow. Every authenticated deployment uses the canonical
    explicit-grant resolver and therefore has no default-open membership path.

    The bypass grants every CAPABILITY; it does not conjure the project. A
    project id that names no active row is refused in both modes, so a typo
    answers 404 in `make dev` exactly as it does under OAuth instead of the
    200-with-an-empty-list that made an unknown project look like a new one
    (live finding C1).
    """
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if auth_mode == "disabled" and identity == "anonymous":
        from core.project_access import project_exists  # noqa: PLC0415

        return project_exists(project_id, conn, include_archived=include_archived_project)

    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    return resolve_strict_resource_access(
        identity,
        conn,
        project_id=project_id,
        minimum_capability=minimum_capability,
        auth_mode=auth_mode,
        hold_access=hold_access,
        include_archived_project=include_archived_project,
    ).allowed


def _project_not_found_response() -> JSONResponse:
    """Return the shared non-disclosing project authorization refusal."""
    return JSONResponse(
        {"code": "not_found", "message": "Project not found"},
        status_code=404,
    )


def _refuse_unless_project_allowed(
    identity: str,
    project_id: str,
    minimum_capability: str,
    surface: str,
    include_archived_project: bool = False,
) -> JSONResponse | None:
    """Return a 404 unless *identity* holds *minimum_capability* on *project_id*.

    AI-171. Six handlers repeated the same eight lines around
    ``_strict_project_capability_allowed`` -- open a connection, ask, translate a
    refusal AND a lookup failure into the same non-disclosing 404. Six copies is
    how five of them came to be written without the gate at all: the motif was
    long enough to skip. Written once, it is one call to add.

    Existence-hiding is deliberate and matches ``_list_feedback``: a project the
    caller may not see must not be distinguishable from one that does not exist.
    A failure to REACH the access seam is refused too -- an authorization that
    cannot be evaluated is not an authorization granted.
    """
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability=minimum_capability,
                include_archived_project=include_archived_project,
            )
    except Exception as exc:  # noqa: BLE001 -- refuse on an unreachable seam
        logger.warning(
            "admin_api: %s access unavailable project=%s: %s",
            surface,
            project_id,
            type(exc).__name__,
        )
        return _project_not_found_response()
    if not allowed:
        return _project_not_found_response()
    return None


async def _check_invitation_identity(request: Request) -> tuple[bool, str]:
    """Require an OAuth-verified email identity for invitation transitions."""
    from core.api_auth import authenticate_invitation_identity

    return await authenticate_invitation_identity(request)


async def _check_invitation_principal(request: Request):
    """Resolve the invited EMAIL and the canonical person behind it, together.

    The third member of the tuple is never None for an authorized caller: an
    invitation transition binds a person, and binding it on the email alone is
    the legacy shape this seam no longer has.
    """
    ok, principal = await _check_canonical_principal(request)
    if not ok or principal is None or not principal.verified_email:
        return False, "", None
    return True, principal.verified_email, principal


# ---------------------------------------------------------------------------
# Story 7.4 (AC4, AC7) -- notebook project-scope enforcement (AI-38 fix).
#
# The admin API uses a single shared Bearer token (API_SECRET_KEY): any holder
# is "the admin" and, until now, could PATCH / SCHEDULE / EXPORT ANY project's
# notebook by id (review-epic-6 F-2). This helper closes that gap on every write
# path: it fetches the notebook's project_id, verifies caller access via the
# per-identity ACL (active Organization membership plus exact resource grants;
# single-tenant), and — critically — treats a scope violation as 404 so the
# endpoint does not even confirm the notebook's existence to a non-member. Every
# rejection is AUDITED (ACTION_CROSS_SCOPE_ATTEMPT) so refused access is
# observable (FR12, AD-5, AD-8).
#
# Callers pass the notebook's project_id (already fetched, so no second SELECT).
# When an explicit project_id scope hint is supplied (query param or body), a
# mismatch is ALSO a 404 — this is what the isolation suite exercises: a caller
# claiming project_alpha must never touch project_beta's notebook.
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


























# ---------------------------------------------------------------------------
# Story 25.5: account topology onboarding endpoints (discovery / selection /
# backfill). Logic lives in core.account_topology; these handlers mirror the
# neighbouring /api/connections* auth, AD-5 project-scoping and error shapes.
#
# Audit action for a verified account selection. Defined locally (audit.py is a
# shared, append-only registry; a literal action string is accepted by
# write_audit_row's free-form action column -- no migration needed).
# ---------------------------------------------------------------------------























def _google_oidc_caller_is_ours(request: Request) -> bool:
    """True when the caller presents a Google OIDC id token minted for this service.

    Story 56.7. Cloud Scheduler and Cloud Tasks can attach one; a Pub/Sub push
    subscription can attach ONLY this -- it has no way to send `X-Internal-Auth`.
    So the substrate's third component would have been unreachable without it.

    What is verified, and why each part matters:
      * the signature, against Google's keys -- a token nobody could forge;
      * the audience, against `INTERNAL_OIDC_AUDIENCE` (defaulting to the service
        URL Cloud Tasks already targets) -- a token minted for ANOTHER service
        must not open this one;
      * the issuer, which the library pins to Google.

    Absent or invalid means "not this mechanism", never "denied": the caller
    still gets the shared-secret and user-token paths. A malformed token is not
    an attack to report, it is a request that authenticates some other way.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    audience = (
        os.environ.get("INTERNAL_OIDC_AUDIENCE", "").strip()
        or os.environ.get("CLOUD_TASKS_WORKER_URL", "").strip()
    )
    if not audience:
        return False
    try:
        from google.auth.transport import requests as google_requests  # noqa: PLC0415
        from google.oauth2 import id_token as google_id_token  # noqa: PLC0415

        claims = google_id_token.verify_oauth2_token(
            header.split(" ", 1)[1].strip(),
            google_requests.Request(),
            audience=audience,
        )
    except Exception:  # noqa: BLE001 -- see docstring: not this mechanism
        return False

    # An identity Google vouches for is not automatically OURS. When the expected
    # service account is declared, a token from any other Google principal --
    # including another customer's -- is refused.
    expected = os.environ.get("INTERNAL_OIDC_SERVICE_ACCOUNT", "").strip()
    if expected and claims.get("email") != expected:
        logger.warning(
            "admin_api: oidc_wrong_principal path=%s email=%s",
            request.url.path,
            claims.get("email"),
        )
        return False
    return True


async def _authorize_internal(request: Request) -> Response | None:
    """Authorize a platform-to-platform call. Returns None when authorized.

    STORY 56.5, AND IT FIXES A CONTRADICTION THAT WOULD HAVE 401'd EVERY TASK.
    The internal endpoints used to require BOTH the shared-secret header AND
    ``_check_auth`` -- a user Bearer token. Cloud Scheduler and Cloud Tasks do
    not carry one: they are the platform calling itself, not a person. So the
    two mechanisms are alternatives, not a conjunction:

      * the shared secret matches -> authorized, this IS the platform;
      * otherwise -> a human may still call it with a valid Bearer token, which
        is how these endpoints are exercised by hand.

    FAIL-CLOSED WHERE IT MATTERS. Under QUEUE_BACKEND=cloud_tasks the secret is
    MANDATORY: without it no task could ever authenticate, and the endpoints
    would answer 401 to every delivery -- a misconfiguration that reads exactly
    like a broken queue. Answering 503 instead makes Cloud Tasks come back once
    an operator sets it, so the work waits rather than dies.

    AI-127 -- THE HUMAN BRANCH WAS NAKED, AND IT GUARDS EIGHT ENDPOINTS.
    Breaking the conjunction was right; what it left behind was ``_check_auth``
    ALONE, so any authenticated principal, from any organization, could fire
    ``dispatch-nightly``, ``reconcile-queues``, ``drain-outbox``,
    ``poll-health``, ``run-dq-monitors``, ``dispatch-hourly``,
    ``execute-pull/{job_id}`` and ``execute-activation/{job_id}``. The action
    item named four; the code has eight. Those are not reads: they start work at
    PLATFORM scale, and two of them execute a named job. The mismatched secret
    was AUDITED and then waved through, which reads as a guard and is not one.

    So the human branch now costs what it always should have: a platform role,
    via the same ``TOOROW_SUPER_ADMINS`` allow-list every other no-organization
    act in this file uses (``_enforce_platform_admin``). Deliberately NOT a new
    notion of "platform operator" -- a second vocabulary for the same idea is
    how the two drift apart.

    THE TWO MACHINE BRANCHES ARE UNTOUCHED, and that is the point: Cloud
    Scheduler and Cloud Tasks return above on OIDC or on the secret, so the 56.8
    cutover is not re-opened by this. Refusal is 404, not 403, matching
    ``_enforce_platform_admin``: we do not confirm the surface to a caller who
    is not allow-listed.

    Deny-by-default follows from the allow-list: with ``TOOROW_SUPER_ADMINS``
    unset, NO human can call these by hand. That is the correct posture for an
    endpoint whose other two callers are machines -- an operator who needs one
    either joins the allow-list or presents the internal secret.

    Phase B note: an OIDC token minted for this service is the stronger form and
    the one the original TODO named. It is not implemented here -- what IS fixed
    is the conjunction that made the endpoints unreachable by their own callers.
    """
    # A Google-issued OIDC id token is the STRONGER form, and for Pub/Sub push it
    # is the ONLY one: a push subscription cannot send a custom header, so the
    # shared secret is unreachable there. Checked first because a caller that
    # presents one is the platform proving its identity cryptographically rather
    # than by knowing a string.
    if _google_oidc_caller_is_ours(request):
        return None

    required_secret = os.environ.get("INTERNAL_ENDPOINTS_REQUIRE_HEADER", "")
    if required_secret:
        if request.headers.get("x-internal-auth", "") == required_secret:
            return None
        logger.warning(
            "admin_api: internal_auth_rejected path=%s -- falling back to user auth",
            request.url.path,
        )
        write_audit_row(
            identity="anonymous",
            action=ACTION_CROSS_SCOPE_ATTEMPT,
            provider_account="",
            connection_ref="",
            metadata={"path": str(request.url.path), "reason": "internal_auth_mismatch"},
        )
    elif os.environ.get("QUEUE_BACKEND", "local") == "cloud_tasks":
        logger.error(
            "admin_api: internal_secret_missing path=%s -- QUEUE_BACKEND=cloud_tasks "
            "requires INTERNAL_ENDPOINTS_REQUIRE_HEADER, no task can authenticate",
            request.url.path,
        )
        return JSONResponse(
            {
                "code": "internal_auth_unconfigured",
                "message": "INTERNAL_ENDPOINTS_REQUIRE_HEADER must be set when "
                "QUEUE_BACKEND=cloud_tasks",
            },
            status_code=503,
        )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token or internal secret required"},
            status_code=401,
        )
    # AI-127: authenticated is not authorized. A valid Bearer says WHO, not
    # WHETHER, and everything below this line is platform-scale work.
    return await _enforce_platform_admin(
        request, identity, f"internal:{request.url.path}"
    )



















# ---------------------------------------------------------------------------
# Story 4.3 — Context events REST handlers (AC4)
#
# POST /api/context-events -- create a context event from the admin console.
# GET  /api/context-events -- list events with project_id + optional date filters.
#
# Validation logic (_validate_event_input) is shared with the MCP tool in main.py
# via a local import to avoid a circular dependency (main imports admin_api at startup
# via build_asgi_app; admin_api cannot import from main). The admin_api has its own
# inline validation that mirrors the MCP tool's logic exactly.
#
# AD-8: admin console communicates exclusively through this REST API.
# HG-2: widget writes via callServerTool (add_context_event MCP tool) — never here.
# AD-2: no module-specific strings.
# ---------------------------------------------------------------------------













# ---------------------------------------------------------------------------
# Story 4.4 (AC8) — POST /api/mirror/sync (manual mirror sync trigger)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Story 5.2 (AC4) — GET /api/health (REST proxy for the MCP health tool)
#
# The admin console PipelinePanel fetches this endpoint to get circuit breaker
# states (data.quota) and mirror sync lag (data.mirror_sync).
#
# The health() function is defined in core/main.py as an MCP tool. We import
# it here and call it directly -- the result shape is identical to the MCP
# structuredContent response.
#
# Auth-guarded (same pattern as all other admin API endpoints).
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Story 5.3 (AC5) -- /api/alert-definitions CRUD
#
# GET    /api/alert-definitions?project_id=<id>  -- list definitions
# POST   /api/alert-definitions                  -- create definition
# PATCH  /api/alert-definitions/{id}             -- toggle enabled / update threshold
# DELETE /api/alert-definitions/{id}             -- soft-delete (enabled=false)
#
# All routes guarded by api_auth (same pattern as above).
# AD-8: admin console communicates exclusively through this REST API.
# AD-2: no module-specific strings.
# ---------------------------------------------------------------------------
















# ---------------------------------------------------------------------------
# Story 59.6 -- /api/alert-destinations CRUD + test send
#
# GET    /api/alert-destinations?project_id=<id>   -- list, MASKED, never sealed
# POST   /api/alert-destinations                   -- create
# PATCH  /api/alert-destinations/{id}              -- update
# DELETE /api/alert-destinations/{id}              -- remove
# POST   /api/alert-destinations/{id}/test         -- send a test, write NO firing
#
# The word is `Alert destination` and it is posed once: the table, the route, the
# component and the test all carry it. `Channel` and `Webhook` are already
# ratified for the INPUT side of a Datastream (`glossary.md:161-163`, `:355`) and
# "one word, one meaning" is the rule story 59.5 just spent itself enforcing.
#
# A secret NEVER leaves through a read: `include_secret` is answered
# `secret_is_write_only`, and the list renders `target_masked` because a Slack
# incoming-webhook URL is itself a credential.
# ---------------------------------------------------------------------------
















# ---------------------------------------------------------------------------
# Story 5.5 (AC7) — GET /api/feedback (queryable feedback store)
#
# Lists feedback rows for a project, optionally filtered by module.
# Auth-guarded by api_auth (same pattern as all other admin API endpoints).
# AD-8: admin console communicates via this REST API.
# Privacy: created_by is NOT returned (kept server-side; audit log has it).
# FR10: queryable per report/module via project_id + module filters.
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# GET /api/tracked-entities was removed by Story 48.5.
#
# It returned every entity with `"bindings": []` hard-coded -- a field the
# response promised and never filled, on a route whose only caller was an
# unmounted page. The registry is now read through the governed Master Data
# collection (`/api/projects/{project_id}/governance/master-data` with
# `lens=competitor-registry`), which composes identities, Project roles,
# governed source representations and the real per-Datastream binding states
# from their owners.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GET /api/procedures -- RETIREE LE 2026-08-05 (AI-172), ET C'EST UN RETRAIT.
#
# Le handler `_list_procedures` lisait `app.metric_procedures` (migration 095),
# l'un des sept magasins que la migration 145 (story 49.4) a remplaces par la
# famille gouvernee `metric_reconciliation` de `governance_rule_sets`. L'en-tete
# de 145 dit ce qui restait a faire et pourquoi elle ne l'a pas fait elle-meme :
# « They stop being AUTHORITIES in this story; the DROP belongs to the commit
# that removes the last reader. » Ce lecteur etait le dernier -- `grep -rn
# metric_procedures server` ne rendait que ce fichier.
#
# CE N'EST PAS LE MENAGE D'UN SYMBOLE QUI SEMBLE MORT. Trois mesures, dans cet
# ordre, avant de retirer quoi que ce soit :
#   1. la surface qui remplace celle-ci EXISTE et est routee -- lens
#      `reconciliation` de `governance/controls-quality`, servie par
#      `governance_read_model._reconciliation_lens` ;
#   2. `app.metric_procedures` compte 0 ligne en production le 2026-08-05, six
#      jours apres la mesure de 145 ;
#   3. les deux vocabulaires de methodes DIFFERENT (`sum_then_divide...` ici,
#      `SUM|PRIORITY|DEDUP_ID|ESTIMATE|KEEP_SEPARATE` dans la famille gouvernee),
#      donc garder cette route revenait a tenir un second magasin, un second
#      vocabulaire et un second ecran pour une seule chose -- ce que le
#      « Incomplete if » de docs/product-architecture/governance.md interdit.
#
# L'absence est TENUE, pas seulement faite : `tests/core/test_metric_procedures_scope.py`
# echoue si la route est remontee, sur le motif retenu pour
# `_create_datastream_mapping_version`. Un retrait silencieux se fait remonter
# par le lecteur suivant, qui n'a aucun moyen de savoir qu'il rouvre une porte.
#
# La garde de portee posee par AI-125 sur ce handler n'est pas perdue : elle est
# devenue `_refuse_unless_project_allowed`, que six autres routes appellent
# (AI-171).
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Story 6.1 (AC9) -- report management endpoints
#
# GET   /api/reports/available?project_id=<id>
#         -> all reports available for the project, merged from the module
#            registry (LoadedModule.reports) and app.project_reports. A report not
#            in project_reports is returned with enabled=false (opt-in default).
# PATCH /api/reports/{project_id}/{connector_name}/{report_id}
#         -> upsert the app.project_reports row (INSERT ... ON CONFLICT DO UPDATE).
#
# Both project-scoped and api_auth-guarded. AD-8: admin console -> REST API only.
# AD-2: no module-specific strings; the report catalog comes from the loader.
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Story 7.2 (AC7) -- Connector enablement endpoints
#
# GET  /api/connectors/available?project_id=<id>
#        -> all installed Connectors with per-project enablement state.
# PATCH /api/connectors/{project_id}/{connector_name}
#        -> upsert enablement in app.project_modules.
#
# Vocabulary (docs/product-architecture/glossary.md): the object is a
# CONNECTOR. "module" survives only as the on-disk path and the not-yet
# migrated column names of app.project_modules.
#
# Both guarded by api_auth; project-scoped. AD-8: admin console -> REST only.
# AD-2: no module-specific strings; catalog from LoadedModule registry.
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Story 6.5 (AC5) -- Notebooks CRUD + run trigger
#
# GET    /api/notebooks?project_id=<id>       -- list notebooks for project
# GET    /api/notebooks/{notebook_id}          -- single notebook + last 5 runs
# PATCH  /api/notebooks/{notebook_id}          -- update title/window_rule/narrative_prompt
# DELETE /api/notebooks/{notebook_id}          -- delete notebook (cascades runs)
# POST   /api/notebooks/{notebook_id}/run      -- trigger a run (calls run_notebook logic)
#
# All project-scoped (never return another project's notebooks). api_auth-guarded.
# AD-8: admin console communicates exclusively through this REST API.
# ---------------------------------------------------------------------------














# ---------------------------------------------------------------------------
# Story 6.6 — Schedule endpoint (AC2)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Story 6.6 -- Share token endpoints (AC3): RETIRED by Story 50.7
# ---------------------------------------------------------------------------
#
# `_share_notebook`, `_shared_notebook_endpoint` and the `token[:8]` rate limiter
# stood here. They are deleted rather than left unmounted: an unmounted handler
# body is exactly the artefact that got a route remounted once already in this
# repository (SESSIONS.md, "Desaccord CLOS: _create_datastream_mapping_version"),
# because the next reader sees a capability with no door and supplies one.
#
# What replaced them, and why the replacement is not the same shape:
#   * the raw path token `/api/notebooks/shared/{token}` is GONE -- no route
#     matches it, which is what AC10 requires. A 410 there would still be a route
#     answering on a plaintext bearer carried in a URL path, and it served the
#     LATEST run, so the artefact behind a link changed under its recipient.
#   * `PATCH /api/notebooks/{notebook_id}/share` stays MOUNTED and answers 410
#     through `core.render_shares_api.share_notebook_gone`. Deleting the entry
#     would answer 405, which is a different statement and indistinguishable to a
#     client from a routing regression.
#   * Sharing is now one revocable grant over one immutable Render, exchanged
#     once through the AD-30 fragment flow (`core.render_shares_api`).


# ---------------------------------------------------------------------------
# Story 6.6 — Slide export: HTML endpoint (AC5)
# ---------------------------------------------------------------------------







# ===========================================================================
# Story 18.2 -- Google server-side OAuth flow (authorize + callback).
#
# AD-14/AD-15: the OAuth flow lives in the admin console (never the chat iframe).
# The authorize endpoint requires an authenticated admin AND per-identity project
# access (core.project_access) BEFORE minting the consent URL. The callback
# verifies the anti-CSRF state, exchanges the code, persists the encrypted token
# via the Story 18.1 store (single writer -- AD-8/AD-21), and writes an emission
# audit row (AD-14 On-Behalf-Of). NO token or authorization code ever appears in
# a log, an error, or a redirect URL (NFR3).
# ===========================================================================










# ===========================================================================
# Story 18.4 -- Console: etat Google et revocation.
#
# GET  /api/google/oauth/status/{connection_ref_id}
#      Retourne l'etat courant de la connexion Google directe pour une
#      connexion donnee : auth_path, scopes accordes, expiry, sante derivee.
#      JAMAIS le blob chiffre (NFR3). AD-5 : verifie l'acces projet.
#
# POST /api/google/oauth/revoke/{connection_ref_id}
#      Revoke le token cote Google (best-effort), purge le blob chiffre en
#      local (clear_google_token) et ecrit un audit On-Behalf-Of AD-14.
#      Idempotent : une connexion deja claire repond 200 sans erreur.
#      performed_by = identite REELLE du Bearer token (jamais 'system').
#
# AD-5 : acces projet verifie avant toute operation.
# AD-14 : audit On-Behalf-Of pour emission ET revocation (18.2 + 18.4).
# NFR3 : aucun token, aucun blob en reponse.
# AI-56 : seam ASGI teste avec assertions sur les valeurs.
# ===========================================================================












# ---------------------------------------------------------------------------
# Router (exported for mounting in build_asgi_app)
# ---------------------------------------------------------------------------

# ===========================================================================
# Story 21.1 -- Organization CRUD + membership (Epic 21, FR37/CAP-25).
#
# app.organizations is the tenant created in migration 035; app.org_members maps
# identity -> org -> role. These endpoints are the single config surface for the
# org layer (AD-15). FOUNDATION ONLY: no access-resolution change here (the
# org-level default-closed flip is Story 21.5). All guarded by _check_auth.
# ===========================================================================


# Roles that can manage the org (own the "active manager" floor an enrolled org
# must never drop below -- else it reaches zero active members and reopens).
_ORG_MANAGE_ROLES = frozenset({"owner", "admin"})











def _enforce_org_manage(org_id: str, identity: str, conn, operation: str) -> Response | None:
    """Story 21.5: gate a MUTATION behind owner/admin of *org_id* on the SAME conn.

    Under default-open-until-enrolled (human decision) an org with ZERO members is
    OPEN -> resolve_org_role() returns "owner" -> this passes (keeps 21.1-21.4
    green). An ENROLLED org (>= 1 member) is CLOSED: a non owner/admin gets a 403
    and an ACTION_CROSS_SCOPE_ATTEMPT audit row (reuse of the _google_revoke denial
    pattern). Returns the 403 Response on refusal, or None when allowed.
    """
    from core.project_access import identity_can_manage_org  # noqa: PLC0415

    if identity_can_manage_org(org_id, identity or "anonymous", conn):
        return None
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "operation": operation, "reason": "not_org_manager"},
    )
    logger.warning(
        "admin_api: org_manage_denied identity=%s org=%s op=%s",
        identity,
        org_id,
        operation,
    )
    return JSONResponse(
        {
            "code": "forbidden",
            "message": "Acces refuse : droits owner/admin requis sur l'organisation.",
        },
        status_code=403,
    )


async def _enforce_platform_admin(
    request: Request, identity: str, operation: str
) -> Response | None:
    """Gate an act that has NO organization in scope behind the platform allow-list.

    An ENTRY invitation (``org_id`` absent) is issued after a waitlist approval:
    there is no organization to be a manager of, so ``_enforce_org_manage`` has
    nothing to check. Its issuer is a PLATFORM admin -- the same deny-by-default
    ``TOOROW_SUPER_ADMINS`` allow-list the CRM control surface uses (story 34.3).

    Resolution is delegated to ``core.super_admin.identity_is_super_admin`` --
    THE one answer to "who is a super-admin" (audit 12, P1-2). It resolves the
    bearer subject to its verified email through ``app.person_identities``; the
    OAuth-verified email carried by the token is handed over as an ``extra`` key
    because it is a stronger source than the registry, not a second resolution.
    Refusal is a 404, not a 403: we do not reveal that this surface exists to a
    caller who is not allow-listed. Returns the refusal Response, or None when
    allowed.

    This changes NOTHING for invitations that name an organization: they keep the
    membership check.
    """
    from core.super_admin import identity_is_super_admin  # noqa: PLC0415

    _, verified_email = await _check_invitation_identity(request)
    if identity_is_super_admin(identity, extra=(verified_email,)):
        return None
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={"operation": operation, "reason": "not_platform_admin"},
    )
    logger.warning("admin_api: platform_admin_denied identity=%s op=%s", identity, operation)
    return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)

































# ===========================================================================
# RGPD deletion path -- preview, org erasure core, account erasure.
#
# Three things must tell the SAME story: what the preview announces, what the
# DELETE actually removes, and what the audit ledger records afterwards. They
# therefore read the same facts helper below -- a preview that undercounts is
# worse than no preview at all, because the user consents to the wrong thing.
# ===========================================================================























# Back-compat alias for callers/tests that still use the earlier helper name.
# Its behavior now follows the ratified owner-only floor.
_would_orphan_last_manager = _would_orphan_last_owner






# ===========================================================================
# Story 21.2 -- Global user profile (self-service). Keyed on the AD-14 identity
# subject; a user reads/writes ONLY their own profile. Avatars come from here or
# the identity provider -- NEVER from the Google DATA callback (no identity scope).
# ===========================================================================






# ===========================================================================
# RGPD account erasure (DELETE /api/me) -- the personal counterpart of the org
# drop above. Two promises are kept here and stated out loud in the payload:
#   * an organization is NEVER left orphaned: being the last owner of an org
#     that still has other active members is a REFUSAL (409), not something the
#     endpoint silently resolves by promoting somebody or abandoning the tenant;
#   * an org that belongs to nobody but the caller LEAVES WITH THEM -- otherwise
#     its warehouse datasets survive, unowned and billed, which is exactly the
#     orphan state this whole path exists to prevent.
# ===========================================================================









# ===========================================================================
# Story 21.3 -- Credential ownership + per-account cross-org grants.
#
# The "credential" is app.connection_ref. An org OWNS a credential; it SHARES an
# external account to another org one account at a time (credential_account_grants).
# Structural isolation: a grant targets a (credential_id, external_account_id) that
# must exist in credential_accounts -- you cannot grant a whole credential. The
# per-identity "who may expose" enforcement is Story 21.5; here we gate on
# _check_auth only (consistent with 21.1/21.2 AC5).
# ===========================================================================




def _enforce_credential_org_read(credential_id: str, identity: str, conn) -> Response | None:
    """Story 21.5 follow-up (reads scoping): None when the caller may read this
    credential's resources, else a 404 Response.

    404 if the credential is absent OR owned by an org the caller cannot see. A
    NULL owner_org_id (legacy un-owned credential) is treated as open (compat).
    """
    from core.project_access import identity_has_org_access  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute("SELECT owner_org_id FROM app.connection_ref WHERE id = %s", (credential_id,))
        row = cur.fetchone()
    if row is None:
        return JSONResponse(
            {"code": "not_found", "message": "credential not found"}, status_code=404
        )
    owner_org = row[0]
    if owner_org is not None and not identity_has_org_access(
        owner_org, identity or "anonymous", conn
    ):
        return JSONResponse(
            {"code": "not_found", "message": "credential not found"}, status_code=404
        )
    return None












def _invitation_no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
























# ===========================================================================
# Story 24.5 -- Dataset marts access grants (Epic 24, P4).
#
# Lets an owner/admin expose the org's BigQuery marts dataset to an external
# IAM principal (serviceAccount/user/group).  The grant is ALWAYS scoped to
# ``org_<wslug>_marts`` -- never to raw nor mirror_*.  Soft-delete on revoke
# (revoked_at set, row kept for RGPD).  Audited on every mutation.
# ===========================================================================

#: Valid IAM principal type prefixes (BigQuery member syntax).
_VALID_IAM_TYPES = frozenset({"user", "serviceAccount", "group"})













# ===========================================================================
# Story 21.4 -- Flux org-scoped + linkable to MANY projects (Epic 21, FR37/CAP-25).
#
# The "flux" is app.datastreams. A flux is OWNED by an org (datastreams.org_id)
# and LINKED to N projects of that SAME org via app.project_flux (M:N). The
# cross-org link is refused with a friendly 409 (code "cross_org") AND backstopped
# structurally by the composite FKs to projects(org_id, id) / datastreams(org_id,
# id) that share org_id. The per-identity "who may link a flux" enforcement is
# Story 21.5; here we gate on _check_auth only (consistent with 21.1/21.3 AC5).
# ===========================================================================








# ===========================================================================
# Story 7.1 -- Project CRUD (AC3, AC4).
#
# app.projects is the anchor table created in migration 018. These endpoints are
# the single config surface for creating / listing / updating / archiving
# projects (AD-15). All guarded by _check_auth. Auth subject -> created_by.
# ===========================================================================

# The Project reporting currency, from the ONE vocabulary the product owns.
#
# This was a hand-written set of SEVEN codes with "extend as needed" beside it,
# while Project Settings offers the whole ISO 4217 list through
# `/api/reference/currencies` -- `ProjectSettings.tsx:173`, "a searchable
# validated ISO 4217 list". So a project could be given a currency at
# settings-time that its own creation route refuses, and the two disagreed by
# 166 codes (7 against 173 loaded by `load_currency_vocabulary`).
#
# `capabilities/currency-fx.md:17` contracts "select Project reporting currency"
# with no shortlist anywhere. A per-deployment allowlist is a product decision no
# document takes, and hand-maintaining a subset of a seeded vocabulary is how the
# two ends drift again.
#
# Loaded once, lazily: the seed is a file read, and the module is imported on
# every request path.
@lru_cache(maxsize=1)






def _slugify(name: str) -> str:
    """Convert a project name to a URL-safe kebab-case slug (Dev Notes rules).

    lowercase -> spaces/underscores to hyphens -> strip non [a-z0-9-] ->
    collapse consecutive hyphens -> trim leading/trailing hyphens.
    Example: "Acme Corp (FR)" -> "acme-corp-fr".
    """
    s = name.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


























def _deny_project_scope(identity: str, project_id: str, operation: str) -> Response:
    write_audit_row(
        identity=identity,
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={"claimed_project_id": project_id, "operation": operation},
    )
    return JSONResponse(
        {"code": "not_found", "message": "Project not found"},
        status_code=404,
    )














# ---------------------------------------------------------------------------
# Story 7.3 (AC4) -- Connection revocation endpoint
#
# POST /api/projects/{project_id}/connections/{connection_id}/revoke
#
# Steps:
#   1. Verify connection belongs to the project (404 if not).
#   2. Call Nango API to delete the connection (best-effort; log + continue on error).
#   3. Purge health poller cache entry (health row delete + in-memory cleanup).
#   4. Mark connection_ref row as revoked.
#   5. Write audit row.
#   6. Return {"status": "revoked", "nango_deleted": bool}.
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Story 7.3 (AC5) -- Key rotation endpoint
#
# POST /api/projects/{project_id}/rotate-key
#
# Steps:
#   1. Call tenant_key_backend.rotate_key(project_id).
#   2. Write tka_ row with action='key_rotated'.
#   3. Return {"status": "rotated", "rotated_at": ISO_timestamp}.
# ---------------------------------------------------------------------------




# ===========================================================================
# Story 8.2 -- Datastream CRUD + /run endpoint.
#
# GET    /api/datastreams?project_id=<id>   -- list datastreams for a project
# POST   /api/datastreams                   -- create a datastream
# GET    /api/datastreams/{id}              -- get one datastream (project-scoped)
# PATCH  /api/datastreams/{id}              -- update a datastream
# DELETE /api/datastreams/{id}              -- delete / soft-archive a datastream
# POST   /api/datastreams/{id}/run          -- enqueue a pull for this datastream
#
# All endpoints:
#   - Require Bearer token (api_auth, same as other admin endpoints).
#   - Are project-scoped: callers supply ?project_id= or body.project_id.
#   - Return 404 + ACTION_CROSS_SCOPE_ATTEMPT audit row on cross-scope access (AD-5).
#   - Use French error messages.
#   - AD-8: admin console only; never direct DB access.
# ===========================================================================


def require_datastream_in_project(conn, *, datastream_id: str, project_id: str) -> bool:
    """Is THIS Datastream in THIS project? One statement, both columns (AI-219).

    Read-then-compare in Python was the shape most callers used, and it is one
    statement too many: it re-opens the window between the read and the use, and
    it tempts a second, distinguishable refusal envelope for "wrong project" that
    would let a caller enumerate stream ids by comparing answers.

    No `archived_at` / `enabled` filter on purpose. Membership is not liveness --
    an archived Datastream still belongs to its project, and the surfaces that
    read one (the detail page, the run history) must keep answering.
    """
    if not datastream_id or not project_id:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.datastreams WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        return cur.fetchone() is not None


def _require_datastream_role(
    project_id: str,
    identity: str,
    minimum_role: str,
    conn,
    *,
    datastream_id: str | None = None,
    pair_proven_by_read: bool = False,
) -> Response | None:
    """Enforce strict Viewer/Member/Owner access for Datastream surfaces.

    AI-219: the `datastream_id` used to reach this function ONLY to decorate the
    audit row. Every one of the 43 call sites that passes one takes it straight
    from the URL path, so a member of project A naming a stream of project B
    passed the role check and the handler read the foreign row. The id now
    decides: proven role, THEN proven membership, and both failures return the
    one envelope an absent stream returns.

    A caller with no stream to name (list, create) passes no `datastream_id` and
    is unaffected -- there is no project-less legitimate caller to weaken this for.

    ``pair_proven_by_read`` IS NOT A WAY OUT, IT IS AN ACCOUNTING RULE. A reader
    whose own statement already says ``d.id = %s AND d.project_id = %s`` has
    proven the pair; making the guard prove it again is a second round trip that
    establishes nothing. That is affordable on a detail page opened by a click
    and is not affordable on `datastream_progress_api`, the one route in the
    product built to be polled -- measured 2026-08-06 at the ASGI seal, the extra
    statement took its tick from 2 to 3 and its worst case from 3 to 4, against
    an acceptance bought at 9 for the `overview` call it replaces. So the claim
    is allowed, and it is CHECKED: `tests/conformance/
    test_datastream_readers_carry_project_scope.py` resolves every call site that
    makes it and fails if that reader's SQL does not in fact carry both columns.
    """

    from core.project_access import (  # noqa: PLC0415
        ProjectAccessUnavailable,
        identity_has_project_role,
    )

    reason = "insufficient_project_role"
    try:
        allowed = identity_has_project_role(
            project_id,
            identity or "anonymous",
            minimum_role,
            conn,
        )
        if allowed and datastream_id and not pair_proven_by_read:
            allowed = require_datastream_in_project(
                conn, datastream_id=datastream_id, project_id=project_id
            )
            if not allowed:
                reason = "datastream_outside_project"
    except ProjectAccessUnavailable:
        return JSONResponse(
            {"code": "unavailable", "message": "Verification des droits indisponible"},
            status_code=503,
        )
    if allowed:
        return None

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={
            "project_id": project_id,
            "datastream_id": datastream_id,
            "minimum_role": minimum_role,
            # The AUDIT separates the two refusals; the RESPONSE never does.
            # Which one it was belongs to the operator reading the log, not to
            # the caller comparing two 404s.
            "reason": reason,
            "operation": "datastream_access",
        },
    )
    return JSONResponse(
        {"code": "not_found", "message": "Flux de donnees introuvable"},
        status_code=404,
    )


def _enforce_datastream_project_scope(
    ds_project_id: str,
    identity: str,
    ds_id: str,
    conn,
    claimed_project_id: str = "",
    minimum_role: str = "viewer",
) -> Response | None:
    """Return a non-disclosing error unless scope and strict role are proven."""

    if claimed_project_id and claimed_project_id != ds_project_id:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CROSS_SCOPE_ATTEMPT,
            provider_account="",
            connection_ref="",
            metadata={
                "datastream_id": ds_id,
                "ds_project_id": ds_project_id,
                "claimed_project_id": claimed_project_id,
                "reason": "scope_mismatch",
                "operation": "datastream_access",
            },
        )
        return JSONResponse(
            {"code": "not_found", "message": "Flux de donnees introuvable"},
            status_code=404,
        )
    return _require_datastream_role(
        ds_project_id,
        identity,
        minimum_role,
        conn,
        datastream_id=ds_id,
    )


def _resolve_datastream_route_scope(
    conn, datastream_id: str, route_project_id: str
) -> str | None:
    """Return the Datastream data-owner project when linked to the route project."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ds.project_id
            FROM app.datastreams ds
            JOIN app.project_flux pf
              ON pf.flux_id = ds.id AND pf.org_id = ds.org_id
            WHERE ds.id = %s AND pf.project_id = %s AND ds.archived_at IS NULL
            """,
            (datastream_id, route_project_id),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# Story 12.19 -- deterministic daily sample-preview (server-side masking).
#
# GET /api/datastreams/{id}/sample
#   ?stage={collected|mapped|processed|published}&date_from=&date_to=&limit=5
#
# Returns, per day in the range, the first ``limit`` (default 5, hard-capped 20)
# deterministically-ordered eligible rows for the requested stage, with PII columns
# masked SERVER-SIDE before the response is built (cache_warehouse.read_datastream_
# sample owns the warehouse read + masking). Project/auth scoped exactly like the
# neighbouring Datastream routes (AD-5): Viewer role on the datastream's own project.
#
# The current mart is consolidated by owner project + connector and has neither a
# Datastream nor execution discriminator. The endpoint therefore fails closed when
# that scope is ambiguous and explicitly reports that no version binding exists.
# ---------------------------------------------------------------------------


















async def _publish_datastream_first_publication(request: Request) -> Response:
    """RETIREE. La publication gouvernee repond a cette question depuis
    `POST /api/projects/{project_id}/datastreams/{datastream_id}/executions/
    {execution_id}/publish-activate` (et `/publish-confirmations`), monte par
    `core.datastream_preconfiguration_api`. Ce handler n'est monte par rien et
    appele par rien -- mesure 2026-08-12, AD-43. Il est GARDE plutot que
    supprime, comme `_create_datastream_mapping_version` (arbitrage `e6e33d1`) :
    supprime, le retrait ne laisse aucune trace et le prochain lecteur remonte
    la route. `tests/conformance/test_retired_admin_routes.py` refuse ce
    remontage.

    POST /api/projects/{project_id}/datastreams/{ds_id}/publish

    Story 43.17: First Publication promotion seam. Promotes candidate version
    pointer to published version pointer in app.datastreams with audit row.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"}, status_code=401
        )

    project_id = request.path_params.get("project_id", "").strip()
    ds_id = request.path_params.get("ds_id", "").strip()

    if not project_id or not ds_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id et ds_id sont requis"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id,
                identity,
                "member",
                conn,
                datastream_id=ds_id,
            )
            if role_error is not None:
                return role_error

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT candidate_version_id, published_version_id
                    FROM app.datastreams
                    WHERE id = %s AND project_id = %s AND archived_at IS NULL
                    """,
                    (ds_id, project_id),
                )
                row = cur.fetchone()
                if not row:
                    return JSONResponse(
                        {"code": "not_found", "message": "Datastream introuvable."},
                        status_code=404,
                    )
                candidate_id, published_id = row
                target_version = candidate_id or f"ver_{ds_id}_1"

                cur.execute(
                    """
                    UPDATE app.datastreams
                    SET published_version_id = %s, updated_at = NOW()
                    WHERE id = %s AND project_id = %s
                    """,
                    (target_version, ds_id, project_id),
                )
            conn.commit()

            write_audit_row(
                identity=identity or "anonymous",
                action=ACTION_DATASTREAM_UPDATED,
                provider_account="datastream",
                connection_ref=ds_id,
                metadata={
                    "project_id": project_id,
                    "datastream_id": ds_id,
                    "event": "first_publication_promoted",
                    "published_version_id": target_version,
                },
            )
    except Exception as exc:
        logger.error("admin_api: publish_datastream db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."}, status_code=500
        )

    return JSONResponse(
        {
            "status": "published",
            "datastream_id": ds_id,
            "published_version_id": target_version,
        }
    )


def _overlay_rejection_counts(days: list[dict], ds_id: str, project_id: str, conn) -> None:
    """RETIRE -- le compte de rejets se lit ou les jours sont construits.

    Annotait APRES coup une liste de jours deja batie. Le compte se lit
    aujourd'hui dans le decoupage journalier lui-meme (`datastreams_api`), ce qui
    supprime le second passage sur la meme table. Garde en place, appele par
    rien -- voir `tests/conformance/test_retired_admin_routes.py`.

    Ce qu'elle faisait : set each day's ``rejection_count`` from the managed-feed
    rejected-row store.

    Groups app.managed_feed_rejected_rows by the rejected row's day for this
    project-scoped datastream, then annotates the matching sample day. Days with no
    rejections stay at 0. Read-only, project-scoped (AD-5).
    """
    if not days:
        return
    date_from = days[0]["date"]
    date_to = days[-1]["date"]
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        # created_at is the reject event time; the ledger's interval day is the row's
        # logical day. We bucket by created_at::date, bounded to the requested range.
        cur.execute(
            """
            SELECT created_at::date AS d, COUNT(*) AS n
            FROM app.managed_feed_rejected_rows
            WHERE datastream_id = %s AND project_id = %s
              AND created_at::date BETWEEN %s AND %s
            GROUP BY created_at::date
            """,
            (ds_id, project_id, date_from, date_to),
        )
        for d, n in cur.fetchall():
            counts[d.isoformat() if hasattr(d, "isoformat") else str(d)] = int(n)
    for day in days:
        if day["date"] in counts:
            day["rejection_count"] = counts[day["date"]]














async def _create_datastream_mapping_version(request: Request) -> Response:
    """RETIREE par `26695dc`, et l arbitrage est CLOS (`e6e33d1`).
    La route POST n'est pas remontee : ce handler garde la forme d'avant le
    Workbench six onglets -- il appelle `save_field_mapping` avec
    `advance_pointer` a son defaut `True`, donc il ACTIVERAIT la version
    qu'il ajoute, ce que les deux appelants gouvernes refusent explicitement.
    Le chemin qui repond aujourd'hui est `datastream_change.confirm_change`,
    qui tourne dans `execute_operation`. Ses six tests sont en
    `xfail(strict=True)` : un remontage les fait basculer en *unexpectedly
    passing*. AD-43 : cette note vit ICI parce qu'elle vivait dans
    `SESSIONS.md` et dans un commentaire de route, c'est-a-dire partout sauf
    la ou on lit la fonction.

    POST /api/datastreams/{id}/mapping/versions -- append immutable mapping version."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_header", "message": "Idempotency-Key header required"},
            400,
        )
    if len(idempotency_key) > 255:
        return JSONResponse(
            {"code": "invalid_header", "message": "Idempotency-Key header is too long (max 255)"},
            400,
        )

    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    mapping_payload = body.get("mapping") or body.get("mapping_payload")
    if not project_id or not isinstance(mapping_payload, dict):
        return JSONResponse(
            {"code": "missing_field", "message": "project_id et mapping sont requis"}, 400
        )
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingConflict,
            DatastreamMappingNotFound,
            DatastreamMappingStructuralError,
            DatastreamMappingUnavailable,
            save_field_mapping,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error

            res = save_field_mapping(
                datastream_id=ds_id,
                project_id=project_id,
                mapping_payload=mapping_payload,
                identity=identity or "anonymous",
                idempotency_key=idempotency_key,
                conn=conn,
            )
            status_code = 200 if res.get("idempotent_replay") else 201
            return JSONResponse(res, status_code=status_code)
    except DatastreamMappingNotFound:
        return JSONResponse({"code": "not_found", "message": "Flux introuvable"}, 404)
    except DatastreamMappingConflict:
        return JSONResponse({"code": "conflict", "message": "Conflit d'idempotence"}, 409)
    except DatastreamMappingStructuralError as exc:
        return JSONResponse({"code": "invalid_mapping", "issues": list(exc.issues)}, 422)
    except DatastreamMappingUnavailable as exc:
        logger.error("admin_api: create_datastream_mapping_version_unavailable: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Enregistrement du mapping indisponible"}, 503
        )
    except Exception as exc:
        logger.error("admin_api: create_datastream_mapping_version_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Enregistrement du mapping indisponible"}, 503
        )








# ===========================================================================
# Story 12.5: atomic candidate publication REST seams.
#
# create-execution (Member), state-advance (Member; publishing/published are
# internal-only via commit_publication), reconcile (Owner), publication-log
# (Viewer), single execution (Viewer). All reuse _require_datastream_role;
# cross-project returns a non-disclosing 404 + audit. Opaque 5xx (no str(exc)
# leak).
#
# The direct POST .../executions/{exec_id}/publish route was retired by
# 26695dcc (six-tab Workbench): ungoverned publication is closed, publishing
# goes through the governed confirmation flow (core.governed_publication, the
# project-scoped publish-confirmations/publish-activate routes). Its handler
# (_publish_datastream_execution) was deleted with AI-126; the absence of the
# route is pinned by test_datastream_activation.py::
# test_unsafe_direct_publication_routes_are_not_mounted.
# ===========================================================================












# ===========================================================================
# Story 12.7: read-only external BigQuery observation/registration.
#
# observe (Member) -- observe an EXISTING BigQuery object read-only and, on a
# fresh `ok` verdict, mint a 12.5 candidate execution (never writes the external
# object). The `external_object` coordinates are NOT taken from the request body:
# they are read server-side from the pinned plan version's intent
# (source.external_object) so the caller cannot re-target the observation at a
# different object than the one the plan was validated against. `probe_result`
# is the Phase-B live / injected read-only probe outcome. Reuses
# _require_datastream_role; cross-project returns a non-disclosing 404 + audit.
# ===========================================================================






# ===========================================================================
# Story 12.11: bounded sync / reload / reprocess (prepare + confirm).
#
# prepare (Member; assemble the AD-27 immutable proposal, no durable operation),
# confirm (Member; route EXACTLY ONE operations.execute_operation). All reuse
# _require_datastream_role; cross-project returns a non-disclosing 404 + audit.
# BoundedRecoveryError codes -> 422/409/404 (see _bounded_recovery_error_response).
# ===========================================================================










# ===========================================================================
# Story 12.12: safe replace / append / rollback (dataset recovery).
#
# rollback/preview (Viewer), rollback (Member), replace/preflight (Member),
# append/availability (Viewer), destination-policy (Owner via enforce_owner_floor).
# All reuse _require_datastream_role; cross-project returns a non-disclosing 404 +
# audit. DatasetRecoveryError codes -> the stable HTTP map in the story spec.
# ===========================================================================


def _dataset_recovery_error_response(exc) -> Response | None:
    """Map a DatasetRecoveryError to its stable ``.code`` -> HTTP, or None.

    Per the 12.12 INTEGRATION SPEC error map:
      rollback_window_expired -> 409; rollback_target_invalid /
      rollback_target_not_found -> 409/404; rollback_gate_failed -> 422 (+issues);
      concurrent_mutation_active -> 409 (+blocking id + lock reason);
      empty_replacement_blocked -> 422; owner_floor_required -> 403;
      access_unavailable -> 503; append_unavailable is handled at the read seam (200
      + fallback), never raised on these paths.
    """
    from core.dataset_recovery import DatasetRecoveryError  # noqa: PLC0415

    if not isinstance(exc, DatasetRecoveryError):
        return None
    code = exc.code
    if code == "rollback_window_expired":
        return JSONResponse(
            {
                "code": code,
                "message": exc.detail,
                "deadline": getattr(exc, "deadline", None),
                "deadline_source": getattr(exc, "deadline_source", None),
            },
            409,
        )
    if code == "rollback_target_not_found":
        return JSONResponse({"code": code, "message": "Cible de rollback introuvable"}, 404)
    if code == "rollback_target_invalid":
        return JSONResponse({"code": code, "message": exc.detail}, 409)
    if code == "rollback_gate_failed":
        return JSONResponse({"code": code, "issues": getattr(exc, "issues", [])}, 422)
    if code == "concurrent_mutation_active":
        return JSONResponse(
            {
                "code": code,
                "message": exc.detail,
                "blocking_execution_id": getattr(exc, "blocking_execution_id", None),
                "lock_reason": getattr(exc, "lock_reason", None),
            },
            409,
        )
    if code == "empty_replacement_blocked":
        return JSONResponse({"code": code, "message": exc.detail}, 422)
    if code == "owner_floor_required":
        return JSONResponse({"code": code, "message": exc.detail}, 403)
    if code == "access_unavailable":
        return JSONResponse({"code": code, "message": "Verification des droits indisponible"}, 503)
    return JSONResponse({"code": code, "message": exc.detail}, 422)


async def _preview_dataset_rollback(request: Request) -> Response:
    """RETIREE. L'apercu est devenu la moitie `/confirm` de la paire de preparations
    du Workbench (`core.datastream_workbench_api`). Meme garde (AD-43).

    GET /api/datastreams/{id}/rollback/preview?project_id=<id> (Viewer).

    Story 12.12. Resolves the default rollback target ONCE (the caller MUST echo the
    returned target_execution_id back to POST /rollback for idempotency across
    retries -- H1). 200 with {available, target_execution_id, current_execution_id,
    rollback_deadline, deadline_source, window_source, expired, reason}.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    target_execution_id = (request.query_params.get("target_execution_id") or "").strip() or None
    try:
        from core.dataset_recovery import preview_rollback  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            preview = preview_rollback(
                conn,
                datastream_id=ds_id,
                project_id=project_id,
                target_execution_id=target_execution_id,
            )
    except Exception as exc:
        logger.error("admin_api: preview_dataset_rollback_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Preview unavailable"}, 503)
    return JSONResponse(preview, 200)


async def _rollback_dataset(request: Request) -> Response:
    """RETIREE. Le retour arriere appartient a la confirmation exacte du Workbench,
    portee par le Projet : `POST /api/projects/{project_id}/datastreams/
    {datastream_id}/workbench/outputs/rollback-preparations`, monte par
    `core.datastream_workbench_api`. Meme garde que ci-dessus (AD-43).

    POST /api/datastreams/{id}/rollback (Member) -- Story 12.12.

    Body: {project_id, target_execution_id (REQUIRED -- resolved once via
    /rollback/preview), idempotency_key?}. Swaps the dataset pointer BACK to the
    retained target only after the DQ gates pass AND within the deadline. 200 with
    the rollback result (or {already_at_target: True} on an idempotent retry).
    DatasetRecoveryError codes -> the stable HTTP map.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    target_execution_id = str(body.get("target_execution_id") or "").strip()
    if not target_execution_id:
        # H1: the caller MUST resolve the target once via /rollback/preview and pass
        # the SAME id here so a retry short-circuits to already_at_target.
        return JSONResponse(
            {"code": "missing_field", "message": "target_execution_id est requis"}, 422
        )

    try:
        from core.dataset_recovery import DatasetRecoveryError, rollback_dataset  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                result = rollback_dataset(
                    conn,
                    datastream_id=ds_id,
                    project_id=project_id,
                    actor=identity or "anonymous",
                    target_execution_id=target_execution_id,
                )
                conn.commit()
            except DatasetRecoveryError as exc:
                conn.rollback()
                mapped = _dataset_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: rollback_dataset_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Rollback indisponible"}, 503)
    return JSONResponse(result, 200)








# ===========================================================================
# Story 12.14: versioned Datastream read model (Viewer).
#
# GET /api/datastreams/{id}/read-model -- surfaces, from the 12.2-12.5 tables, the
# plan versions, mapping versions, the current published execution + its DQ
# state/freshness/row_count, the current candidate (newest non-terminal execution),
# the publication log (actor/trace/prior-execution evidence), and the last import
# ledger rows (row/rejection counts). Reuses existing read helpers; adds NO new
# query logic beyond a single scoped candidate/current-pointer SELECT.
# ===========================================================================























# ---------------------------------------------------------------------------
# Story 8.3 — Extract ledger + refetch routes
#
# GET  /api/datastreams/{id}/ledger?project_id=&from=&to=
#      Returns day-grain extract ledger (last 35 days by default).
# POST /api/projects/{project_id}/datastreams/{datastream_id}/refetch  (story 58.4)
#      Body: {"dates": [YYYY-MM-DD, ...]} or {"from": ..., "to": ...}
#      Enqueues bounded pull(s) via enqueue_pull with datastream_id.
#      Contiguous date selections are grouped into one window each.
# ---------------------------------------------------------------------------
















# ===========================================================================
# Story 19.3 -- Observabilite + controle du cache DuckDB (CAP-22 / AD-22).
#
# GET  /api/admin/cache/status
#      Retourne l'etat courant du cache read-through : age, tables, fenetre,
#      row counts (lus du manifeste 19.1), hit rate (compteurs 19.2), project_ids
#      couverts. Etat honnete : "no-cache" | "stale" | "fresh" | "disabled".
#      AD-5 : l'endpoint expose uniquement les row counts des projets auxquels
#      l'appelant a acces (identite resolue depuis le Bearer token). Les project_ids
#      couverts sont filtres ; les row counts globaux sont presentes sans filtre
#      (ils ne revelent pas de donnees metier, juste des volumes). Ce choix suit
#      le modele des autres endpoints admin : la granularite de scoping est le projet
#      pour les donnees metier (cartes, rapports), pas pour les metriques d'infra.
#      Coherent avec /api/health qui expose des metriques d'infra sans scoping fin.
#
# POST /api/admin/cache/rebuild
#      Declenche un rebuild on-demand en reutilisant cache_warehouse.rebuild_cache().
#      Bornee (une seule execution, pas de rejouabilite). Auditee (AD-14 -- AD-15)
#      avec performed_by = identite REELLE du Bearer token (jamais 'system').
#      403 + audit sur tentative cross-projet (AD-5).
#      Repond toujours proprement (invariant f) : un echec retourne {"status": "failed"}
#      sans jamais propager une exception en 500 brut.
#
# Regles AD-5 retenues (detaillees dans l'artifact 19-3) :
#   - Le status expose les project_ids couverts bruts (metadonnees d'infra, pas
#     de donnees metier). Un admin qui voit la page sante voit l'etat global du cache.
#   - Le rebuild POST verifie que l'appelant a acces a AU MOINS un projet actif avant
#     de declencher (protection contre les anonymous). Pas de scoping fin : le cache
#     couvre tous les projets (AD-5 s'applique dans le fichier cache, pas dans cet
#     endpoint de controle). Coherent avec /api/health qui est accessible a tout admin.
# ===========================================================================











def _setup_no_store(response: Response) -> Response:
    response.headers.update(
        {
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
    )
    return response


def _setup_host_context(request: Request) -> dict[str, str]:
    return {
        "host": "rest",
        "workspace_id": (request.headers.get("X-Workspace-Id") or "console")[:256],
    }


def _setup_gate_response() -> Response | None:
    """The Epic 36 setup gate: open, and it is a SEAM, not a vestige.

    Same repair as the five `if not True:` blocks removed from
    `invitations_api` (audit 12, P2-4): a branch a future `not True` -> `not flag`
    would have turned into an inverted refusal. The branch goes; the function
    stays, and says out loud that it is open.

    IT STAYS FOR ONE REASON, and only one. Twelve call sites in six modules read
    this gate by name, and their own docstrings describe these routes as gated by
    it (`mcp_hosts_api`, `publication_reviews_api`, `source_delegations_api`,
    `first_value_api`, `entry_api`). Deleting it would silently ungate a dozen
    routes' PROSE while changing no behaviour -- a documentation lie is worse than
    an open gate that admits it is open. NOT for the reason its neighbours claim:
    `first_value_api` says "une soixantaine de suites patchent
    `core.admin_api._setup_gate_response`", and `grep -rl _setup_gate_response
    server/tests/` returns ZERO files. Closing the gate is therefore a decision
    still to make, not a switch someone is already flipping in tests.
    """
    return None


def _authorize_setup_task(
    conn,
    *,
    identity: str,
    task_id: str,
    minimum_capability: str = "edit",
) -> Response | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.org_id,j.project_id FROM app.setup_tasks t "
            "JOIN app.setup_journeys j ON j.id=t.journey_id WHERE t.id=%s",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    if row[1]:
        from core.project_access import resolve_strict_resource_access

        decision = resolve_strict_resource_access(
            identity,
            conn,
            minimum_capability=minimum_capability,
            project_id=row[1],
        )
        if not decision.allowed or decision.org_id != row[0]:
            return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
        return None
    return _enforce_org_manage(row[0], identity, conn, "manage_setup_task")


def _setup_error(exc: Exception) -> Response:
    from core.operations import OperationIdempotencyConflict
    from core.setup_responsibilities import SetupConflict, SetupUnavailable, SetupValidationError

    if isinstance(exc, SetupUnavailable):
        status, code, message = 404, "not_found", "Setup unavailable"
    elif isinstance(exc, (SetupConflict, OperationIdempotencyConflict)):
        status, code, message = 409, "conflict", "Setup state already changed"
    elif isinstance(exc, (SetupValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: setup operation failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Setup unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


async def _prepare_setup_handoff(request: Request) -> Response:
    """RETIRE -- la remise d'une tache de mise en route est passee au PROJET.

    Repondait `POST /api/setup/handoffs`, a l'echelle de l'organisation. Ce que
    la console ouvre aujourd'hui est
    `POST /api/projects/{project_id}/getting-started/tasks/{task_id}/handoffs`
    (`core/getting_started_api.py`), et c'est le deplacement qui compte : la
    porte org-wide repondait sans projet.

    Garde en place, monte par rien -- `tests/conformance/test_retired_admin_routes.py`
    refuse a la fois son remontage et la disparition de son successeur.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.setup_responsibilities import prepare_handoff

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            denied = _authorize_setup_task(
                conn,
                identity=identity,
                task_id=request.path_params["task_id"],
                minimum_capability="edit",
            )
            if denied is not None:
                return denied
            result = prepare_handoff(
                conn,
                task_id=request.path_params["task_id"],
                actor=identity,
                expires_in_hours=body.get("expires_in_hours", 48),
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _setup_error(exc)
    payload = {
        "handoff_id": result.handoff_id,
        "state": result.state,
        "expires_at": result.expires_at,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "replayed": result.replayed,
    }
    if result.delivery_url:
        payload["delivery_handoff"] = {"url": result.delivery_url, "single_return": True}
    return _setup_no_store(
        Response(
            json.dumps(payload),
            status_code=201,
            media_type="application/vnd.toorow.setup-handoff+json",
        )
    )


async def _reassign_setup_task(request: Request) -> Response:
    """RETIRE -- la reassignation d'une tache est passee au PROJET.

    Repondait `POST /api/setup/tasks/{task_id}/owner`. Le geste vit aujourd'hui
    dans `patch_task_owner`, sur
    `PATCH /api/projects/{project_id}/getting-started/tasks/{task_id}/owner`,
    qui appelle le meme `setup_responsibilities.reassign_task` avec une portee.

    Garde en place, monte par rien -- voir `test_retired_admin_routes.py`.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.setup_responsibilities import reassign_task

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            denied = _authorize_setup_task(
                conn,
                identity=identity,
                task_id=request.path_params["task_id"],
                minimum_capability="manage",
            )
            if denied is not None:
                return denied
            result = reassign_task(
                conn,
                task_id=request.path_params["task_id"],
                actor=identity,
                actor_type=str(body.get("actor_type") or ""),
                assigned_identity=body.get("assigned_identity"),
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _setup_error(exc)
    return _setup_no_store(JSONResponse(result))




























































router = Router(
    routes=[
        *_BROWSER_AUTH_ROUTES,
        *DATA_SURFACE_ROUTES,
        # Registered BEFORE the Governance read surface. Its `{section}` template
        # cannot match a change-set address today, but the two families share a
        # prefix, and ordering them by specificity means a future section route
        # cannot start swallowing the command family silently.
        *SEMANTIC_MODEL_ROUTES,
        *GOVERNANCE_SURFACE_ROUTES,
        # Story 49.6: the `ai-path` route type was registered in navigation.ts and
        # answered nothing. These are its reads.
        *AI_PATH_ROUTES,
        *datastream_preconfiguration_routes,
        # Story 57.7: an organization's saved setup templates. Saving only --
        # applying one is a PATCH on a setup draft, a route that already exists.
        *datastream_setup_templates_routes,
        *datastream_workbench_routes,
        # Story 63.2: where a run is, one index probe per call. Registered
        # beside the Workbench because it shares its address prefix; no template
        # of that family carries a variable last segment, so `/progress` cannot
        # be swallowed by one (verified against `admin_api.router.routes`).
        *datastream_progress_routes,
        # Story 58.1: the day-grain read of a Datastream. Same address family as
        # the two above and, like `/progress`, a fixed last segment -- no
        # template of the Workbench family ends in a variable, so it cannot be
        # swallowed by one.
        *datastream_daily_breakdown_routes,
        *query_spec_routes,
        # Chantier B: which Datastream holds a measure's total, and what each
        # breakdown of it sums to. A Governance address family -- its every
        # segment is literal except the two ids, so it swallows nothing.
        *metric_grain_routes,
        # Story 62.1: the MMM extract. `/exports/` is a literal segment of its
        # own under the Project, so it is swallowed by nothing and swallows
        # nothing -- and it is deliberately NOT under `/analyze`: an extract is
        # not a Result, it stores none, and putting it there would put a read
        # that writes nothing inside the family whose every member writes one.
        *_MMM_EXPORT_ROUTES,
        # Story 62.2 -- the SECOND reader of that same seam, beside the first and
        # under the same `/exports/` segment: one family, one file shape, two
        # readings. `capabilities/analytics-alignment.md` §4 requires exactly
        # that, and a route family is where a reader looks for the sibling.
        *_PLANNED_ACTUAL_EXPORT_ROUTES,
        # Chantier C: arming a Datastream's events, one gesture instead of four
        # operations. Registered late and still reached: of the 91 sub-routes of
        # `/datastreams/{id}/`, the six with a variable last segment all sit
        # deeper and behind a literal, so none can capture `/event-stream`.
        # Checked by resolving the address against `admin_api.router`, not by
        # reading the list -- the same proof AD-43 used for its 539 routes.
        *event_stream_arming_routes,
        # Chantier C: which observed entities carry no detail. One literal
        # segment under `analyze`, asked on demand -- a DISTINCT over a
        # published relation is not folded into every Result read.
        *entity_detail_gaps_routes,
        # Story 52.1: the Answerable Topic catalog. Its door is Analyze > Topics.
        *answerable_topic_routes,
        *calculated_field_proposal_routes,
        *ANALYZE_WORKBENCH_ROUTES,
        *_ANALYZE_ARTIFACT_ROUTES,
        *_DOSSIER_ROUTES,
        *visualization_spec_routes,
        # Story 72.5: the Chart Template family. Declared AFTER the Spec family
        # for the reason the two are siblings and not one: a template is unbound
        # and a Spec names members, so they share a base path and nothing else.
        *chart_template_routes,
        *render_share_routes,
        *render_share_console_routes,
        *golden_question_routes,
        *evaluation_run_routes,
        *trace_observation_routes,
        *feedback_regression_routes,
        *feedback_review_routes,
        # Story 22.11/22.19/22.23 (AI-123): a file-source template could only
        # be created by writing to the database directly -- which is how the
        # one row in production got there. This is its door.
        *file_source_template_routes,
        *project_settings_routes,
        # Story 75-4: the AI settings of a Project and of an Organization. Two
        # literal `ai-settings` segments under two families whose siblings are
        # literal too, so neither captures nor is captured.
        *_AI_SETTINGS_ROUTES,
        *project_overview_routes,
        *project_access_routes,
        *getting_started_routes,
        *INSTANCE_CLAIM_ROUTES_1,
        # Entrer sans scope : l'etat d'entree, l'entree hebergee, la remise.
        *ENTRY_ROUTES_1,
        *ORGANIZATIONS_ROUTES_1,
        # L'arrivee : la page d'amorce, l'echange du jeton, l'acceptation.
        *INVITATIONS_ARRIVAL_ROUTES,
        *INVITATIONS_ROUTES_1,
        # Entrer sans scope : l'etat d'entree, l'entree hebergee, la remise.
        *ENTRY_ROUTES_2,
        # Deleguer l'ouverture d'une source, et la revoquer.
        *SOURCE_DELEGATIONS_ROUTES_1,
        *MCP_HOSTS_ROUTES_1,
        *FIRST_VALUE_ROUTES_1,
        # La relecture d'une publication gouvernee.
        *PUBLICATION_REVIEWS_ROUTES_1,
        *INVITATIONS_ROUTES_2,
        *ORG_MEMBERS_ROUTES_1,
        *ORGANIZATIONS_ROUTES_2,
        # Exploitation : cache, schemas, miroir, sante.
        *PLATFORM_MAINTENANCE_ROUTES_1,
        *ME_ROUTES_1,
        *CREDENTIAL_ACCOUNTS_ROUTES_1,
        *DATASET_ACCESS_ROUTES_1,
        # La lecture sortante gouvernee, et le contrat que le mart publie.
        *PROJECT_DATASET_ACCESS_ROUTES,
        *_MART_CONTRACT_ROUTES,
        # Le lien entre un flux et un projet.
        *FLUX_PROJECTS_ROUTES_1,
        *PROJECT_CONNECTIONS_ROUTES_1,
        *DATASTREAM_SAMPLE_ROUTES_1,

        # Story 7.1 (AC3, AC4): project CRUD. Static /api/projects precedes the
        # parametrized /{project_id} routes so Starlette matches list/create first.
        # Les pays : meme sujet que les devises et les fuseaux (AD-43).
        *COUNTRY_VOCABULARY_ROUTES,
        *PROJECTS_ROUTES_1,
        *PROJECT_CONNECTIONS_ROUTES_2,
        # Le catalogue livre avec le produit, et ce que ce projet en retient.
        *CATALOG_ROUTES_1,
        *ME_ROUTES_2,
        # Deleguer l'ouverture d'une source, et la revoquer.
        *SOURCE_DELEGATIONS_ROUTES_2,
        *CONNECTIONS_ROUTES_1,
        *GOOGLE_OAUTH_ROUTES_1,
        # AI-119: the console's door onto the schedule. Same row as the MCP tools.
        *DATASTREAM_SCHEDULE_ROUTES,
        *CONNECTIONS_ROUTES_2,
        # Un travail lance : liste, etat, verification.
        *JOBS_ROUTES_1,
        *INTERNAL_ROUTES_1,
        # Story 56.7: the subscribers to `pull.landed`. One endpoint per consumer,
        # so each gets its own invocation, status and retry -- instead of being a
        # named call inside the worker whose failure was one WARNING line.
        *_FACT_ROUTES,
        # Ce qui s'est passe ce jour-la.
        *CONTEXT_EVENTS_ROUTES_1,
        # Exploitation : cache, schemas, miroir, sante.
        *PLATFORM_MAINTENANCE_ROUTES_2,
        *ALERT_DEFINITIONS_ROUTES_1,
        *ALERT_DESTINATIONS_ROUTES_1,
        # Story 5.5 (AC7): feedback queryability endpoint
        # La surface de preuve d'epic 14 : non referencee, pas cassee.
        *LEGACY_EVIDENCE_ROUTES,
        # Le catalogue livre avec le produit, et ce que ce projet en retient.
        *CATALOG_ROUTES_2,
        *NOTEBOOKS_ROUTES_1,
        # Story 6.6 (AC3): share token management.
        Route(
            # Story 50.7: kept MOUNTED on purpose. Deleting the entry answers 405,
            # which is a different statement and is indistinguishable to a client
            # from a routing regression. AC10 requires an explicit 410 naming the
            # replacement.
            "/api/notebooks/{notebook_id}/share",
            endpoint=share_notebook_gone,
            methods=["PATCH"],
        ),
        *NOTEBOOKS_ROUTES_2,
        # Story 8.2: the Datastream object itself -- list, create, intent versions,
        # validation. STATIC sub-paths first: `/{id}` absorbs them otherwise,
        # which is why the tail collection is spliced separately below.
        *DATASTREAM_OBJECT_ROUTES,
        # WHEN it collects and WHICH days: run, ledger, re-collection.
        *DATASTREAM_COLLECTION_ROUTES,
        # The mapping it applies and the projection it compiles. The collection
        # carries the comment explaining why there is deliberately no POST.
        *DATASTREAM_MAPPING_ROUTES,
        # One execution's lifecycle, its publications, and the read-only observation.
        *DATASTREAM_EXECUTION_ROUTES,
        # Story 12.8 / 12.9 / 12.10: the managed-feed import surface -- ledger,
        # upload, parsing contract and recurring sync. Extracted under AD-40; the
        # order INSIDE each collection is the order these routes were declared
        # here, and the two collections are spliced in that same order, so
        # Starlette resolves them exactly as before.
        *MANAGED_FEED_IMPORT_ROUTES,
        *FILE_IMPORT_ROUTES,
        # Story 12.11 bounded recovery, and the governed destination questions.
        *DATASTREAM_RECOVERY_ROUTES,
        # The object's catch-alls. LAST of the family on purpose -- see above.
        *DATASTREAM_OBJECT_TAIL_ROUTES,
        # Story 8.5: data model (target fields + mappings) CRUD -- routes live in
        # core.datamodel_api; static /fields paths precede /{name} inside the list.
        *_DATAMODEL_ROUTES,
        # Story 8.7: declarative flows (shared MCP/REST layer) -- core.flows_api.
        *_FLOWS_ROUTES,
        # Story 49.4 unmounted the seven /api/dq/* routes. Two reasons, both
        # named in its Reuse/Retire list: they were browser-composed DQ truth
        # (health inferred from alert firings and pull days, and the issue
        # identity PARSED out of an alert message), and /api/dq/evaluate started
        # its work in a request-scoped ThreadPoolExecutor that no operation could
        # observe or resume. Their only consumer was DataQualityPage.tsx, removed
        # in the same story; the governed replacement is the Controls & Quality
        # lens and the DQ Monitor workbench.
        #
        # core.dq_api itself STAYS: `fetch_dq_report_data` is a server-side helper
        # that first_report_readiness, main, mapping_proposal_mcp and
        # datastream_diagnosis all read. Deleting the module would break four
        # callers to remove seven doors.
        # Story 8.9: report-to-datamodel chain view -- core.report_chain.
        # NOTE: this route pattern /api/reports/{module}/{report_id}/chain must be
        # listed AFTER any /api/reports/{project}/{module}/{id} PATCH route so the
        # static suffix "chain" takes precedence in Starlette's routing order.
        *_REPORT_CHAIN_ROUTES,
        # Story 9.1: card library catalog + get_card REST mirror -- core.cards_api.
        *_CARDS_ROUTES,
        # Story 11.1: context layer topics + procedures CRUD -- core.context_api.
        *_CONTEXT_ROUTES,
        # Story 45.1: organization taxonomy + governed business links.
        *_BUSINESS_TAXONOMY_ROUTES,
        # Story 11.2: schema-context auto-generation trigger (ADMIN-only) --
        # core.schema_context_api. Separate module from 11.1's CONTEXT_ROUTES.
        *_SCHEMA_CONTEXT_ROUTES,
        # Story 22.1: media plans -- core.mediaplan_api.
        *_MEDIAPLAN_ROUTES,
        # Story 13.5 volet (a): galerie des rendus -- core.rendus_api.
        # Route statique /api/rendus/snapshots declaree avant /{snapshot_id}.
        *_RENDUS_ROUTES,
        # Epic 35 Story 35.4: boite insights + partage equipe -- core.daily_insights_api.
        *_DAILY_INSIGHTS_ROUTES,
        # Exploitation : cache, schemas, miroir, sante.
        *PLATFORM_MAINTENANCE_ROUTES_3,
        # Story 27.2: metric semantics curation REST API -- core.metric_semantics_api.
        *_METRIC_SEMANTICS_ROUTES,
        # Story 27.9: inverse lineage ("what feeds this conformed dimension?") and the
        # client-owned label -- core.dimension_lineage_api. Static /fed-by and /labels
        # declared before the parameterised /labels/{canonical_dimension}.
        *_DIMENSION_LINEAGE_ROUTES,
        # Story 75-6: the display block that rides the same cascade as the client
        # label -- core.presentation_extends_api. `/history` is declared before the
        # bare collection inside that list.
        *_PRESENTATION_EXTENDS_ROUTES,
        # Story 60.1: the client's OWN value mapping tables -- core.value_mapping_api.
        # A different store from the conformance one above and deliberately so
        # (migration 235 header): this one carries a NAME the client gives and the
        # Datastreams it is assigned to. Nothing here is read at render time yet.
        *_VALUE_MAPPING_ROUTES,
        # `unresolved-values.md` S1 and S2: the values a mapped Datastream carries
        # that the reading cannot name -- core.unresolved_values_api. ONE reading
        # behind both addresses, because the Workbench `Map` tab and the
        # `Value Tables` lens answering two different numbers for one Project is
        # the criterion that document refuses outright. `/extract` is declared
        # before the bare stream path inside that module.
        *_UNRESOLVED_VALUES_ROUTES,
        # Story 27.8: the language family's binding cycle -- core.language_bindings_api.
        # The primitive shipped complete and unaddressed (its whole lifecycle had zero
        # production callers until 2026-08-17); these three routes are that address.
        # Declaring a binding is a human act, so it carries `evidence_source='human'`
        # and names the dimension explicitly -- the automatic path still cannot confirm.
        *_LANGUAGE_BINDINGS_ROUTES,
        # Story 60.3: cleanup rules -- core.cleanup_rules_api. A rule stores a
        # PATTERN and never SQL, is applied AT READ, and its `/preview` segment is
        # declared before the parameterised `/{rule_id}` inside that module.
        *_CLEANUP_RULE_ROUTES,
        # Story 60.5: the immutable history of both families above, and the
        # confirmation preview that names the version, the hash and the affected
        # Datastreams BEFORE a change -- core.rule_versions_api. One module for
        # the two, because "what did this rule used to be" is the same question
        # asked twice and two answers would drift.
        *_RULE_VERSION_ROUTES,
        # Lot A1 (issue #68): the MDM canonical vocabulary, at an address of its
        # own -- core.mdm_canonical_fields_api. Six production modules validate
        # bindings against `app.mdm_canonical_fields` and nothing listed it
        # outside the file-source Template wizard, which returns neither the
        # value type nor the scope. A read, and only a read: the writer stays
        # `canonical_field_registry.declare_project_field`, which refuses to mint
        # a platform field from a project door.
        *_MDM_CANONICAL_FIELD_ROUTES,
        # Story 66.1: the common key -- core.mdm_common_keys_api. The object that
        # says two Datastreams speak of the same business identity, which nothing
        # in the repository carried before (measured 2026-08-13: zero occurrences
        # of `common_key`). Declared here, next to the vocabulary its components
        # come from, and never in a Datastream mapping or a Semantic View: those
        # two own the physical binding and the join, not the identity.
        *_MDM_COMMON_KEY_ROUTES,
        # Story 71.1: the measurement grain -- core.metric_dimensions_api. The
        # mirror of the common key on the measure axis: one canonical metric (the
        # head) reported against a set of canonical dimensions (the members).
        # Declared here, next to the vocabulary and the common key its head and
        # members come from, and never in a Datastream mapping or a Semantic View:
        # governance owns which measure is cut by which dimensions, not a private
        # per-Datastream model (governance.md amendment 2026-08-27).
        *_MDM_METRIC_DIMENSION_ROUTES,
        # Story 68.1: the declared entity type -- core.entity_types_api. The
        # feeder-less half of Story 64.1's declaration: a kind, its canonical
        # key and its label as governed configuration, before any source feeds
        # it. ONE writer with the MCP door: both call
        # `object_kind_registry.declare_entity_type`.
        *_ENTITY_TYPE_ROUTES,
        # Story 68.7: the discovery read -- core.entity_context_api. What the
        # Project declares (types, bindings, rule-set versions, coverage) in ONE
        # governed read, served to the console AND to the model by the same
        # function (`object_kind_registry.describe_entity_reconciliation_
        # context`): two doors, one writer, and the unavailable sections named
        # rather than zeroed.
        *_ENTITY_CONTEXT_ROUTES,
        # Story 66.2: which published sources can usefully be crossed --
        # core.datastream_matches_api. Two addresses, ONE producer: the Datastream
        # door is the Analyze catalog filtered, because two producers drift the day
        # one of them learns a new candidate kind. A read only: it ranks and it
        # explains, it approves nothing.
        *_DATASTREAM_MATCH_ROUTES,
        # Story 66.6: the pivot -- core.pivot_api. A projection of ONE immutable
        # Result, computed on the server. React receives cells and computes none:
        # a SUM in the browser would be a second semantic engine, with no measure
        # contract and no test, disagreeing with the warehouse the first time a
        # ratio or a truncated page appeared.
        *_PIVOT_ROUTES,
        # Stories 66.4/66.5 opened by 66.7: compile a cross-source plan, then run
        # it -- core.multi_source_api. Two acts, two addresses, because "your
        # request is ambiguous" and "the warehouse is down" must not arrive as the
        # same failure.
        *_MULTI_SOURCE_ROUTES,
        # Story 13.2: MDM conflicts + FX binding -- core.conflict_resolutions_api.
        # Static routes (/api/mdm/conflicts/resolutions) declared before parameterised
        # (/api/mdm/conflicts/resolutions/{project_id}/{target_field}/{source_module}).
        *_CONFLICT_RESOLUTION_ROUTES,
        # Story 48.3 removed /api/money/aggregation-check, /api/money/reconcile and
        # /api/timezone/day-offset-check. All three answered a question about MONEY or
        # DAYS from amounts, currencies and Datastream ids the CALLER supplied, so the
        # answer described whatever the caller claimed rather than what this Project
        # published -- and none of the three had a screen. The engines they wrapped
        # were KEPT, and this comment claimed all three were "now reached through
        # core.money_derivation". Re-measured 2026-08-17 (chantier 67-12), that is
        # true of ONE of them:
        #   * core.timezone_signal -- ALIVE, but not via money_derivation: it is
        #     called from core.datamodel:496 (check_cross_source_day_offset).
        #   * core.money_reconciliation -- DEAD: zero production importers.
        #   * core.currency_refusal -- DEAD transitively: its only importer is
        #     money_reconciliation:401, which nothing reaches.
        # And core.money_derivation reaches nothing itself -- its only production
        # importer takes two gap CONSTANTS (money_provenance_columns:68), never the
        # engine. See completeness-ledger.json `_reopened` 2026-08-17, which reopens
        # currency-fx[3] and [5] for citing that same dead path as proof.
        # Story 49.4: the ONE Controls & Quality command family. Reads stay on the
        # Story 49.1 Governance surface; this is the only consequential family, and
        # the Console and any future MCP surface call the same services.
        *_CONTROLS_QUALITY_ROUTES,
        # Story 48.3: the two governed reference vocabularies the always-present
        # Currency & FX and Reporting Timezone selectors read. Same immutable
        # versions core.money_policy validates against, so the list an operator
        # picks from and the list the server accepts cannot drift apart.
        *_REFERENCE_VOCABULARY_ROUTES,
        # Story 34.3: org-plan control surface. The POST is super-admin only
        # (deny-by-default); the GET added by AI-176 is member-scoped and is what
        # lets an org read the trial ceiling it is enforced against.
        *_ORG_PLAN_ROUTES,
        # AD-36: the platform clocks (Cloud Scheduler jobs) become readable,
        # editable and runnable without gcloud. Platform allow-list only; the
        # action paths are declared before /{clock_name} so it cannot shadow them.
        *_PLATFORM_CLOCK_ROUTES,
        # Story 38.2: connector installation state surface (platform-admin + catalog gate).
        # More-specific paths (/installation suffix) before any future less-specific
        # connector routes per Starlette convention.
        *_CONNECTOR_INSTALLATION_ROUTES,
        # Story 38.3: connector domain and adapter-route configuration (platform-admin).
        # /domain routes are distinct from /installation routes; Starlette resolves by
        # path suffix so ordering between them is irrelevant, but we keep 38.3 after 38.2.
        *_CONNECTOR_DOMAIN_ROUTES,
        # Story 38.4: connector verification + synthetic test delivery (platform-admin).
        # /verify, /test-delivery, /verification are more-specific than the bare
        # connector param routes; Starlette resolves these before any future catch-alls.
        *_CONNECTOR_VERIFICATION_ROUTES,
        # Story 38.5: connector activation/deactivation (org-owner) + health layering.
        # /activation/deactivate is more-specific than /activation; routes listed
        # more-specific-first per Starlette convention.
        *_CONNECTOR_ACTIVATION_ROUTES,
        # Story 38.6: import template catalog read + inbound managed-feed Datastream
        # creation. /templates and /datastreams are distinct sub-paths under the
        # connector param route; both are declared after more-specific connector
        # suffixes (/installation, /domain, /verify, /activation) per Starlette
        # convention (more-specific first).
        *_IMPORT_TEMPLATE_ROUTES,
        # Story 38.7: inbound delivery credential lifecycle (issue/rotate/revoke/list).
        # /credentials and /credentials/{id}/rotate|revoke are more-specific than the
        # bare connector/datastream param routes. Rotate/revoke are declared before the
        # bare /credentials collection route (more-specific first).
        *_INBOUND_CREDENTIAL_ROUTES,
        # Story 38.14: connector health + attachment inbox + delivery timeline.
        # Declared AFTER the credential routes and, within the module,
        # most-specific first (deliveries/{id} then inbox then health), because
        # Starlette resolves in declaration order and
        # /datastreams/{datastream_id}/inbox would otherwise be shadowed by any
        # broader /datastreams/{datastream_id}/{something} route added later.
        *_INBOUND_HEALTH_ROUTES,
        # Story 38.18: GET prepares (no side effect), POST executes. Both carry
        # a literal /raw-imports/ segment, so neither competes with the health
        # routes above.
        *_INBOUND_REPROCESS_ROUTES,
    ]
)
