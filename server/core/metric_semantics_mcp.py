"""toorow -- Metric-semantics MCP surface (Story 27.6, Epic 27).

The LLM SURFACE of the metric-semantics epic: it makes the reference table (layer 1
definitions + layer 2 reconciliation, delivered by 27.1/27.2/27.3) consumable AND
curable by the LLM through MCP tools, and exposes "verified queries" (question <->
reference query pairs, the Cortex Analyst pattern) that ground NL->metric.

CORRECTED 2026-07-31 -- THE VERIFIED QUERIES NO LONGER COME FROM THE EVAL CORPUS.
As delivered on 2026-07-21, this module read ``server/tests/evals/corpus.yaml`` and
served its 50 question<->SQL pairs to the model at runtime. That was wrong twice over,
and either reason alone is sufficient:

  * the corpus declares it on its own first line -- ``AD-17: this record is TEST CODE,
    not knowledge. It judges the context layer; it is not part of it.`` --  and
    ``docs/product-architecture/analyze-and-test.md`` (ratified 2026-07-29, so AFTER
    this module shipped) repeats it: the corpus "remains test code and never becomes
    Context Hub knowledge";
  * and it quietly destroyed the measurement. ``server/tests/evals/test_eval_gate.py``
    GRADES the system against those same 50 pairs. Grounding the model with the answer
    key to the exam it is about to sit means the gate stops measuring anything -- it
    reports how well the model reads what it was just handed. A green adherence gate
    was, on this path, evidence of nothing.

So the corpus read is gone. What replaces it is NOT the Golden Question of Epic 51
either: that object is the EVALUATION specification, and grounding the runtime with it
would reintroduce the same contamination one layer up. The runtime grounding source is
the **Answerable Topic** -- the product object, project-scoped and governed -- which is
not consolidated yet (see ``docs/product-architecture/glossary.md``, "Named here,
deliberately not built"). Until it exists, the tool stays registered and states its
absence: removing it would erase the only trace that this capability is owed.

Three read tools (metric_reference, metric_route, metric_verified_queries) and three
curation tools (metric_mapping_confirm/rename/reject) -- `metric_definition_upsert` was
retired on 2026-08-25, see the note at its former place in `register` -- all
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

# Story 52.2 DELIVERED the governed store, so the absence this module used to state --
# `governed_verified_query_store_not_delivered`, with the Answerable Topic named as its
# owner -- no longer exists. What remains are the two absences that are facts about the
# PROJECT or about the RUN, never about us, and they must stay distinguishable: a reader
# that cannot tell "nobody bound a query here" from "the store could not be read" cannot
# act on either. The codes themselves live in `core.answerable_topics`, which owns the
# store; duplicating their literals here would be a second copy of one contract.
def _verified_query_messages() -> tuple[dict, dict]:
    from core import answerable_topics as _topics  # noqa: PLC0415

    notes = {
        _topics.NO_BINDING_REASON: (
            "This project has bound no governed query to any Answerable Topic yet. A "
            "query is authored in Analyze > Explore (Query Spec, Story 50.1) and then "
            "bound to a topic. The evaluation corpus is deliberately NOT used as "
            "grounding: it is test code, and it is the answer key the adherence gate "
            "grades against."
        ),
        _topics.UNAVAILABLE_REASON: (
            "The governed binding store could not be read. This is NOT a statement that "
            "the project has no verified query -- the two are different facts."
        ),
    }
    summaries = {
        _topics.NO_BINDING_REASON: (
            "No verified query: this project has bound none yet."
        ),
        _topics.UNAVAILABLE_REASON: (
            "No verified query returned: the governed store could not be read."
        ),
    }
    return notes, summaries


class _LazyMessages(dict):
    """Resolve the message tables on first read, so import order stays free."""

    def __init__(self, index: int):
        super().__init__()
        self._index = index

    def __missing__(self, key):
        return _verified_query_messages()[self._index].get(key, "")


_VERIFIED_QUERY_NOTES = _LazyMessages(0)
_VERIFIED_QUERY_SUMMARIES = _LazyMessages(1)


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
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity) as conn:
            allowed = _require_org_read(org_id, identity, conn)
    except Exception as exc:  # noqa: BLE001 -- fail-closed on the guard (never unguarded).
        logger.error("metric_semantics_mcp: org-read guard failed org=%s: %s", org_id, exc)
        raise _tool_error("not_found", "Organization not found.") from exc
    if not allowed:
        raise _tool_error("not_found", "Organization not found.")


def _guard_org_manage(org_id: str, identity: str) -> None:
    """Raise forbidden unless *identity* is owner/admin of *org_id* (manage capability).

    Mirrors metric_semantics_api._require_org_manage (identity_can_manage_org). A guard
    evaluation failure fails closed (forbidden) -- the mutation must NOT proceed unguarded.
    """
    from core.metric_semantics_api import _require_org_manage  # noqa: PLC0415

    try:
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity) as conn:
            allowed = _require_org_manage(org_id, identity, conn)
    except Exception as exc:  # noqa: BLE001 -- fail-closed on the guard (never unguarded).
        logger.error("metric_semantics_mcp: org-manage guard failed org=%s: %s", org_id, exc)
        raise _tool_error("forbidden", "Droits insuffisants.") from exc
    if not allowed:
        raise _tool_error("forbidden", "Droits insuffisants.")


def _guard_project_read(project_id: str) -> None:
    """Raise not_found unless the caller may READ *project_id* (Story 52.2).

    Fail-CLOSED, deliberately: a guard that cannot be evaluated refuses, exactly like
    `_guard_org_read` above. The read that follows exposes a project's own authored
    question <-> query pairing, which is project configuration, not public metadata.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.project_access import identity_can_read_project  # noqa: PLC0415

    identity = _identity()
    try:
        with request_connection(identity) as conn:
            allowed = identity_can_read_project(project_id, identity, conn)
    except Exception as exc:  # noqa: BLE001 -- never proceed unguarded
        logger.error(
            "metric_semantics_mcp: project-read guard failed project=%s: %s", project_id, exc
        )
        raise _tool_error("not_found", "Project not found.") from exc
    if not allowed:
        raise _tool_error("not_found", "Project not found.")


