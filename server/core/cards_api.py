"""toorow -- Card REST route handlers (Story 9.1, Epic 9).

Provides CARDS_ROUTES: list[Route] -- the REST MIRROR of the MCP card tools. Both
channels (REST here, MCP tools in core.main) call the SAME core.cards functions, so
the admin catalog surface and the LLM agent share one selection + render path (the
"common interface" invariant, Part D of epic 8, carried into epic 9).

Routes:
  GET  /api/cards/templates                          the discoverable catalog
  GET  /api/cards?project_id=&template=&metrics=&report_ref=&date_from=&date_to=
                                                     get_card mirror (AD-1 envelope)

Auth: shared _check_auth from core.admin_api (Bearer via core.api_auth). Identity from
the token drives AD-5 scoping inside the handler (same contract as datamodel_api).
AD-8: the admin console communicates through this REST layer only.

This module is NEVER imported by admin_api.py at module level (no circular import);
the orchestrator splices CARDS_ROUTES into admin_api.router at startup.

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentification requise"},
        status_code=401,
    )


def _loaded_modules():
    """Best-effort access to the loaded-module registry (for report_ref resolution)."""
    try:
        from core.main import get_loaded_modules  # noqa: PLC0415

        return get_loaded_modules()
    except Exception:  # pragma: no cover - registry unavailable in some unit contexts
        return []


# ---------------------------------------------------------------------------
# GET /api/cards/templates -- the discoverable catalog
# ---------------------------------------------------------------------------


def _enrich_catalog_entry(entry: dict) -> dict:
    """Add admin-catalog fields to a raw catalog entry (Story 9.8).

    Adds:
      * ``comment_builder`` -- True when a deterministic per-card comment builder is
        registered for this card id (narrative.CARD_COMMENT_BUILDERS). The KPI/base card
        uses the generic build_narrative (False here).
      * ``kind`` is already on the entry (context vs kpi) from to_catalog_entry.
    """
    from core import narrative as narrative_module  # noqa: PLC0415

    out = dict(entry)
    out["comment_builder"] = entry.get("id") in narrative_module.CARD_COMMENT_BUILDERS
    return out


def _canonical_vocabulary() -> tuple[set[str], set[str]]:
    """Return ``(metric_names, dimension_names)`` from the loaded modules' manifests.

    review-epic-9-backend F-6: a template distinguishes REQUIRED METRICS from REQUIRED
    DIMENSIONS, so the usability check must not conflate the two pools. The canonical
    vocabulary that classifies a field as a metric vs a dimension lives in each module
    manifest (``canonical_metric_mapping`` values = metrics, ``canonical_dimension_mapping``
    values = dimensions -- the same split 8.5's data dictionary uses). We union across every
    loaded module (AD-2: source-agnostic -- no module name is hardcoded; the names come from
    the manifests). ``average_position`` (nested target under a dict) is handled.
    """
    metrics: set[str] = set()
    dimensions: set[str] = set()
    for mod in _loaded_modules():
        manifest = getattr(mod, "manifest", None) or {}
        for target_raw in (manifest.get("canonical_metric_mapping") or {}).values():
            canon = target_raw.get("canonical") if isinstance(target_raw, dict) else target_raw
            if canon:
                metrics.add(canon)
        for target_raw in (manifest.get("canonical_dimension_mapping") or {}).values():
            canon = target_raw.get("canonical") if isinstance(target_raw, dict) else target_raw
            if canon:
                dimensions.add(canon)
    # Story 27.6: enrich the METRIC pool with the synonyms of the resolved definitions so a
    # question phrased with a synonym ("CA" for revenue) matches. ADDITIVE (set union) and
    # FAIL-SOFT (DB down / layer 27 absent -> no-op): the vocabulary is unchanged from the
    # manifests-only behaviour. Synonyms are layer-1 metric terms -> the metric pool only.
    # F-2 (27.6): subtract the (fully-populated) dimension pool FIRST so a synonym that
    # collides with a canonical DIMENSION name never leaks into the metric pool -- a field
    # must stay classified as a dimension, never become an ambiguous metric-and-dimension.
    metrics |= _synonym_metric_terms() - dimensions
    return metrics, dimensions


def _flatten_synonym_terms(value) -> set[str]:
    """Flatten one jsonb ``synonyms`` value into a set of terms (PURE, tolerant).

    The 27.1 ``synonyms`` column (F-2 / schema A.2) is a list whose entries are either a
    bare string, or ``{"lang": "fr", "terms": ["CA", "chiffre d'affaires"]}``. This helper
    accepts both forms and tolerates noise (None, non-list, non-string terms) -> an empty
    set. Offline-testable, no I/O.
    """
    terms: set[str] = set()
    if not isinstance(value, list):
        return terms
    for entry in value:
        if isinstance(entry, str):
            cleaned = entry.strip()
            if cleaned:
                terms.add(cleaned)
        elif isinstance(entry, dict):
            for term in entry.get("terms") or []:
                if isinstance(term, str) and term.strip():
                    terms.add(term.strip())
    return terms


def _synonym_metric_terms() -> set[str]:
    """Synonym terms of the resolved definitions, added to the metric pool (FAIL-SOFT).

    Reads the PLATFORM (+ ORG if an org context were available; none is at the vocabulary
    call site, so PLATFORM only) definition rows via the 27.1 store
    (metric_semantics._load_definition_rows / reduce_definitions_by_specificity) and
    flattens each row's jsonb ``synonyms`` into a set of terms. FAIL-SOFT ABSOLUTE: any
    exception (DB down, schema absent, 27.1 tables not migrated) -> empty set, so
    ``_canonical_vocabulary`` behaves EXACTLY as before. No hard dependency on layer 27:
    without a DB, the cards vocabulary is today's manifests-only vocabulary. AD-2 preserved
    (no provider name added -- synonyms come from the DB rows, never from code).
    """
    try:
        from core.metric_semantics import (  # noqa: PLC0415
            _load_definition_rows,
            reduce_definitions_by_specificity,
        )

        rows = _load_definition_rows(org_id=None, project_id=None)
        resolved = reduce_definitions_by_specificity(rows)
        terms: set[str] = set()
        for defn in resolved.values():
            terms |= _flatten_synonym_terms(defn.get("synonyms"))
        return terms
    except Exception as exc:  # noqa: BLE001 -- fail-soft: DB down -> manifests-only vocabulary.
        logger.debug("cards_api: synonym vocabulary enrichment skipped: %s", exc)
        return set()


def _project_available_canonical_fields(project_id: str, conn) -> tuple[set[str], set[str]]:
    """Return ``(available_metrics, available_dimensions)`` a project's datastreams map.

    The admin "Cartes" page uses this to show which templates are SATISFIED by a project.
    Source: app.datastream_mappings.target_field for the project's enabled datastreams
    (the same relation report_chain uses). AD-5: project-scoped via ds.project_id.

    review-epic-9-backend F-6: the raw target_field set mixes metric and dimension names.
    The mapping table does not itself record the field kind, so we classify each field
    against the manifest-derived canonical vocabulary (_canonical_vocabulary) into SEPARATE
    metric and dimension pools. A template requiring dimension ``X`` is then NOT satisfied
    just because a METRIC named ``X`` exists (and vice versa). A field present in neither
    vocabulary (an unmapped/custom canonical) is conservatively offered to BOTH pools so a
    legitimate project field is never silently dropped from usability.
    """
    raw_fields: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT dm.target_field
            FROM app.datastream_mappings dm
            JOIN app.datastreams ds ON ds.id = dm.datastream_id
            WHERE ds.project_id = %s
              AND ds.enabled = TRUE
              AND dm.target_field IS NOT NULL
            """,
            (project_id,),
        )
        for (target_field,) in cur.fetchall():
            if target_field:
                raw_fields.add(target_field)

    vocab_metrics, vocab_dimensions = _canonical_vocabulary()
    available_metrics: set[str] = set()
    available_dimensions: set[str] = set()
    for field in raw_fields:
        in_metric = field in vocab_metrics
        in_dimension = field in vocab_dimensions
        if in_metric:
            available_metrics.add(field)
        if in_dimension:
            available_dimensions.add(field)
        if not in_metric and not in_dimension:
            # Unknown canonical (vocabulary unavailable in this context, or a custom field):
            # offer it to both pools so a real project field is never dropped from usability.
            available_metrics.add(field)
            available_dimensions.add(field)
    return available_metrics, available_dimensions


