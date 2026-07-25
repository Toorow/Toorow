"""toorow -- Metric-semantics MCP surface (Story 27.6, Epic 27).

The LLM SURFACE of the metric-semantics epic: it makes the reference table (layer 1
definitions + layer 2 reconciliation, delivered by 27.1/27.2/27.3) consumable AND
curable by the LLM through MCP tools, and projects the eval corpus golden questions as
"verified queries" (question <-> reference query pairs, the Cortex Analyst pattern) that
ground NL->metric.

Three read tools (metric_reference, metric_route, metric_verified_queries) and four
curation tools (metric_mapping_confirm/rename/reject, metric_definition_upsert), all
registered by ``register(mcp)`` -- the ONLY hook 27.6 adds to core.main. This module is
NEVER imported by main.py at module level (no cycle): main.py receives only
``from core.metric_semantics_mcp import register`` + ``register(mcp)`` (2 lines).

GUARDS ARE MIRRORED, NEVER BYPASSED (contract of the orchestrator): every read tool passes
``identity_has_org_access``, every mutation passes ``identity_can_manage_org``, and the
27.2 security fixes are reproduced identically -- IDOR (F-1: a mapping of another org is
404, never mutated), project leak (F-3/N-1: a project of another org is 404), and PLATFORM
forbidden via the surface. There is NO tool that reads or writes the reference table
without its guard, and NO read tool has a side effect. Every mutation is an EXPLICIT user
action delegating to the 27.1 store (which audits each mutation in its transaction) --
never an autonomous AI change (human-validation-by-design).

HONEST STATUSES (27.3): metric_route serialises the typed RouteDecision and NEVER presents
a combined total when the status is KEEP_SEPARATE / UNRULED_OVERLAP /
OVERRIDE_NOT_MATERIALIZED / NOT_COMBINABLE -- it exposes the per-source series + the
reason.message (the invitation to configure, AD-9). F-4: series_present is the intersection
of the declared members with the REAL emitters (a member declared in the topology but
emitted by no source is filtered out of the real-data view), while series_declared keeps
the declared members for configuration transparency.

PASSIVE ON THE RUNTIME (invariant 1): 27.6 opens NO warehouse cursor -- the read tools read
the app semantic layer and route MART NAMES (never their content); the curation tools write
only via the 27.1 store. No warehouse row is read or written.

AD-2 (ZERO provider vocabulary): this module contains NO connector names. Connector names
come from the DB rows / manifests / corpus data, never from code.

Design mirrors metric_semantics_api.py / metric_reconciliation.py: ``from __future__ import
annotations``, module logger, ``core.*`` imports LAZY inside the function bodies (no cycle),
pure functions kept separate from I/O so the invariants are testable without Postgres.
French ToolError messages throughout. ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Envelope schema version (mirrors core.main.SCHEMA_VERSION -- kept as a local constant so
# this module never imports a private of core.main; single canonical value "1").
_SCHEMA_VERSION = "1"

# Default corpus path resolved relative to the repo (server/tests/evals/corpus.yaml), from
# server/core/metric_semantics_mcp.py -> two parents up is server/, then tests/evals. The
# CORPUS_PATH env var (or the tool argument) overrides it for offline tests (fixtures).
# server/tests/evals is READ-ONLY here (owned by the evals session 14.x) -- data only.
_CORPUS_ENV = "CORPUS_PATH"
_CORPUS_RELATIVE = ("tests", "evals", "corpus.yaml")

# The eval-corpus role we few-shot: only the VALIDATED reference query. The corpus also
# carries deliberately-wrong variants (the eval harness) -- we NEVER few-shot those.
_ROLE_CANONICAL_CORRECT = "canonical_correct"


# ---------------------------------------------------------------------------
# Envelope + error helpers (local -- never import a private of core.main).
# ---------------------------------------------------------------------------


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope (same shape as core.main).

    ``{schema_version, meta, data}`` -- rebuilt locally (not imported from main) so this
    module has no import coupling to core.main at module level.
    """
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON (FR message).

    The MCP layer then surfaces ``isError: true`` + ``{code, message}`` -- pattern get_card
    in core.main. Never a raw/untyped crash.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    """Resolve the caller identity from the MCP token (same pattern as get_card).

    ``token.claims.get("sub", token.client_id)`` when a token is present, else
    ``"anonymous"`` -- which the org guards then reject as a non-member (existence-hiding).
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


# ---------------------------------------------------------------------------
# Guard helpers -- MIRROR the REST guards (never a bypass). Source of truth for the
# rights is core.project_access (epic 21); we reuse the SAME helpers 27.2 uses.
# ---------------------------------------------------------------------------


def _guard_org_read(org_id: str, identity: str) -> None:
    """Raise not_found (existence-hiding) unless *identity* may READ *org_id*.

    Mirrors metric_semantics_api._require_org_read (identity_has_org_access). A non-member
    of an enrolled org gets 404 -- never disclose the org's existence. A guard-evaluation
    failure (DB down) also 404s: the read must NOT proceed unguarded.
    """
    from core.metric_semantics_api import _require_org_read  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _require_org_read(org_id, identity, conn)
    except Exception as exc:  # noqa: BLE001 -- fail-closed on the guard (never unguarded).
        logger.error("metric_semantics_mcp: org-read guard failed org=%s: %s", org_id, exc)
        raise _tool_error("not_found", "Organisation introuvable.") from exc
    if not allowed:
        raise _tool_error("not_found", "Organisation introuvable.")


def _guard_org_manage(org_id: str, identity: str) -> None:
    """Raise forbidden unless *identity* is owner/admin of *org_id* (manage capability).

    Mirrors metric_semantics_api._require_org_manage (identity_can_manage_org). A guard
    evaluation failure fails closed (forbidden) -- the mutation must NOT proceed unguarded.
    """
    from core.metric_semantics_api import _require_org_manage  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _require_org_manage(org_id, identity, conn)
    except Exception as exc:  # noqa: BLE001 -- fail-closed on the guard (never unguarded).
        logger.error("metric_semantics_mcp: org-manage guard failed org=%s: %s", org_id, exc)
        raise _tool_error("forbidden", "Droits insuffisants.") from exc
    if not allowed:
        raise _tool_error("forbidden", "Droits insuffisants.")


def _assert_project_in_org(project_id: str, org_id: str) -> None:
    """F-3/N-1: verify *project_id* belongs to *org_id*, else raise not_found.

    A member of org A must never read the PROJECT definitions/routes of a project of org B.
    Absent/foreign project => not_found (existence-hiding). A lookup failure also 404s.
    """
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.error("metric_semantics_mcp: project lookup failed project=%s: %s", project_id, exc)
        raise _tool_error("not_found", "Projet introuvable.") from exc
    if not row or row[0] != org_id:
        raise _tool_error("not_found", "Projet introuvable.")


# ---------------------------------------------------------------------------
# A.2 -- reference builder (the merged layer, a deliberate re-write of the 27.2 §B.1
# builder using the SAME 27.1 store helpers -- the two surfaces (REST/MCP) are
# intentionally independent, no cross-module coupling of a router into an MCP module).
# ---------------------------------------------------------------------------


def _build_reference(org_id: str, project_id: str | None = None) -> dict:
    """Build the merged reference layer for *org_id* (+ optional *project_id*).

    Bit-for-bit the 27.2 §B.1 contract: a ``metrics[]`` list where each entry carries
    canonical_name, display_name, aggregation_type, additive, ratio_*, format/unit/
    currency_mode, non_additive_dimensions, synonyms, ai_context, certified,
    resolved_scope, reconciliation (effective rule or None), source_mappings. Re-uses the
    27.1 store helpers (_load_definition_rows / _load_reconciliation_rows / reduce_*) plus
    the same ORG-scoped source_metric_mappings SELECT -- no warehouse cursor is opened.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.metric_semantics import (  # noqa: PLC0415
        _load_definition_rows,
        _load_reconciliation_rows,
        reduce_definitions_by_specificity,
        reduce_reconciliation_by_specificity,
    )

    def_rows = _load_definition_rows(org_id=org_id, project_id=project_id)
    definitions = reduce_definitions_by_specificity(def_rows)

    # Source mappings: ORG scope for this org, grouped by definition id.
    mappings_by_definition: dict[str, list[dict]] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.metric_definition_id, m.connector, m.source_field_path,
                       m.status
                FROM app.source_metric_mappings m
                WHERE m.scope_level = 'ORG' AND m.org_id = %s
                ORDER BY m.connector, m.source_field_path
                """,
                (org_id,),
            )
            for row in cur.fetchall():
                defid = row[0]
                mappings_by_definition.setdefault(defid, []).append(
                    {"connector": row[1], "source_field_path": row[2], "status": row[3]}
                )

    metrics = []
    for canonical_name, defn in sorted(definitions.items()):
        rec_rows = _load_reconciliation_rows(
            org_id=org_id, project_id=project_id, metric=canonical_name
        )
        rec_rule = reduce_reconciliation_by_specificity(rec_rows, canonical_name)
        reconciliation = None
        if rec_rule is not None:
            reconciliation = {
                "method": rec_rule.get("method"),
                "priority_order": rec_rule.get("priority_order"),
                "join_key": rec_rule.get("join_key"),
                "truth_connector": rec_rule.get("truth_connector"),
                "resolved_scope": rec_rule.get("scope_level"),
            }
        source_mappings = mappings_by_definition.get(defn.get("id", ""), [])
        metrics.append(
            {
                "canonical_name": canonical_name,
                "display_name": defn.get("display_name"),
                "aggregation_type": defn.get("aggregation_type"),
                "additive": defn.get("additive"),
                "ratio_numerator": defn.get("ratio_numerator"),
                "ratio_denominator": defn.get("ratio_denominator"),
                "format": defn.get("format"),
                "unit": defn.get("unit"),
                "currency_mode": defn.get("currency_mode"),
                "non_additive_dimensions": defn.get("non_additive_dimensions") or [],
                "synonyms": defn.get("synonyms") or [],
                "ai_context": defn.get("ai_context"),
                "certified": defn.get("certified", False),
                "resolved_scope": defn.get("scope_level"),
                "reconciliation": reconciliation,
                "source_mappings": source_mappings,
            }
        )

    return {"scope": {"org_id": org_id, "project_id": project_id}, "metrics": metrics}


# ---------------------------------------------------------------------------
# A.3 -- RouteDecision serialisation (pure, offline-testable) with F-4 filtering.
# ---------------------------------------------------------------------------

# Statuses for which NO combined total may ever be presented (per-source series only).
_NO_TOTAL_STATUSES = frozenset(
    {
        "KEEP_SEPARATE",
        "UNRULED_OVERLAP",
        "OVERRIDE_NOT_MATERIALIZED",
        "NOT_COMBINABLE",
    }
)


def _route_decision_to_dict(decision, *, emitters) -> dict:
    """Serialise a RouteDecision into a JSON dict, applying the F-4 series filter (PURE).

    ``series_declared`` = every declared member (transparency of configuration).
    ``series_present`` = the SAME list filtered to connectors that REALLY emit the metric
    (``connector in emitters``) -- a series whose connector does not emit the metric is NOT
    real data (F-4). An empty ``series_present`` after filtering is not an error: "the rule
    exists, no source feeds it yet". Deterministic: two calls on the same decision + emitters
    yield an equal dict.
    """
    emitter_set = set(emitters or ())
    series_declared = [
        {"connector": s.connector, "canonical_name": s.canonical_name}
        for s in decision.series
    ]
    series_present = [s for s in series_declared if s["connector"] in emitter_set]

    reason = None
    if decision.reason is not None:
        reason = {
            "code": decision.reason.code,
            "message": decision.reason.message,
            "emitters": list(decision.reason.emitters),
        }

    return {
        "metric": decision.metric,
        "status": decision.status,
        "method": decision.method,
        "scope_level": decision.scope_level,
        "overlap_group_id": decision.overlap_group_id,
        "target_mart": decision.target_mart,
        "series_declared": series_declared,
        "series_present": series_present,
        "priority_order": list(decision.priority_order),
        "reason": reason,
    }


def _route_summary(payload: dict) -> str:
    """Build the <=30-line honest summary for a serialised route decision.

    NEVER presents a combined total for a no-total status: it enumerates the per-source
    series and carries reason.message (the invitation to configure, AD-9). For
    ROUTED_TO_MART / DIRECT_SUM it indicates the route (mart / direct sum) WITHOUT computing
    any figure (27.6 does not read the warehouse).
    """
    metric = payload.get("metric")
    status = payload.get("status")
    present = payload.get("series_present") or []
    reason = payload.get("reason") or {}
    lines = [f"Metrique '{metric}' -- statut de reconciliation : {status}."]

    if status == "ROUTED_TO_MART":
        lines.append(
            f"Voie : mart pre-calcule '{payload.get('target_mart')}' "
            "(lecture par un consommateur en aval ; aucun chiffre calcule ici)."
        )
    elif status == "DIRECT_SUM":
        lines.append(
            "Voie : somme directe de la metrique additive (aucun chiffre calcule ici)."
        )
    elif status in _NO_TOTAL_STATUSES:
        # No combined total -- enumerate the per-source series, never add them together.
        if status == "KEEP_SEPARATE":
            lines.append(
                f"{len(present)} serie(s) par-source a NE PAS additionner (garder separees) :"
            )
        elif status == "NOT_COMBINABLE":
            lines.append(
                "Metrique non-additive sans regle : pas de serie de donnees combinable."
            )
        else:
            lines.append(f"{len(present)} serie(s) par-source (jamais de total combine) :")
        for s in present[:20]:
            lines.append(f"  - {s['connector']}")
        message = reason.get("message")
        if message:
            lines.append(f"Invitation a configurer : {message}")

    return "\n".join(lines[:30])


# ---------------------------------------------------------------------------
# C -- verified queries (golden questions -> question<->reference query pairs, read-only).
# ---------------------------------------------------------------------------


def _default_corpus_path():
    """Resolve the default corpus path (repo-relative), overridable by CORPUS_PATH env.

    server/core/metric_semantics_mcp.py -> parents[1] is server/, then tests/evals/
    corpus.yaml. READ-ONLY (owned by the evals session 14.x) -- data only.
    """
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    override = os.environ.get(_CORPUS_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1].joinpath(*_CORPUS_RELATIVE)


def _load_verified_queries(corpus_path=None) -> list[dict]:
    """Load the corpus and project it into verified-query pairs (PURE-ish, fail-soft).

    Opens the YAML with ``yaml.safe_load`` (READ mode only -- never writes the corpus).
    Each corpus entry becomes a pair ``{question, reference_sql, note, surface, difficulty,
    tags, tool_invocation}`` where ``reference_sql`` is taken from the FIRST
    ``reference_queries[]`` of role ``canonical_correct`` (the validated reference query;
    deliberately-wrong variants of the eval harness are excluded). An entry with no
    canonical_correct query yields ``reference_sql: None`` (never a wrong query few-shotted).

    FAIL-SOFT ABSOLUTE: a missing/unreadable file, an invalid YAML, or an unimportable yaml
    dependency returns ``[]`` -- a deployment without the corpus stays functional, never a
    crash.
    """
    from pathlib import Path  # noqa: PLC0415

    path = Path(corpus_path) if corpus_path is not None else _default_corpus_path()
    try:
        import yaml  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 -- yaml unavailable -> fail-soft to [].
        logger.warning("metric_semantics_mcp: yaml import failed (fail-soft): %s", exc)
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:  # READ ONLY, never a write mode.
            corpus = yaml.safe_load(handle)
    except FileNotFoundError:
        logger.info("metric_semantics_mcp: corpus absent at %s (fail-soft [])", path)
        return []
    except Exception as exc:  # noqa: BLE001 -- unreadable / invalid YAML -> fail-soft to [].
        logger.warning("metric_semantics_mcp: corpus read/parse failed (fail-soft): %s", exc)
        return []

    if not isinstance(corpus, dict):
        return []
    questions = corpus.get("questions")
    if not isinstance(questions, list):
        return []

    pairs: list[dict] = []
    for entry in questions:
        if not isinstance(entry, dict):
            continue
        reference_sql, note = _pick_canonical_query(entry.get("reference_queries"))
        pairs.append(
            {
                "id": entry.get("id"),
                "question": entry.get("question"),
                "reference_sql": reference_sql,
                "note": note,
                "surface": entry.get("surface"),
                "difficulty": entry.get("difficulty"),
                "tags": list(entry.get("tags") or []),
                "tool_invocation": entry.get("tool_invocation"),
            }
        )
    return pairs


def _pick_canonical_query(reference_queries) -> tuple[str | None, str | None]:
    """Return (reference_sql, note) of the FIRST canonical_correct query, else (None, None).

    Never returns a query of any other role (the deliberately-wrong eval variants).
    """
    if not isinstance(reference_queries, list):
        return None, None
    for rq in reference_queries:
        if isinstance(rq, dict) and rq.get("role") == _ROLE_CANONICAL_CORRECT:
            return rq.get("reference_sql"), rq.get("note")
    return None, None


def _filter_verified_queries(
    pairs: list[dict], *, surface: str | None, tag: str | None, limit: int
) -> list[dict]:
    """Filter verified-query pairs by surface/tag and bound the count (PURE)."""
    out = []
    for pair in pairs:
        if surface is not None and pair.get("surface") != surface:
            continue
        if tag is not None and tag not in (pair.get("tags") or []):
            continue
        out.append(pair)
    bound = limit if isinstance(limit, int) and limit > 0 else 20
    return out[:bound]


# ---------------------------------------------------------------------------
# A.5 -- register(mcp): define each handler locally and register it functionally.
# ---------------------------------------------------------------------------


def register(mcp) -> None:
    """Register the metric-semantics MCP tools on *mcp*.

    Called once from core.main (the ONLY hook 27.6 adds to main.py). Defines each handler
    as a local function and calls ``mcp.tool(handler)`` -- the functional registration form
    (like ``mcp.tool(list_card_templates)`` in main.py), so this module never imports the
    mcp instance at module level (no cycle). Tool names are prefixed ``metric_`` for LLM
    discoverability: metric_reference / metric_route / metric_verified_queries (read) and
    metric_mapping_confirm / metric_mapping_rename / metric_mapping_reject /
    metric_definition_upsert (curation).
    """

    # ---- Read: merged reference layer -------------------------------------
    def metric_reference(org_id: str, project_id: str | None = None):
        """Table de references mergee pour *org_id* (+ *project_id* optionnel).

        Le contrat MCP miroir de GET /api/metric-semantics/reference (27.2 B.1). Chaque
        metrique porte : canonical_name, display_name, aggregation_type, additive, ratio_*,
        format/unit/currency_mode, non_additive_dimensions, synonyms, ai_context, certified,
        resolved_scope, reconciliation (regle effective ou null), source_mappings. C'est la
        couche mergee et claire pour le LLM, pas un dictionnaire passif. Guard :
        identity_has_org_access ; projet etranger -> not_found (F-3).
        """
        org_id = (org_id or "").strip() or None
        project_id = (project_id or "").strip() or None
        if org_id is None:
            raise _tool_error("missing_param", "org_id est requis.")
        identity = _identity()
        _guard_org_read(org_id, identity)
        if project_id is not None:
            _assert_project_in_org(project_id, org_id)
        try:
            data = _build_reference(org_id, project_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("metric_semantics_mcp: reference failed org=%s: %s", org_id, exc)
            raise _tool_error("server_error", "Erreur serveur.") from exc
        n = len(data["metrics"])
        summary = f"Reference metriques (org={org_id}) : {n} metrique(s) resolue(s)."
        return _result(summary, data)

    # ---- Read: honest reconciliation route --------------------------------
    def metric_route(org_id: str, project_id: str, metric: str):
        """Decision de reconciliation HONNETE pour (project_id, metric).

        Miroir MCP de resolve_route (27.3). Guard org-read ; projet etranger -> not_found.
        Le structured_content porte la RouteDecision serialisee : status (un des 6), method,
        target_mart, series_present, series_declared, priority_order, reason {code, message,
        emitters}. F-4 : series_present = intersection(series declarees, emitters_of(metric))
        -- une serie dont le connecteur n'emet pas la metrique est filtree (pas une donnee
        reelle). Le resume NE PRESENTE JAMAIS un total pour KEEP_SEPARATE / UNRULED_OVERLAP /
        OVERRIDE_NOT_MATERIALIZED / NOT_COMBINABLE : il enumere les series par-source +
        l'invitation reason.message (AD-9). ROUTED_TO_MART / DIRECT_SUM -> il indique la voie
        (mart / somme directe), sans calculer de chiffre (27.6 ne lit pas le warehouse).
        """
        org_id = (org_id or "").strip() or None
        project_id = (project_id or "").strip() or None
        metric = (metric or "").strip() or None
        if org_id is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if project_id is None:
            raise _tool_error("missing_param", "project_id est requis.")
        if metric is None:
            raise _tool_error("missing_param", "metric est requis.")
        identity = _identity()
        _guard_org_read(org_id, identity)
        _assert_project_in_org(project_id, org_id)
        try:
            from core import metric_reconciliation as _mr  # noqa: PLC0415

            decision = _mr.resolve_route(project_id, metric)
            emitters = _mr.emitters_of(metric)
        except Exception as exc:  # noqa: BLE001
            logger.error("metric_semantics_mcp: route failed metric=%s: %s", metric, exc)
            raise _tool_error("server_error", "Erreur serveur.") from exc
        data = _route_decision_to_dict(decision, emitters=emitters)
        return _result(_route_summary(data), data)

    # ---- Read: verified queries (golden questions) ------------------------
    def metric_verified_queries(
        surface: str | None = None, tag: str | None = None, limit: int = 20
    ):
        """Paires question <-> requete de reference (verified queries, pattern Cortex).

        Lit le corpus d'evals (server/tests/evals/corpus.yaml) COMME DONNEE (jamais
        d'ecriture), chemin surcharge par la variable d'env CORPUS_PATH. Chaque entree
        devient une paire {question, reference_sql, note, surface, difficulty, tags,
        tool_invocation} en ne retenant que les reference_queries de role 'canonical_correct'
        (jamais une variante erronee du harnais d'eval). FAIL-SOFT : corpus absent/illisible/
        YAML invalide -> liste vide + note explicative, jamais un crash. Filtrable par
        surface/tag ; limite bornee. LECTURE SEULE : 27.6 ne possede pas le corpus (session
        evals 14.x) et ne l'ecrit jamais. Pas de guard org (corpus produit non-sensible), mais
        borne et read-only ; l'acces exige deja un token MCP (hors mode disabled).
        """
        surface = (surface or "").strip() or None
        tag = (tag or "").strip() or None
        pairs = _load_verified_queries()
        filtered = _filter_verified_queries(pairs, surface=surface, tag=tag, limit=limit)
        data: dict = {"verified_queries": filtered, "count": len(filtered)}
        if not pairs:
            # Fail-soft note: an empty corpus is functional, not an error.
            data["note"] = (
                "Aucune verified query disponible (corpus absent ou illisible) ; "
                "surface produit fonctionnelle sans grounding few-shot."
            )
            summary = "Aucune verified query disponible (corpus absent ou illisible)."
        else:
            summary = f"{len(filtered)} verified query(ies) (paires question<->requete)."
        return _result(summary, data)

    # ---- Curation: shared fetch+IDOR helper -------------------------------
    def _fetch_org_mapping_or_404(org_id: str, mapping_id: str) -> dict:
        """Fetch a mapping and enforce the 27.2 IDOR check, else 404 (existence-hiding).

        The single fetch path for confirm/rename/reject: the fetched mapping MUST belong to
        *org_id* AND be ORG-scoped, else an admin of org A could read/mutate a mapping of
        org B. Raising not_found (never forbidden) hides the mapping's existence. Callers
        MUST have already passed the manage guard on *org_id* before calling this.
        """
        from core.metric_semantics_api import _get_mapping_by_id  # noqa: PLC0415

        existing = _get_mapping_by_id(mapping_id)
        if (
            existing is None
            or existing.get("org_id") != org_id
            or existing.get("scope_level") != "ORG"
        ):
            raise _tool_error("not_found", "Mapping introuvable.")
        return existing

    # ---- Curation: shared mutation helper ---------------------------------
    def _mutate_mapping(org_id: str, mapping_id: str, *, status: str, target_id=None):
        """Guard(manage) -> fetch+IDOR (F-1) -> delegate to the 27.1 store.

        Reproduces the 27.2 confirm/rename/reject handler exactly: the manage guard is on
        the caller's org_id (from the argument); the fetched mapping must belong to that org
        AND be ORG-scoped (via _fetch_org_mapping_or_404), else 404 (IDOR, never mutate
        another org's mapping). Delegates to upsert_source_metric_mapping (which audits
        in-transaction) with the new *status* and ``created_by=identity``. ``target_id``
        overrides the definition id (rename re-target). Returns the refreshed mapping row.
        """
        from core.metric_semantics import upsert_source_metric_mapping  # noqa: PLC0415
        from core.metric_semantics_api import _build_mapping_row  # noqa: PLC0415

        identity = _identity()
        _guard_org_manage(org_id, identity)
        existing = _fetch_org_mapping_or_404(org_id, mapping_id)
        definition_id = target_id if target_id is not None else existing["metric_definition_id"]
        try:
            updated = upsert_source_metric_mapping(
                metric_definition_id=definition_id,
                connector=existing["connector"],
                source_field_path=existing.get("source_field_path"),
                extraction_note=existing.get("extraction_note"),
                scope_level=existing["scope_level"],
                org_id=existing.get("org_id"),
                project_id=existing.get("project_id"),
                status=status,
                created_by=identity,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "metric_semantics_mcp: mapping %s failed id=%s: %s", status, mapping_id, exc
            )
            raise _tool_error("server_error", "Erreur serveur.") from exc
        from core.metric_semantics_api import _get_mapping_by_id  # noqa: PLC0415

        refreshed = _get_mapping_by_id(updated["id"])
        return _build_mapping_row(refreshed or updated)

    def metric_mapping_confirm(org_id: str, mapping_id: str):
        """Confirme un mapping propose (statut -> confirmed). Guard manage + IDOR (F-1).

        Miroir MCP de POST /mappings/{id}/confirm (27.2 B.3a). Delegue au store 27.1 (qui
        audite la mutation) avec created_by = identite du token MCP.
        """
        org_id = (org_id or "").strip() or None
        mapping_id = (mapping_id or "").strip() or None
        if org_id is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if mapping_id is None:
            raise _tool_error("missing_param", "mapping_id est requis.")
        row = _mutate_mapping(org_id, mapping_id, status="confirmed")
        return _result(f"Mapping confirme : {row.get('canonical_name')}.", row)

    def metric_mapping_rename(org_id: str, mapping_id: str, canonical_name: str):
        """Re-cible un mapping vers une autre metrique canonique (statut -> renamed).

        Miroir MCP de POST /mappings/{id}/rename (27.2 B.3b). La cible canonical_name est
        resolue via _resolve_definition_id (ORG puis PLATFORM) ; cible inconnue ->
        invalid_target. Guard manage + IDOR (F-1). Delegue au store 27.1 (audit).
        """
        org_id = (org_id or "").strip() or None
        mapping_id = (mapping_id or "").strip() or None
        new_canonical = (canonical_name or "").strip() or None
        if org_id is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if mapping_id is None:
            raise _tool_error("missing_param", "mapping_id est requis.")
        if new_canonical is None:
            raise _tool_error("missing_param", "canonical_name est requis.")

        from core.metric_semantics_api import _resolve_definition_id  # noqa: PLC0415

        # F-1: EXACT 27.2 contract order -- guard manage FIRST, THEN fetch+IDOR the mapping,
        # THEN resolve the target on the VALIDATED mapping. Resolving before the IDOR check
        # would let an admin of org A probe org B's project scope; and the definition must be
        # scoped by the mapping we are actually allowed to rename, not by an unvalidated one.
        identity = _identity()
        _guard_org_manage(org_id, identity)
        existing = _fetch_org_mapping_or_404(org_id, mapping_id)

        # Resolve the target on the validated mapping's scope (unknown target -> invalid_target,
        # BEFORE any store mutation). _mutate_mapping re-guards + re-fetches for the write.
        target_id = _resolve_definition_id(
            new_canonical,
            org_id=existing.get("org_id"),
            project_id=existing.get("project_id"),
        )
        if target_id is None:
            raise _tool_error("invalid_target", "Metrique cible introuvable.")
        row = _mutate_mapping(org_id, mapping_id, status="renamed", target_id=target_id)
        return _result(f"Mapping renomme vers : {row.get('canonical_name')}.", row)

    def metric_mapping_reject(org_id: str, mapping_id: str):
        """Rejette un mapping propose (statut -> rejected). Guard manage + IDOR (F-1).

        Miroir MCP de POST /mappings/{id}/reject (27.2 B.3c). Delegue au store 27.1 (audit).
        """
        org_id = (org_id or "").strip() or None
        mapping_id = (mapping_id or "").strip() or None
        if org_id is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if mapping_id is None:
            raise _tool_error("missing_param", "mapping_id est requis.")
        row = _mutate_mapping(org_id, mapping_id, status="rejected")
        return _result(f"Mapping rejete : {row.get('canonical_name')}.", row)

    def metric_definition_upsert(
        org_id: str,
        scope_level: str,
        canonical_name: str,
        aggregation_type: str,
        additive: bool = True,
        project_id: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        ratio_numerator: str | None = None,
        ratio_denominator: str | None = None,
        format: str | None = None,  # noqa: A002 -- column name is 'format'
        unit: str | None = None,
        currency_mode: str | None = None,
        non_additive_dimensions: list | None = None,
        synonyms: list | None = None,
        ai_context: str | None = None,
        certified: bool = False,
    ):
        """Cree/met a jour une definition de metrique ORG ou PROJECT (+ audit du store 27.1).

        Miroir MCP de POST /definitions (27.2 B.4b). PLATFORM interdit via la surface
        (les defaults PLATFORM viennent des seeds) -> forbidden. Guard manage sur l'org
        cible. Delegue a upsert_metric_definition (qui valide le scope via validate_scope et
        audite) ; InvalidScope -> invalid_scope. Transmet synonyms et ai_context -- les
        colonnes qui alimentent l'enrichissement du vocabulaire cards et la surface LLM.
        """
        org_id_norm = (org_id or "").strip() or None
        project_id = (project_id or "").strip() or None
        scope = (scope_level or "").strip().upper()
        canonical = (canonical_name or "").strip() or None
        aggregation = (aggregation_type or "").strip() or None

        if scope == "PLATFORM":
            raise _tool_error(
                "forbidden", "La portee PLATFORM ne peut pas etre modifiee via la surface."
            )
        if scope not in ("ORG", "PROJECT"):
            raise _tool_error("invalid_param", "scope_level doit etre ORG ou PROJECT.")
        if canonical is None:
            raise _tool_error("missing_param", "canonical_name est requis.")
        if aggregation is None:
            raise _tool_error("missing_param", "aggregation_type est requis.")
        if not isinstance(additive, bool):
            raise _tool_error("invalid_param", "additive doit etre un booleen.")

        # Resolve the org to guard on (F-2: never write unguarded). For PROJECT without an
        # org_id, resolve the org from the project row; no resolvable org -> not_found.
        identity = _identity()
        guard_org_id = org_id_norm
        if guard_org_id is None and project_id is not None:
            from core.db import get_connection  # noqa: PLC0415

            try:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                        )
                        prow = cur.fetchone()
                if prow and prow[0]:
                    guard_org_id = prow[0]
            except Exception as exc:  # noqa: BLE001
                logger.error("metric_semantics_mcp: upsert project->org lookup failed: %s", exc)
                raise _tool_error("not_found", "Projet introuvable.") from exc
        if guard_org_id is None:
            raise _tool_error("not_found", "Projet introuvable.")

        _guard_org_manage(guard_org_id, identity)

        try:
            from core.metric_semantics import (  # noqa: PLC0415
                InvalidReconciliationRule,
                InvalidScope,
                upsert_metric_definition,
            )

            row = upsert_metric_definition(
                canonical_name=canonical,
                aggregation_type=aggregation,
                additive=additive,
                scope_level=scope,
                org_id=org_id_norm,
                project_id=project_id,
                created_by=identity,
                display_name=display_name or None,
                description=description or None,
                ratio_numerator=ratio_numerator or None,
                ratio_denominator=ratio_denominator or None,
                format=format or None,
                unit=unit or None,
                currency_mode=currency_mode or None,
                non_additive_dimensions=non_additive_dimensions or None,
                synonyms=synonyms or None,
                ai_context=ai_context or None,
                certified=bool(certified),
            )
        except (InvalidScope, InvalidReconciliationRule) as exc:
            # S-1: never propagate the internal exception text to the caller (it can leak
            # scope/id internals). Constant FR message out; the detail goes to the log only.
            logger.warning("metric_semantics_mcp: definition upsert rejected scope: %s", exc)
            raise _tool_error("invalid_scope", "Scope invalide.") from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("metric_semantics_mcp: definition upsert failed: %s", exc)
            raise _tool_error("server_error", "Erreur serveur.") from exc

        return _result(f"Definition upsert : {canonical} ({scope}).", row)

    mcp.tool(metric_reference)
    mcp.tool(metric_route)
    mcp.tool(metric_verified_queries)
    mcp.tool(metric_mapping_confirm)
    mcp.tool(metric_mapping_rename)
    mcp.tool(metric_mapping_reject)
    mcp.tool(metric_definition_upsert)