def _assert_project_in_org(project_id: str, org_id: str, identity: str) -> None:
    """F-3/N-1: verify *project_id* belongs to *org_id*, else raise not_found.

    A member of org A must never read the PROJECT definitions/routes of a project of org B.
    Absent/foreign project => not_found (existence-hiding). A lookup failure also 404s.
    """
    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.error("metric_semantics_mcp: project lookup failed project=%s: %s", project_id, exc)
        raise _tool_error("not_found", "Project not found.") from exc
    if not row or row[0] != org_id:
        raise _tool_error("not_found", "Project not found.")


# ---------------------------------------------------------------------------
# A.2 -- reference builder (the merged layer, a deliberate re-write of the 27.2 §B.1
# builder using the SAME 27.1 store helpers -- the two surfaces (REST/MCP) are
# intentionally independent, no cross-module coupling of a router into an MCP module).
# ---------------------------------------------------------------------------


def _build_reference(org_id: str, identity: str, project_id: str | None = None) -> dict:
    """Build the merged reference layer for *org_id* (+ optional *project_id*).

    Bit-for-bit the 27.2 §B.1 contract: a ``metrics[]`` list where each entry carries
    canonical_name, display_name, aggregation_type, additive, ratio_*, format/unit/
    currency_mode, non_additive_dimensions, synonyms, ai_context, certified,
    resolved_scope, reconciliation (effective rule or None), source_mappings. Re-uses the
    27.1 store helpers (_load_definition_rows / reduce_definitions_by_specificity) plus
    the same ORG-scoped source_metric_mappings SELECT -- no warehouse cursor is opened.
    ``reconciliation`` comes from `metric_semantics.reference_reconciliation`, the
    governed resolver shared with the REST surface and the runtime (AI-295).
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.metric_semantics import (  # noqa: PLC0415
        _load_definition_rows,
        reduce_definitions_by_specificity,
        reference_reconciliation,
    )

    def_rows = _load_definition_rows(org_id=org_id, project_id=project_id)
    definitions = reduce_definitions_by_specificity(def_rows)

    # Source mappings: ORG scope for this org, grouped by definition id.
    mappings_by_definition: dict[str, list[dict]] = {}
    with request_connection(identity) as conn:
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
        # AI-295: the Project's published Rule Set, through the SAME resolver the
        # REST surface and the runtime use. The two surfaces stay independent of
        # each other; what they must not be independent of is the model.
        reconciliation = reference_reconciliation(
            project_id=project_id, metric=canonical_name
        )
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
    lines = [f"Metric '{metric}' -- reconciliation status: {status}."]

    if status == "ROUTED_TO_MART":
        lines.append(
            f"Route: pre-computed mart '{payload.get('target_mart')}' "
            "(read by a downstream consumer; no figure computed here)."
        )
    elif status == "DIRECT_SUM":
        lines.append(
            "Route: direct sum of the additive metric (no figure computed here)."
        )
    elif status in _NO_TOTAL_STATUSES:
        # No combined total -- enumerate the per-source series, never add them together.
        if status == "KEEP_SEPARATE":
            lines.append(
                f"{len(present)} per-source series that must NOT be added up (keep separate):"
            )
        elif status == "NOT_COMBINABLE":
            lines.append(
                "Non-additive metric with no rule: no combinable data series."
            )
        else:
            lines.append(f"{len(present)} per-source series (never a combined total):")
        for s in present[:20]:
            lines.append(f"  - {s['connector']}")
        message = reason.get("message")
        if message:
            lines.append(f"Configuration invited: {message}")

    return "\n".join(lines[:30])


# ---------------------------------------------------------------------------
# C -- verified queries (golden questions -> question<->reference query pairs, read-only).
# ---------------------------------------------------------------------------


def _governed_verified_queries(project_id: str, identity: str) -> tuple[list[dict], str | None]:
    """The verified-query pairs of ONE Project, from the governed store.

    STORY 52.2 CLOSED THE ABSENCE THIS FUNCTION USED TO STATE. The store is
    ``app.answerable_topic_query_bindings`` (migration 172): an Answerable Topic
    names N Query Spec versions, ordered, each with a role, each pinned exactly.
    The pairing is therefore authored and audited, not derived.

    This function used to open ``server/tests/evals/corpus.yaml`` and project its 50
    entries into few-shot pairs. It does not, and must not, for the two reasons stated at
    the top of this module: the corpus declares itself TEST CODE on its first line
    (AD-17), and it is the answer key the adherence gate grades against.

    Returns ``(pairs, reason_code)``. The reason code is what keeps three different
    facts apart -- the Project bound nothing, versus the store could not be read.
    Collapsing them into one empty list is the defect Story 27.6's correction was
    written against, and delivering the store does not make it safe again.
    """
    from core import answerable_topics as _topics  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            return _topics.verified_query_pairs(project_id, conn)
    except Exception as exc:  # noqa: BLE001 -- a read failure is never "there are none"
        logger.error("metric_semantics_mcp: verified query read failed: %s", exc)
        return [], _topics.UNAVAILABLE_REASON


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
        raise _tool_error("missing_param", "org_id is required.")
    identity = _identity()
    _guard_org_read(org_id, identity)
    if project_id is not None:
        _assert_project_in_org(project_id, org_id, identity)
    try:
        data = _build_reference(org_id, identity, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("metric_semantics_mcp: reference failed org=%s: %s", org_id, exc)
        raise _tool_error("server_error", "Server error.") from exc
    n = len(data["metrics"])
    summary = f"Metric reference (org={org_id}): {n} metric(s) resolved."
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
        raise _tool_error("missing_param", "org_id is required.")
    if project_id is None:
        raise _tool_error("missing_param", "project_id is required.")
    if metric is None:
        raise _tool_error("missing_param", "metric is required.")
    identity = _identity()
    _guard_org_read(org_id, identity)
    _assert_project_in_org(project_id, org_id, identity)
    try:
        from core import metric_reconciliation as _mr  # noqa: PLC0415

        decision = _mr.resolve_route(project_id, metric)
        emitters = _mr.emitters_of(metric)
    except Exception as exc:  # noqa: BLE001
        logger.error("metric_semantics_mcp: route failed metric=%s: %s", metric, exc)
        raise _tool_error("server_error", "Server error.") from exc
    data = _route_decision_to_dict(decision, emitters=emitters)
    return _result(_route_summary(data), data)


# ---- Read: verified queries (Answerable Topic grounding) ---------------
def metric_verified_queries(
    project_id: str, surface: str | None = None, tag: str | None = None, limit: int = 20
):
    """Paires question <-> requete gouvernee, pour UN projet (Story 52.2).

    Chaque paire vient d'un liage AUTORISE ET AUDITE : un Answerable Topic nomme
    N versions de Query Spec, ordonnees, chacune avec un role, chacune epinglee
    exactement (`app.answerable_topic_query_bindings`, migration 172). `surface`
    est la CLE DU TOPIC et `tags` porte le role -- le mapping est ecrit dans
    `answerable_topics.verified_query_pairs`, pas devine ici.

    NE LIT PAS LE CORPUS D'EVALS, et ne le relira jamais. Deux raisons, chacune
    suffisante : ce fichier se declare TEST CODE des sa premiere ligne (AD-17), et
    c'est le corrige sur lequel `test_eval_gate.py` NOTE le systeme -- le souffler au
    modele rendait le gate d'adherence incapable de mesurer quoi que ce soit.

    La source n'est pas non plus la Golden Question d'epic 51 : cet objet est la
    SPECIFICATION D'EVALUATION, et s'en servir pour ancrer l'execution
    reintroduirait la meme contamination un etage plus haut.

    Une liste vide n'est jamais rendue seule : elle porte son code de raison, parce
    que « ce projet n'a rien lie » et « le magasin est illisible » sont deux faits
    differents et qu'un lecteur qui ne peut pas les distinguer ne peut agir sur
    aucun des deux.
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise _tool_error("missing_param", "project_id is required.")
    _guard_project_read(project_id)
    surface = (surface or "").strip() or None
    tag = (tag or "").strip() or None
    pairs, reason_code = _governed_verified_queries(project_id, _identity())
    filtered = _filter_verified_queries(pairs, surface=surface, tag=tag, limit=limit)
    data: dict = {"verified_queries": filtered, "count": len(filtered)}
    if reason_code is not None:
        data["reason_code"] = reason_code
        data["note"] = _VERIFIED_QUERY_NOTES[reason_code]
        summary = _VERIFIED_QUERY_SUMMARIES[reason_code]
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
        raise _tool_error("not_found", "Mapping not found.")
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
        raise _tool_error("server_error", "Server error.") from exc
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
        raise _tool_error("missing_param", "org_id is required.")
    if mapping_id is None:
        raise _tool_error("missing_param", "mapping_id is required.")
    row = _mutate_mapping(org_id, mapping_id, status="confirmed")
    return _result(f"Mapping confirmed: {row.get('canonical_name')}.", row)


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
        raise _tool_error("missing_param", "org_id is required.")
    if mapping_id is None:
        raise _tool_error("missing_param", "mapping_id is required.")
    if new_canonical is None:
        raise _tool_error("missing_param", "canonical_name is required.")

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
        raise _tool_error("invalid_target", "Target metric not found.")
    row = _mutate_mapping(org_id, mapping_id, status="renamed", target_id=target_id)
    return _result(f"Mapping renomme vers : {row.get('canonical_name')}.", row)