async def _list_templates(request: Request) -> Response:
    """GET /api/cards/templates -- the card catalog for the admin "Cartes" page.

    Each entry carries id, title, answers_question, required/optional metrics +
    dimensions, widget_uri, fallback_rank, kind (context vs kpi) and comment_builder
    presence (Story 9.8).

    Optional ``project_id`` query param adds per-project usability: a ``usable`` boolean
    (True when the project's available canonical fields satisfy the template's required
    metrics/dimensions) and ``missing`` (what is absent) per entry. AD-5: when project_id
    is given, project access is enforced (404 on refusal). Context cards (kind=context)
    are always ``usable`` (they read app-level sources, not fact fields).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    project_id = (request.query_params.get("project_id") or "").strip() or None

    try:
        from core import cards as cards_module  # noqa: PLC0415

        raw = cards_module.list_templates()
        templates = [_enrich_catalog_entry(e) for e in raw]

        if project_id:
            from core.db import get_connection  # noqa: PLC0415
            from core.project_access import identity_has_project_access  # noqa: PLC0415

            with get_connection() as conn:
                # AD-5: enforce access before exposing the project's field inventory.
                if not identity_has_project_access(project_id, identity or "anonymous", conn):
                    return JSONResponse(
                        {"code": "not_found", "message": "Projet introuvable ou acces refuse"},
                        status_code=404,
                    )
                available_metrics, available_dimensions = (
                    _project_available_canonical_fields(project_id, conn)
                )

            for entry, tpl in zip(templates, cards_module.CARD_TEMPLATES):
                if tpl.is_context:
                    # Context cards read app-level sources -> always usable.
                    entry["usable"] = True
                    entry["missing"] = {"metrics": [], "dimensions": []}
                    continue
                # review-epic-9-backend F-6: separate metric and dimension pools so a
                # dimension requirement is not satisfied by a like-named metric (or vice versa).
                satisfied = tpl.is_satisfied_by(available_metrics, available_dimensions)
                entry["usable"] = satisfied
                entry["missing"] = cards_module._missing_inputs(
                    tpl, available_metrics, available_dimensions
                )
    except Exception as exc:  # noqa: BLE001
        logger.error("cards_api: list_templates_error: %s", exc)
        return JSONResponse(
            {"code": "internal_error", "message": f"Erreur interne : {exc}"},
            status_code=500,
        )

    return JSONResponse({"templates": templates})


# ---------------------------------------------------------------------------
# GET /api/cards -- get_card mirror
# ---------------------------------------------------------------------------


async def _get_card(request: Request) -> Response:
    """GET /api/cards -- resolve a card (AD-1 envelope + selection meta).

    Query params:
        project_id  (required)
        template    (optional) -- explicit card id; omitted => server suggestion
        metrics     (optional) -- comma-separated canonical metrics (ad-hoc mode)
        report_ref  (optional) -- "{module}/{report_id}" (report-backed mode)
        date_from / date_to (optional ISO) -- default: last 30 days

    Returns: {"summary", "envelope", "widget_uri"}.
    Error responses: 400, 401, 404 (scope / unknown template), 422 (unsatisfiable), 500.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    template = (request.query_params.get("template") or "").strip() or None
    report_ref = (request.query_params.get("report_ref") or "").strip() or None
    metrics_raw = (request.query_params.get("metrics") or "").strip()
    metrics = [m.strip() for m in metrics_raw.split(",") if m.strip()] if metrics_raw else None
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()

    # Story 9.8: a context card (e.g. template="connectors") is resolved from app-level
    # sources, so it needs NEITHER metrics NOR report_ref -- only project_id + template.
    # Fact cards still require one of metrics / report_ref as the row source.
    _is_context = False
    if template:
        try:
            from core import cards as _cards_mod  # noqa: PLC0415

            _tpl = _cards_mod.get_template(template)
            _is_context = bool(_tpl and _tpl.is_context)
        except Exception:  # noqa: BLE001
            _is_context = False

    if not report_ref and not metrics and not _is_context:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "metrics ou report_ref est requis",
            },
            status_code=400,
        )

    try:
        from core import cards as cards_module  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_access  # noqa: PLC0415

        # AD-5: scope enforcement before any warehouse read (best-effort DB open).
        try:
            with get_connection() as conn:
                if not identity_has_project_access(project_id, identity or "anonymous", conn):
                    return JSONResponse(
                        {
                            "code": "forbidden",
                            "message": "Acces refuse pour ce projet",
                        },
                        status_code=404,
                    )
        except Exception as scope_exc:  # noqa: BLE001 - fail open like the tool path
            logger.debug("cards_api: scope_check_skipped: %s", scope_exc)

        summary, envelope, widget_uri = cards_module.get_card(
            _loaded_modules(),
            project_id,
            template=template,
            metrics=metrics,
            report_ref=report_ref,
            date_from=date_from,
            date_to=date_to,
            identity=identity or "anonymous",
        )
    except cards_module.CardTemplateNotFound as exc:
        return JSONResponse(
            {"code": "not_found", "message": f"Template inconnu : {exc.args[0]}"},
            status_code=404,
        )
    except cards_module.CardTemplateUnsatisfied as exc:
        return JSONResponse(
            {
                "code": "unsatisfiable_template",
                "message": (
                    f"Le template '{exc.template_id}' requiert des donnees absentes"
                ),
                "missing": exc.missing,
            },
            status_code=422,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("cards_api: get_card_error: %s", exc)
        return JSONResponse(
            {"code": "internal_error", "message": f"Erreur interne : {exc}"},
            status_code=500,
        )

    return JSONResponse(
        {"summary": summary, "envelope": envelope, "widget_uri": widget_uri}
    )


# ---------------------------------------------------------------------------
# Exported route list (orchestrator wires into admin_api.router)
# ---------------------------------------------------------------------------

CARDS_ROUTES: list[Route] = [
    # Static /templates must precede the base /api/cards route so Starlette matches it.
    Route("/api/cards/templates", endpoint=_list_templates, methods=["GET"]),
    Route("/api/cards", endpoint=_get_card, methods=["GET"]),
]