def metric_mapping_reject(org_id: str, mapping_id: str):
    """Rejette un mapping propose (statut -> rejected). Guard manage + IDOR (F-1).

    Miroir MCP de POST /mappings/{id}/reject (27.2 B.3c). Delegue au store 27.1 (audit).
    """
    org_id = (org_id or "").strip() or None
    mapping_id = (mapping_id or "").strip() or None
    if org_id is None:
        raise _tool_error("missing_param", "org_id is required.")
    if mapping_id is None:
        raise _tool_error("missing_param", "mapping_id is required.")
    row = _mutate_mapping(org_id, mapping_id, status="rejected")
    return _result(f"Mapping rejected: {row.get('canonical_name')}.", row)


def register(mcp) -> None:
    """Register the metric-semantics MCP tools on *mcp*.

    Called once from core.main (the ONLY hook 27.6 adds to main.py). Defines each handler
    as a local function and calls ``mcp.tool(handler)`` -- the functional registration form
    (like ``mcp.tool(list_card_templates)`` in main.py), so this module never imports the
    mcp instance at module level (no cycle). Tool names are prefixed ``metric_`` for LLM
    discoverability: metric_reference / metric_route / metric_verified_queries (read) and
    metric_mapping_confirm / metric_mapping_rename / metric_mapping_reject (curation).
    """

    # THE FOURTH CURATION TOOL IS GONE (2026-08-25, story 49.3 AC1).
    #
    # `metric_definition_upsert` was the MCP door onto `app.metric_definitions`,
    # the second store that declares how a metric aggregates. `governance.md`
    # settled the precedence on 2026-08-15 (the Semantic Model wins) and named
    # the remaining work as "either a projection or the retirement of the lower
    # layer". This is that retirement, and the REST doors
    # (POST/DELETE /api/metric-semantics/definitions) went in the same commit --
    # the order the known-debt entry required: the MCP path last, so agents were
    # never left without an authoring surface the console still had.
    #
    # WHY REMOVED HERE AND ONLY REFUSED IN REST. A REST caller holds a URL, so
    # unmounting the route would answer 404 -- "your object does not exist" --
    # and send it looking; those doors therefore answer 409
    # `legacy_store_is_read_only` with the sentence that names the Semantic
    # Model. An MCP catalogue is discovered at call time: a tool that is not
    # offered is not a wrong answer to anybody. And a tool kept alive only to
    # refuse would still take `org_id`/`project_id` while resolving no access --
    # which `tests/conformance/test_mcp_tools_resolve_project_scope.py` reads,
    # correctly, as an unguarded scoped tool.
    #
    # WHAT AN AGENT DOES INSTEAD, and what it still cannot do. Reading is
    # unchanged: `metric_reference` serves the merged layer. Declaring a metric
    # is a Semantic Model change set, which today has a console surface
    # (`NewConceptDialog`) and NO MCP tool -- that gap is recorded in
    # `docs/product-architecture/known-debt.json` rather than papered over by
    # keeping a door onto the wrong store.

    # AD-43 -- three reads and three curation writes, each declared for what it is.
    # The three writes decide what a metric MEANS across the whole organization:
    # confirming, renaming or rejecting a mapping. They
    # are the mirror of REST endpoints the Console owns (POST /mappings/{id}/confirm
    # and siblings), so declaring them under the profile that governs semantics
    # removes them from the default catalog without removing the capability from
    # anyone -- it moves it back to the surface that has a person in front of it.
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        metric_reference,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        metric_route,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        metric_verified_queries,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        metric_mapping_confirm,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        metric_mapping_rename,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        metric_mapping_reject,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
