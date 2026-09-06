"""Story 50.6 -- the Analyze MCP surface, split into data, app-only reads and render.

THE SPLIT, IN ONE PARAGRAPH.
`docs/product-architecture/visualization-and-rendering.md` ("Tool split"): a DATA
tool returns a compact textual answer, a concise structured summary, the stable
Result identity, a provenance/freshness summary and bounded evidence, and attaches
NO widget resource. A RENDER tool takes the same Result identity plus a
Visualization Spec version, returns the SAME concise summary, and attaches the
shared MCP App resource. APP-ONLY read tools fetch bounded slices of the immutable
Result for the widget and are not exposed as model-callable analytical tools.
Those are four tools with three different jobs, and this module owns all four so
they cannot drift -- the `get_daily_report` / `get_report` / `get_card` divergence
this story repairs is exactly what three copies of one contract produce.

NO SECOND ANALYTICAL PATH. Every read here goes through Story 50.1's modules and
the tables they own. There is no query composition in this file, no warehouse
import, and no outcome logic: `core/query_specs_api.py` states the rule for the
REST adapter -- *"ONE SERVICE, MANY ADAPTERS"* -- and this is the MCP adapter it
predicted.

THE RLS FLOOR IS ARMED. Every connection that touches an Analyze table is opened
through `core.query_specs_api.analyze_connection`, never a bare `get_connection`.
That floor was missing on all four Analyze route families and was repaired in
50.1; a new adapter that opened its own connection would reopen it.

NON-DISCLOSURE. Foreign, denied, absent, revoked and expired converge on ONE
`not_found` raised from ONE place, and the denial answers before any work is done
because the access guard runs first. What is NOT claimed anywhere here: constant
time. Structural uniformity is what the code delivers; nothing in this story
measures a timing guarantee, so nothing asserts one.

Design mirrors `core/first_report_render_mcp.py`: module logger, lazy `core.*`
imports inside function bodies (no import cycle with `core.main`), a `_tool_error`
returning `ToolError(json.dumps({code, message}))`, an access guard that fails
closed to a single `not_found`. The text channel is French-first (UX-DR10, audit
C16); error envelopes stay English (AC14) and the file stays ASCII (AI-03).
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_MCP_PIVOT_MAX_BYTES = 65_536
_MCP_MATCH_CATALOG_MAX_BYTES = 262_144
_MCP_CONTEXT_OPTIONS_LIMIT = 50

#: The name the catalog invariant permits to advertise a widget resource. Imported
#: rather than retyped, so the guard and the tool cannot spell it differently.
#: Story 49.2: which Business Domain an id IS, the authority first. The pin
#: options below are the only reason this module ever touched the superseded
#: taxonomy store, and they go through the catalogue like every other reader.
from core import business_identity_catalogue as catalogue  # noqa: E402
from core.mcp_profiles import RENDER_TOOL_NAME  # noqa: E402

#: The text channel is a RECIT -- the model reads it, a person is shown it -- so
#: its sentences are rendered from the catalogue in the reader's language and are
#: never spelled in this file (`analyze-and-test.md`, amendment 2026-08-25).
from core.narrative_phrases import phrase  # noqa: E402

#: The one resource a render tool may ever attach. Story 50.5 registers it in
#: `core.visualization_runtime_resource`; importing the constant -- never retyping
#: the literal -- is what keeps the tool and the resource ONE identifier instead of
#: two spellings that happen to look alike.
from core.visualization_runtime_resource import VISUALIZATION_RUNTIME_URI  # noqa: E402

#: Why the render tool would be absent, when it is. Recorded as a value so the
#: catalog test can assert the ABSENCE by name and reason, and the missing tool
#: stays inventory instead of becoming invisible (AC1, AC10).
RENDER_TOOL_ABSENT_REASON = "visualization_runtime_not_delivered"
RENDER_TOOL_OWNING_STORY = "50.5"

#: Snapshot of what `register` decided, for the catalog test to read.
REGISTRATION_STATE: dict[str, Any] = {
    "render_tool_registered": False,
    "render_tool_name": RENDER_TOOL_NAME,
    "render_tool_resource_uri": None,
    "render_tool_absent_reason": None,
    "render_tool_owning_story": RENDER_TOOL_OWNING_STORY,
}


# ---------------------------------------------------------------------------
# Errors and identity -- the first_report_render_mcp shapes.
# ---------------------------------------------------------------------------


def _tool_error(code: str, message: str, refusals: list | None = None):
    """One refusal envelope. `refusals` carries the NAMED ones when there are any.

    A composition refused for three reasons and reported as one sentence makes a
    caller retry blind. The list is what tells it which member to change.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    payload: dict = {"code": code, "message": message}
    if refusals:
        payload["refusals"] = refusals
    return ToolError(json.dumps(payload))


def _not_found():
    """THE denial. One code, one message, one call site (AC13)."""
    return _tool_error("not_found", "Not found")


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _guard_project_view(project_id: str, identity: str):
    """Resolve the caller at the `view` floor; fail closed to a single not_found."""
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            )
    except Exception as exc:  # noqa: BLE001 -- never proceed unguarded.
        logger.error("analyze_render_mcp: access guard failed: %s", type(exc).__name__)
        raise _not_found() from exc
    if not decision.allowed or not decision.org_id:
        raise _not_found()
    return decision


# ---------------------------------------------------------------------------
# The exact Result deep link -- a structured owner reference, never a URL.
# ---------------------------------------------------------------------------


def result_deep_link(org_id: str, project_id: str, result_id: str, *, tab: str = "data") -> dict:
    """Return the exact authenticated Result reference for the 50.2 workbench.

    Following `core/project_capabilities_mcp.console_deep_link`: the console builds
    the href from the canonical navigation registry, so handing a host a hard-coded
    path would freeze a route this module does not own. `tab` names the lens the
    workbench should open on -- an exact reference, not "the Analyze screen".
    """
    from core.project_overview import owner_reference  # noqa: PLC0415

    return {
        "kind": "console",
        "requires_authenticated_session": True,
        "organization_id": org_id,
        "project_id": project_id,
        "object_type": "result",
        "object_id": result_id,
        "tab": tab,
        # `explore`, not `results`. A Result is declared under `analyze/explore`
        # (`shell/navigation/analyze.ts`) and that is where `ContentRouter`
        # navigates one; `analyze/results` is a section the console has never
        # had, so this reference resolved to nothing and was dropped in silence.
        # Found by `tests/conformance/test_owner_refs_resolve_against_navigation`.
        "owner_reference": owner_reference(
            "analyze", "explore", object_type="result", object_id=result_id, tab=tab
        ),
    }


# ---------------------------------------------------------------------------
# Reading one immutable Result. No analysis, no query composition.
# ---------------------------------------------------------------------------


def _load_result(conn, *, org_id: str, project_id: str, result_id: str) -> dict[str, Any]:
    """The Story 50.1 Result envelope, read with the exact fields 50.1 persists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, attempt_id, query_spec_version_id, outcome, ai_path_id,
                   ai_path_absent_literal, content_hash, row_count, cell_count,
                   byte_count, truncated, predecessor_result_id, started_at, ended_at
            FROM app.query_results WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise _not_found()
    return {
        "id": row[0],
        "attempt_id": row[1],
        "query_spec_version_id": row[2],
        "outcome": row[3],
        # Exactly one of the two is set; the migration-151 CHECK guarantees it, so
        # `ai_path` is never null and never absent -- it is an id or the exact
        # literal `No AI path` (`core/query_execution.py`).
        "ai_path": row[4] or row[5],
        "content_hash": row[6],
        "row_count": row[7],
        "cell_count": row[8],
        "byte_count": row[9],
        "truncated": row[10],
        "predecessor_result_id": row[11],
        "started_at": row[12].isoformat() if row[12] else None,
        "ended_at": row[13].isoformat() if row[13] else None,
    }


def _load_payload(conn, *, org_id: str, project_id: str, result_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content_hash, result_schema, manifest, rows_chunk
            FROM app.query_result_payloads
            WHERE result_id = %s AND org_id = %s AND project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise _not_found()
    return {
        "content_hash": row[0],
        "result_schema": row[1] or {},
        "manifest": row[2] or {},
        "rows_chunk": row[3] or [],
    }


# ---------------------------------------------------------------------------
# Projecting what the manifest ACTUALLY holds -- and saying `unavailable` for the rest.
# ---------------------------------------------------------------------------

_UNAVAILABLE = "unavailable"

#: The chain `query_execution.capture_evidence` spells inside `provenance`, in
#: the order source -> pull -> mapping -> published relation. `values` is
#: deliberately absent: see `_project_lineage`.
_LINEAGE_KEYS = (
    "source_system",
    "datastream_id",
    "mapping_version_id",
    "relation",
    "pull_id",
    "publication_log_id",
)

#: WHY AN OLD RESULT SAYS `unavailable`. A manifest that carries an execution's
#: evidence but not the Output version identity was frozen before
#: `capture_evidence` recorded it, and that is a different fact from "this
#: execution could not name its source". Same distinction, same honesty, as
#: `daily_insights_schema.authorship_of`: an insight published before the
#: derivation existed carries no derived confidence, and an old insight is not a
#: low-confidence one.
#: The WORDS are in the catalogue; this names only the KEY that fetches them
#: (arbitrage 2026-08-22 -- a sentence is spelled in one place, and this is a
#: sentence a person reads).
_LINEAGE_NOT_RECORDED_KEY = "lineage_not_recorded"

#: A DQ owner that could not be read reports the reason it raised. It is
#: machine-written text of unbounded length travelling in the model channel, so
#: it is cut here rather than allowed to eat the 4 KiB budget.
_DQ_REASON_MAX = 200


def _project_lineage(manifest: dict[str, Any]) -> Any:
    """The chain the execution snapshotted, or `unavailable`.

    THE KEY THE WRITER SPELLS, NOT A SECOND ONE. `capture_evidence` writes
    `provenance`; this function read `lineage`, a key written nowhere in this
    repository, so every Result in existence projected `unavailable` over a
    manifest that held the whole chain. It is the same class as the
    `freshness.as_of` defect below (AI-296): readers disagreeing with the one
    writer, and the writer is the authority because it is the one that saw the
    data.

    PER-MEMBER PROVENANCE IS DELIBERATELY NOT PROJECTED HERE. `provenance.values`
    holds one tuple per requested member and is unbounded, while everything this
    function returns travels in the model channel, whose budget is 4 KiB for the
    WHOLE payload (`core.model_channel`). Only how many were recorded is said,
    and the tuples stay where a reader can page them -- the Result workbench's
    provenance lens, over the same manifest.
    """
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        return _UNAVAILABLE
    chain = {key: provenance[key] for key in _LINEAGE_KEYS if provenance.get(key)}
    if not chain:
        return _UNAVAILABLE
    values = provenance.get("values")
    # Counted only when the list exists. `0` where nothing was recorded would be
    # the measured-zero lie Story 50.2 forbids by name.
    if isinstance(values, list):
        chain["member_count"] = len(values)
    return chain


def _project_data_quality(manifest: dict[str, Any]) -> Any:
    """What DQ evidence was in force, distinguishing "none open" from "unreadable".

    `capture_evidence` takes that distinction seriously enough to record an
    unreadable DQ owner as a reason rather than as zero open cases; a projection
    that collapsed an empty list back into `unavailable` would undo it.
    """
    ids = manifest.get("dq_evaluation_ids")
    reason = manifest.get("dq_unavailable_reason")
    if not isinstance(ids, list) and not reason:
        return _UNAVAILABLE
    from core.model_channel import bounded_head  # noqa: PLC0415

    head, withheld = bounded_head(ids if isinstance(ids, list) else [])
    return {
        "evaluation_ids": [str(value) for value in head],
        "evaluation_ids_withheld": withheld,
        "unavailable_reason": str(reason)[:_DQ_REASON_MAX] if reason else None,
    }


def project_provenance(manifest: Any) -> dict[str, Any]:
    """Project provenance from the frozen manifest, `unavailable` where it is silent.

    Writing `{}` or a plausible default here would be the deferred-evidence
    placeholder Story 50.1's gate forbids by name, so each field says either what
    the manifest holds or the word `unavailable`.

    WHAT CHANGED, AND WHY IT IS NOT A NEW CONTRACT. Story 50.1's close recorded
    that the manifest did not yet carry source -> pull -> mapping -> publication
    lineage, DQ evaluations or the Output version. Two of the three had been
    written since (`capture_evidence`, story 47.5/49.4) under names this function
    never read, and the third is written as of the same commit as this docstring.
    Nothing here invents a value: it reads the keys the writer spells.

    AND AN OLD RESULT STAYS HONEST. A manifest frozen before those keys existed
    keeps its `unavailable` and gains `lineage_unavailable_reason`, which says
    that it predates the recording rather than letting a reader conclude the
    execution could not name what it read.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    lineage = _project_lineage(manifest)
    output_version_id = manifest.get("datastream_output_version_id") or _UNAVAILABLE
    # Evidence WAS snapshotted for this Result, and it still cannot name the
    # Output version: the only way that happens is a Result frozen before the
    # writer existed. A manifest with no evidence at all is a different story --
    # the execution stopped before reading anything, and `missing_link` says
    # where, so nothing is added on top of it.
    predates = output_version_id == _UNAVAILABLE and isinstance(
        manifest.get("provenance"), dict
    )
    return {
        "semantic_view_version_id": manifest.get("semantic_view_version_id") or _UNAVAILABLE,
        "datastream_output_version_id": output_version_id,
        "lineage": lineage,
        "data_quality": _project_data_quality(manifest),
        "lineage_unavailable_reason": (
            phrase(_LINEAGE_NOT_RECORDED_KEY) if predates else None
        ),
        "missing_link": manifest.get("missing_link"),
    }


def project_freshness(manifest: Any) -> dict[str, Any]:
    """Same discipline for freshness: what is recorded, or `unavailable`.

    `stale_since_evaluated` is explicit so an unevaluated null cannot read as
    "evaluated, and fresh" -- the same honesty `core.envelope` states for the card
    surfaces.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    freshness = manifest.get("freshness")
    if not isinstance(freshness, dict):
        return {
            "state": _UNAVAILABLE,
            "as_of": None,
            "complete_through": None,
            "stale_since_evaluated": False,
        }
    return {
        "state": freshness.get("state") or _UNAVAILABLE,
        # `output_created_at` IS the as-of, and reading only `as_of` made this
        # projection answer `None` for every real manifest (AI-296). Three
        # readers disagreed with one writer: `query_execution` writes
        # `freshness.output_created_at` and nothing else, this read
        # `freshness.as_of`, and the Workbench lens read `manifest.stale_since`
        # at the ROOT -- a key written nowhere in the repository, so the console's
        # only freshness line could never render. The writer is the authority
        # here: it is the one that saw the data land.
        "as_of": freshness.get("as_of") or freshness.get("output_created_at"),
        "complete_through": freshness.get("complete_through"),
        "stale_since_evaluated": bool(freshness.get("stale_since_evaluated")),
    }


def bounded_evidence(
    rows: list[Any],
    schema: Any,
    *,
    outcome: str | None = None,
    counts_unknown_reason: str | None = None,
    row_count: int | None = None,
) -> list[dict[str, Any]]:
    """Bounded headline figures -- never zero evidence, never the dataset (AC9).

    A host with no UI at all gets these, and they are the reason a `deep_link` is
    an addition rather than a substitute. At most `MAX_BOUNDED_EVIDENCE_FIGURES`
    figures, each naming its column, taken from the first row of the frozen
    Result. That is enough to be checkable and far too little to be a dataset.

    THE ZERO-ROW CASE IS AN ANSWER, NOT A FAILURE -- and this is the repair.
    A Result with no rows has no column to quote: `core/query_execution.py`
    defaults `result_schema` to `{"fields": []}` and the `unavailable` / `refused`
    terminalizations pass no schema at all, so the loop above produces nothing.
    This function used to return `[]` there, and `_answer`'s
    `enforce_model_channel(..., require_evidence_with_deep_link=True)` then
    REFUSED the honest empty answer -- for `empty`, `refused` and `unavailable`,
    which is every Result this environment can currently produce (gate G5).
    A guard that makes the honest answer unreachable is worse than no guard.

    The repair carries evidence rather than bypassing the rule. Where no column
    can be quoted, what a host can still check is the outcome itself and either
    the stated reason the counts are unknown or the exact row count -- the same
    two facts the summary reports, in the same `{column, first_value}` shape, so
    a reader does not meet a second evidence vocabulary. Never `[]`.
    """
    from core.model_channel import MAX_BOUNDED_EVIDENCE_FIGURES  # noqa: PLC0415

    fields = []
    if isinstance(schema, dict):
        fields = [f.get("name") for f in (schema.get("fields") or []) if isinstance(f, dict)]
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    if not [f for f in fields if isinstance(f, str)] and first:
        # Rows exist but the frozen schema declares no field: the row itself is
        # the only honest statement of what the Result contains. Naming its keys
        # beats the old `{"column": "row", "first_value": "unavailable"}`, which
        # said a Result had evidence while quoting none of it.
        fields = list(first)
    figures: list[dict[str, Any]] = []
    for name in fields:
        if len(figures) >= MAX_BOUNDED_EVIDENCE_FIGURES:
            break
        if not isinstance(name, str):
            continue
        figures.append({"column": name, "first_value": first.get(name)})
    if figures:
        return figures
    figures.append({"column": "outcome", "first_value": outcome or _UNAVAILABLE})
    if counts_unknown_reason:
        figures.append({"column": "counts_unknown_reason", "first_value": counts_unknown_reason})
    else:
        figures.append({"column": "row_count", "first_value": row_count})
    return figures


#: How many grain reconciliations the text channel states. The channel is capped
#: at 30 lines and a Result rarely carries more than one measure; beyond this the
#: count is stated rather than the entries, so the cap never silently drops one.
_MAX_STATED_RECONCILIATIONS = 3


def compact_answer(
    result: dict[str, Any],
    provenance: dict,
    freshness: dict,
    grain_reconciliation: Any = (),
) -> str:
    """The text channel: <= 30 lines, outcome first, limitation last.

    THE WORDS ARE NOT HERE. This is a récit -- a channel a model reads and a
    person is shown -- so it is rendered in the reader's language, and this
    function keeps only its FORM: which facts, in which order (`analyze-and-test.md`,
    amendment 2026-08-25, from Jean's arbitration of 2026-08-22). The docstring
    used to claim « French-first (UX-DR10) » as its authority; UX-DR10 is a
    planning line that lives in no ratified document, and the arbitration
    replaced it.
    """
    lines = [
        phrase("result_outcome", result_id=result["id"], outcome=result["outcome"]),
        phrase(
            "result_counts",
            row_count=result["row_count"],
            cell_count=result["cell_count"],
            truncated=phrase(
                "result_truncated_yes" if result["truncated"] else "result_truncated_no"
            ),
        ),
        phrase(
            "result_query_spec_version", version_id=result["query_spec_version_id"]
        ),
        phrase("result_ai_path", ai_path=result["ai_path"]),
        phrase("result_content_hash", content_hash=result["content_hash"]),
        phrase(
            "result_freshness",
            state=freshness["state"],
            as_of=(
                phrase("result_freshness_as_of", as_of=freshness["as_of"])
                if freshness.get("as_of")
                else ""
            ),
        ),
        phrase(
            "result_provenance", version_id=provenance["semantic_view_version_id"]
        ),
    ]
    if provenance.get("missing_link"):
        lines.append(
            phrase("result_missing_link", missing_link=provenance["missing_link"])
        )
    if result["outcome"] in {"refused", "unavailable"}:
        lines.append(phrase("result_no_rows"))
    if result["truncated"]:
        lines.append(phrase("result_more_rows"))
    # CHANTIER B -- the model is told what the screen shows: how far this
    # breakdown is from the total its measure declares, and whether anyone has
    # explained the difference. A model reading only the breakdown would present
    # it as the whole figure, which is the substitution the declaration exists to
    # prevent. `()` is an older Result that carries none; `[]` is nothing to
    # compare; `None` is a comparison that failed, and those are not one state.
    if grain_reconciliation is None:
        lines.append(phrase("result_not_reconciled"))
    else:
        entries = [e for e in list(grain_reconciliation or ()) if isinstance(e, dict)]
        for entry in entries[:_MAX_STATED_RECONCILIATIONS]:
            lines.append(
                phrase(
                    "result_reconciliation",
                    measure=entry.get("measure_name") or entry.get("measure_id"),
                    total=entry.get("total"),
                    breakdown_sum=entry.get("breakdown_sum"),
                    gap=entry.get("gap"),
                    verdict=entry.get("verdict"),
                    statement=entry.get("statement") or "",
                ).strip()
            )
        withheld = len(entries) - _MAX_STATED_RECONCILIATIONS
        if withheld > 0:
            lines.append(phrase("result_reconciliation_withheld", count=withheld))
    return "\n".join(lines)


def build_summary(
    *,
    org_id: str,
    project_id: str,
    result: dict[str, Any],
    payload: dict[str, Any],
    semantic_view_version_id: str | None,
) -> dict[str, Any]:
    """The concise, model-visible `structuredContent`. Ten keys, and only ten.

    The tenth is `ai_path` -- added 2026-08-07 so the render tool's widget can
    name the walk it draws. It carries the IDENTITY only (an id or the exact
    literal `No AI path`, per the migration-151 CHECK): a handful of bytes, so
    the model channel budget is unaffected, and the walk itself stays in the
    app channel (the `ai_path_walk` key of the result meta that
    `result_slices.build_result_meta` alone writes), where a payload with
    judged branches does not trip the 4 KiB model budget.

    Counts are `None` with a stated reason when the outcome is `refused` or
    `unavailable`, never 0 -- the Story 50.2 rule: a zero that means "we do not
    know" is a lie a chart will happily draw.
    """
    from core.model_channel import MAX_BOUNDED_EVIDENCE_FIGURES, bounded_head  # noqa: PLC0415

    provenance = project_provenance(payload["manifest"])
    freshness = project_freshness(payload["manifest"])
    rows = payload["rows_chunk"] or []
    unknown = result["outcome"] in {"refused", "unavailable"}
    counts_unknown_reason = (
        phrase("result_counts_unknown", outcome=result["outcome"]) if unknown else None
    )
    schema_columns, columns_withheld = bounded_head(
        [
            f.get("name")
            for f in ((payload["result_schema"] or {}).get("fields") or [])
            if isinstance(f, dict)
        ],
        MAX_BOUNDED_EVIDENCE_FIGURES,
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "result": {
            "result_id": result["id"],
            "query_spec_version_id": result["query_spec_version_id"],
            "semantic_view_version_id": semantic_view_version_id or _UNAVAILABLE,
            "content_hash": result["content_hash"],
        },
        "outcome": result["outcome"],
        # The walk's IDENTITY, model-visible: an id or the exact literal
        # `No AI path` -- the migration-151 CHECK guarantees it is never null
        # and never absent. The walk itself is app-channel data; see `_answer`.
        "ai_path": result["ai_path"],
        "summary": {
            "row_count": None if unknown else result["row_count"],
            "cell_count": None if unknown else result["cell_count"],
            "counts_unknown_reason": counts_unknown_reason,
            "truncated": result["truncated"],
            "columns": schema_columns,
            "columns_withheld": columns_withheld,
        },
        "provenance": provenance,
        "freshness": freshness,
        # A Result with no rows and no declared column still owes a no-UI host
        # something checkable, so the outcome and the counts travel as evidence
        # rather than the answer being refused for having none (AC2, AC9).
        "evidence": bounded_evidence(
            rows,
            payload["result_schema"],
            outcome=result["outcome"],
            counts_unknown_reason=counts_unknown_reason,
            row_count=None if unknown else result["row_count"],
        ),
        "pagination": {
            "total_rows": None if unknown else result["row_count"],
            "returned_rows": 0,
            "has_more": bool(rows),
            "how_to_page": phrase("result_how_to_page"),
        },
        "deep_link": result_deep_link(org_id, project_id, result["id"]),
    }


def _compose_answer_on_connection(
    conn,
    *,
    identity: str,
    org_id: str,
    project_id: str,
    result_id: str,
    tool_name: str,
    meta_finalizer=None,
    include_ai_path_walk: bool = True,
):
    """Compose every Result channel on a caller-owned transaction.

    The execution tool uses this before its sole commit, so a model-channel or
    ToolResult refusal can still roll back Result plus AI Path. Read-only tools
    call it through `_answer`, which owns their shorter transaction.
    """
    import contextlib

    from core import result_app_grants, result_slices  # noqa: PLC0415
    from core.model_channel import enforce_model_channel  # noqa: PLC0415

    with contextlib.nullcontext(conn):
        result = _load_result(conn, org_id=org_id, project_id=project_id, result_id=result_id)
        payload = _load_payload(conn, org_id=org_id, project_id=project_id, result_id=result_id)
        if result["content_hash"] != payload["content_hash"]:
            # A grant pins the payload hash. Check the Result's own immutable
            # identity before such a grant can exist, otherwise a corrupt pair
            # would mint a perfectly usable handle for the wrong payload.
            raise _tool_error(
                "render_result_identity_mismatch",
                "The frozen Result identity is inconsistent. Re-run the analysis.",
            )
        semantic_view_version_id = _semantic_view_version(
            conn,
            org_id=org_id,
            project_id=project_id,
            query_spec_version_id=result["query_spec_version_id"],
        )
        # The walk, for the app channel. Loaded HERE, inside `_answer`, so the
        # data tool and the render tool carry it identically (AC10 covers the
        # summary; the meta follows the same single-composition rule).
        ai_path_walk = (
            _load_ai_path_walk(conn, project_id=project_id, ai_path=result["ai_path"])
            if include_ai_path_walk
            else None
        )
        handle = None
        try:
            handle = result_app_grants.issue_handle(
                conn,
                org_id=org_id,
                project_id=project_id,
                result_id=result_id,
                identity=identity,
                content_hash=payload["content_hash"],
                result_schema=payload["result_schema"],
            )
        except result_app_grants.GrantRefused as exc:
            # A Result whose schema declares no field (an `unavailable` outcome,
            # typically) has nothing to slice. That is honest, not an error: the
            # answer still carries its identity, its provenance and its reason.
            logger.info("analyze_render_mcp: no handle issued (%s)", exc.code)
        meta_kwargs = {
            "result_id": result["id"],
            "content_hash": payload["content_hash"],
            "result_schema": payload["result_schema"],
            "manifest": payload["manifest"],
            "rows_chunk": payload["rows_chunk"],
            # Small inline Results do not need an app-read grant. If a wide
            # schema cannot receive one, it must still retain its frozen
            # columns instead of being projected into empty row objects.
            "allowed_columns": (handle or {}).get("allowed_columns")
            or result_app_grants.allowed_columns_from_schema(payload["result_schema"]),
            "row_count": result["row_count"],
            "truncated": bool(result["truncated"]),
            "result_handle": (handle or {}).get("handle_id"),
        }
        try:
            meta = result_slices.build_result_meta(**meta_kwargs, ai_path_walk=ai_path_walk)
        except result_slices.SliceRefused:
            if ai_path_walk is None or "steps" not in ai_path_walk:
                # Leaving the connection context with the refusal rolls back a
                # newly issued grant. Metadata and transport authority therefore
                # become usable together, or neither does.
                raise
            meta = result_slices.build_result_meta(
                **meta_kwargs,
                ai_path_walk=_load_ai_path_walk(
                    conn, project_id=project_id, ai_path=""
                ),
            )
        if meta_finalizer is not None:
            # Render-only validation belongs inside this transaction. A grant
            # and its final app payload are committed together, or neither is.
            meta = meta_finalizer(
                conn=conn,
                identity=identity,
                org_id=org_id,
                result=result,
                payload=payload,
                meta=meta,
            )
        # Finalizers are additive by design.  Revalidate the complete metadata
        # only after the last one ran so no sidecar can bypass the 256 KiB cap.
        meta = result_slices.validate_result_meta(meta)
        # The grant is not usable until EVERY answer channel is valid. Summary
        # and model-channel enforcement can still refuse (for example bounded
        # evidence over budget), so they must run before the sole commit too.
        summary = build_summary(
            org_id=org_id,
            project_id=project_id,
            result=result,
            payload=payload,
            semantic_view_version_id=semantic_view_version_id,
        )
        summary["pagination"]["returned_rows"] = len(
            meta[result_slices.RESULT_META_KEY].get("rows")
            or meta[result_slices.RESULT_META_KEY].get("initial_projection")
            or []
        )
        # The Chart Template block, composed HERE for the same reason the AI path
        # walk is: the data tool and the render tool must carry byte-identical
        # summaries, and a block added at one tool's own call site is how they
        # stop being identical. Attached BEFORE `enforce_model_channel`, so the
        # budget the guard measures is the one the model actually receives.
        _attach_template_offer(
            conn,
            org_id=org_id,
            project_id=project_id,
            result_id=result_id,
            summary=summary,
        )
        text = compact_answer(
            result,
            summary["provenance"],
            summary["freshness"],
            (payload["manifest"] or {}).get("grain_reconciliation", ()),
        )
        enforce_model_channel(tool_name, text, summary)

    return text, summary, meta


def _attach_template_offer(
    conn, *, org_id: str, project_id: str, result_id: str, summary: dict[str, Any]
) -> None:
    """Offer the bounded Chart Template block -- but only where the door is open.

    THE BLOCK INHERITS THE RENDER TOOL'S CONDITIONAL REGISTRATION, and it inherits
    it from the ONE fact that decided it: `REGISTRATION_STATE`, written by
    `register` from `_visualization_runtime_available()`. Without the runtime the
    render tool is not registered, so the only gesture this block names cannot be
    performed -- and a menu of templates nobody could draw would be an invitation
    to a fallback drawing path that does not exist and must never be built. A
    second predicate spelled here would be a second authority, and it would be the
    one that drifts.
    """
    if not REGISTRATION_STATE["render_tool_registered"]:
        return
    from core.analyze_template_offer import attach_offer, compatible_templates  # noqa: PLC0415

    attach_offer(
        summary,
        compatible_templates(
            conn, org_id=org_id, project_id=project_id, result_id=result_id
        ),
    )


def _answer(
    project_id: str,
    result_id: str,
    tool_name: str,
    *,
    meta_finalizer=None,
):
    """Compose and commit the read-side answer for one immutable Result."""
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    project_id = (project_id or "").strip() or None
    result_id = (result_id or "").strip() or None
    if project_id is None or result_id is None:
        raise _tool_error("missing_param", "project_id and result_id are required.")
    identity = _identity()
    decision = _guard_project_view(project_id, identity)
    org_id = decision.org_id

    with analyze_connection(identity) as conn:
        answer = _compose_answer_on_connection(
            conn,
            identity=identity,
            org_id=org_id,
            project_id=project_id,
            result_id=result_id,
            tool_name=tool_name,
            meta_finalizer=meta_finalizer,
        )
        conn.commit()
    return answer


def _render_answer(
    project_id: str,
    result_id: str,
    visualization_spec_version_id: str | None = None,
    visualization_template_version_id: str | None = None,
):
    """Add one complete, pinned app payload to the canonical Analyze answer.

    TWO WAYS TO NAME THE PRESENTATION, AND THEY ARE EXCLUSIVE (story 72.7, AC28).
    A Visualization Spec version is a presentation that already exists; a Chart
    Template version is a presentation this Project will materialise from the
    Result's own schema. Both together are a caller who has not decided, and a
    server that picked one for him would be picking what gets drawn -- so it is a
    named refusal, not a precedence rule. Neither is the refusal this tool has
    always made, under the code it has always used.
    """
    from core import render_app_payload, visualization_specs  # noqa: PLC0415
    from core.result_slices import RESULT_META_KEY  # noqa: PLC0415

    project_id = (project_id or "").strip()
    result_id = (result_id or "").strip()
    spec_version_id = (visualization_spec_version_id or "").strip()
    template_version_id = (visualization_template_version_id or "").strip()
    if spec_version_id and template_version_id:
        raise _tool_error(
            "visualization_pin_ambiguous",
            "Name the presentation once: send visualization_spec_version_id for a "
            "Visualization that already exists, or visualization_template_version_id "
            "to apply a Chart Template to this Result -- never both.",
        )
    if not project_id or not result_id or not (spec_version_id or template_version_id):
        raise _tool_error(
            "missing_param",
            "project_id, result_id and one of visualization_spec_version_id or "
            "visualization_template_version_id are required.",
        )

    def finalize_meta(*, conn, identity, org_id, result, payload, meta, **_context):
        from core import tracing  # noqa: PLC0415
        from core.analyze_feedback import (  # noqa: PLC0415
            SIDECAR_META_KEY,
            feedback_fields_from_visualization_spec,
            mint_eligible_delivery_feedback_context,
        )

        pinned_spec_version_id = spec_version_id or _materialize_pinned_spec(
            conn,
            identity=identity,
            org_id=org_id,
            project_id=project_id,
            result_id=result["id"],
            template_version_id=template_version_id,
        )

        try:
            spec_version = visualization_specs.load_visualization_spec_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                visualization_spec_version_id=pinned_spec_version_id,
            )
        except visualization_specs.VisualizationNotFound as exc:
            raise _tool_error(
                "visualization_spec_not_available",
                "The Visualization Spec is not available in this Project. Choose another visual.",
            ) from exc

        if spec_version["query_spec_version_id"] != result["query_spec_version_id"]:
            raise _tool_error(
                "incompatible_visualization_spec",
                "The Visualization Spec was built for another Query Spec. "
                "Choose a compatible visual.",
            )

        result_meta = (meta or {}).get(RESULT_META_KEY) or {}
        projection_size = result_meta.get("projection_size")
        rows = (
            result_meta.get("rows")
            if projection_size == "small"
            else result_meta.get("initial_projection")
        )
        payload_rows = payload.get("rows_chunk")
        hashes_match = (
            result["content_hash"]
            == payload["content_hash"]
            == result_meta.get("content_hash")
        )
        if not hashes_match:
            raise _tool_error(
                "render_result_identity_mismatch",
                "The frozen Result identity is inconsistent. Re-run the analysis.",
            )
        complete_small = (
            projection_size == "small"
            and isinstance(rows, list)
            and isinstance(payload_rows, list)
            and all(isinstance(row, dict) for row in payload_rows)
            and not bool(result["truncated"])
            and result["row_count"] == len(payload_rows) == len(rows)
            and rows == payload_rows
        )
        bounded_large = (
            projection_size == "large"
            and isinstance(rows, list)
            and isinstance(payload_rows, list)
            and all(isinstance(row, dict) for row in payload_rows)
            and all(isinstance(row, dict) for row in rows)
            and result_meta.get("result_id") == result["id"]
            and result_meta.get("row_count") == result["row_count"] == len(payload_rows)
            and rows == payload_rows[: len(rows)]
            and bool(result_meta.get("result_handle"))
        )
        if not (complete_small or bounded_large):
            raise _tool_error(
                "render_result_not_complete",
                "This Result has no valid bounded projection for rendering. "
                "Open the Result workbench.",
            )

        pivot_sidecar = _pivot_render_sidecar(
            conn,
            project_id=project_id,
            result=result,
            payload=payload,
        )

        # Story 75-6 -- the presentation extends, and ONLY here. The scope cascade
        # PLATFORM > ORG > PROJECT carries a `display` block beside the definitions
        # (governance.md, amendment of 2026-09-05), and this is the single place a
        # served figure's spec document is composed. The STORED Visualization Spec
        # version is untouched -- its `content_hash` does not move; what is extended
        # is the copy handed to the runtime, and only when the document left its
        # `formatting.number_style` at the default `auto` AND an organization or a
        # project actually set a format leaf. With no override stored, the very
        # same object is returned -- byte for byte the stored document, which is
        # the amendment's own `Incomplete if` -- so today's behaviour is unchanged.
        # A resolution that fails is rolled back to its own SAVEPOINT, so this
        # transaction is still usable for the composition below.
        from core.presentation_extends import (  # noqa: PLC0415
            apply_presentation_extends,
        )

        served_spec_document = apply_presentation_extends(
            conn,
            document=spec_version["spec"],
            org_id=org_id,
            project_id=project_id,
            query_spec_version_id=result["query_spec_version_id"],
        )

        try:
            runtime_manifest = render_app_payload.load_runtime_manifest()
            pins = render_app_payload.resolve_runtime_pins(
                runtime_manifest,
                family=spec_version["family"],
                schema_version=spec_version["schema_version"],
                profile="mcp-inline",
            )
            render_input = render_app_payload.compose_render_input(
                result={
                    "result_id": result["id"],
                    "content_hash": payload["content_hash"],
                    "outcome": result["outcome"],
                    "schema": payload["result_schema"],
                    "rows": rows,
                    "manifest": render_app_payload.project_result_manifest(
                        payload["manifest"]
                    ),
                    "truncated": bool(result["truncated"]),
                    "row_count": result["row_count"],
                },
                spec={
                    "visualization_spec_version_id": spec_version["id"],
                    "spec_contract_version": spec_version["spec_contract_version"],
                    "schema_version": spec_version["schema_version"],
                    "document": served_spec_document,
                },
                pins=pins,
                profile="mcp-inline",
                display={},
            )
            visualization_meta = {
                "visualization_spec_version_id": pinned_spec_version_id,
                "runtime_resource_uri": VISUALIZATION_RUNTIME_URI,
            }
            final_meta = dict(meta or {})
            final_meta["toorow.visualization"] = visualization_meta
            if pivot_sidecar is not None:
                final_meta["toorow.pivot"] = pivot_sidecar
        except render_app_payload.RenderAppPayloadRefused as exc:
            raise _tool_error(exc.code, str(exc)) from exc

        walk = result_meta.get("ai_path_walk")
        step_ordinals = (
            [step["ordinal"] for step in walk.get("steps", []) if "ordinal" in step]
            if isinstance(walk, dict)
            else None
        )
        ai_path_id = result["ai_path"] if result["ai_path"] != "No AI path" else None
        feedback_context = mint_eligible_delivery_feedback_context(
            conn,
            org_id=org_id,
            project_id=project_id,
            result_id=result["id"],
            result_content_hash=result["content_hash"],
            surface="mcp_app",
            stored_rows=payload_rows,
            delivered_rows=rows,
            delivered_fields=feedback_fields_from_visualization_spec(
                payload["result_schema"], spec_version["spec"], payload["manifest"]
            ),
            ai_path_id=ai_path_id,
            path_step_ordinals=step_ordinals,
            w3c_trace_id=tracing.current_trace_id_hex(),
            pins={
                "visualization_spec_version_id": spec_version["id"],
                "renderer_build_id": pins["renderer_build"],
                "runtime_build_id": pins["runtime_build"],
                "theme_version": pins["theme_version"],
                "formatter_version": pins["formatter_version"],
            },
        )
        if feedback_context is not None:
            final_meta[SIDECAR_META_KEY] = feedback_context
        try:
            app_payload = render_app_payload.compose_render_app_payload(
                render_input=render_input, existing_meta=final_meta
            )
        except render_app_payload.RenderAppPayloadRefused as exc:
            raise _tool_error(exc.code, str(exc)) from exc
        final_meta[render_app_payload.APP_PAYLOAD_META_KEY] = app_payload
        return final_meta

    return _answer(
        project_id,
        result_id,
        RENDER_TOOL_NAME,
        meta_finalizer=finalize_meta,
    )


def _materialize_pinned_spec(
    conn,
    *,
    identity: str,
    org_id: str,
    project_id: str,
    result_id: str,
    template_version_id: str,
) -> str:
    """Apply one Chart Template to this Result and return the Spec version it wrote.

    THE MODEL CANNOT MAKE COMPATIBLE WHAT IS NOT (story 72.7, AC29). The verdict
    of `core.template_compatibility` is asked inside `materialize_template` and is
    binding: an incompatible template is refused with EVERY unsatisfied predicate
    named -- the well, the role, the cardinality bound -- and nothing is written.
    There is no nearest family, no dropped requirement and no partial document;
    the refusal travels as the tool's own `refusals` list so a model reads which
    predicate to repair instead of retrying blind.

    It writes inside the CALLER's transaction, which is `_answer`'s single one. A
    refusal further down -- an incomplete projection, an over-budget model channel
    -- therefore rolls the materialised version back with everything else: a
    Visualization Spec version that exists while its render never happened would
    be an object the person never asked for.

    `proposed_by="model"` because this is a model's proposal, taking the same
    route, the same validator and the same refusals as a human hand
    (`visualization-and-rendering.md`, "A model chooses a template by its typed
    metadata"). Nothing about the produced document says it came from a template:
    the template version is written beside it as provenance only.
    """
    from core.template_materialization import (  # noqa: PLC0415
        MaterializationRefused,
        materialize_template,
    )
    from core.visualization_specs import (  # noqa: PLC0415
        VisualizationNotFound,
        VisualizationSpecRefused,
    )

    try:
        created = materialize_template(
            conn,
            org_id=org_id,
            project_id=project_id,
            template_version_id=template_version_id,
            result_id=result_id,
            actor=identity,
            proposed_by="model",
        )
    except MaterializationRefused as exc:
        raise _tool_error(
            exc.code, str(exc), [r.as_dict() for r in exc.refusals]
        ) from exc
    except VisualizationSpecRefused as exc:
        raise _tool_error(
            exc.code, str(exc), [r.as_dict() for r in exc.refusals]
        ) from exc
    except VisualizationNotFound as exc:
        # Absent, archived or held by another Project answer identically, exactly
        # as every other read in this module answers them.
        raise _not_found() from exc
    return created["id"]


def _pivot_render_sidecar(
    conn,
    *,
    project_id: str,
    result: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Project a stored multi-source pivot for the MCP App, never in React."""
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict) or not manifest.get("plan_content_hash"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT spec FROM app.query_spec_versions WHERE id = %s AND project_id = %s",
            (result["query_spec_version_id"], project_id),
        )
        row = cur.fetchone()
    plan = row[0] if row and isinstance(row[0], dict) else {}
    pivot_request = plan.get("pivot")
    if plan.get("contract_version") != _MULTI_SOURCE_PLAN_CONTRACT or not isinstance(
        pivot_request, dict
    ):
        return None
    if result.get("outcome") not in {"success", "empty"}:
        return None
    if bool(result.get("truncated")):
        raise _tool_error(
            "truncated_result_not_pivotable",
            "This Result is a bounded prefix, so its pivot totals would be partial. "
            "Narrow the analysis and run it again.",
        )

    from core.pivot_projection import PivotRefused, project  # noqa: PLC0415

    try:
        matrix = project(
            result_id=str(result["id"]),
            content_hash=str(result["content_hash"]),
            schema=payload.get("result_schema") or {"fields": []},
            rows=payload.get("rows_chunk") or [],
            request=pivot_request,
            max_response_bytes=_MCP_PIVOT_MAX_BYTES,
            project_id=project_id,
        )
    except PivotRefused as exc:
        raise _tool_error(exc.code, exc.message) from exc
    return {
        "schema_version": "analyze-pivot-render.v1",
        "result_id": result["id"],
        "content_hash": result["content_hash"],
        "analysis_context": plan.get("analysis_context"),
        "sources": [
            {
                "datastream_id": member.get("datastream_id"),
                "name": member.get("name"),
            }
            for member in plan.get("members") or []
        ],
        "common_keys": [
            {
                "version_id": edge.get("common_key_version_id"),
                "components": [
                    component.get("canonical_name") or component.get("canonical_field_id")
                    for component in edge.get("components") or []
                ],
                "relationship": (edge.get("relationship") or {}).get("relationship_name"),
                "cardinality": (edge.get("relationship") or {}).get("cardinality"),
                "execution_safety": edge.get("measured_safety"),
            }
            for edge in plan.get("edges") or []
        ],
        "matrix": matrix,
    }


def _load_ai_path_walk(conn, *, project_id: str, ai_path: str) -> dict[str, Any]:
    """Delegate the legacy app-channel seam to the one safe projector."""
    from core import ai_paths  # noqa: PLC0415

    return ai_paths.project_observed_ai_path(conn, project_id=project_id, ai_path=ai_path)


def _semantic_view_version(conn, *, org_id, project_id, query_spec_version_id) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT semantic_view_version_id FROM app.query_spec_versions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (query_spec_version_id, org_id, project_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _result_tool_result(text: str, summary: dict, meta: dict | None):
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=summary,
        meta=meta,
    )


#: The cross-source plan contract (story 66.4). Named here rather than imported:
#: this door dispatches on a string the stored document declares, and importing
#: the analytical module for one constant would make the MCP surface depend on it
#: at module load.
_MULTI_SOURCE_PLAN_CONTRACT = "multi-source-plan.v1"



def _list_analyze_facets(project_id: str, semantic_view_id: str, semantic_view_version_id: str):
    """What a published View lets anyone ask -- as a BOUNDED list, never the whole answer.

    An agent that may only run what a human pinned cannot explore. It has to be
    able to read what is combinable and try a measure against a dimension, which
    is how a lesson is found rather than confirmed. This is that read.

    IT IS A PROJECTION, AND THAT IS THE POINT. The governed facets answer is 3241
    tokens for one View of this size; the names an agent needs to decide are 83.
    The Context Hub says it of its own discovery tools: what exists is a bounded,
    readable list, and the bodies belong to the application channel. Handing the
    whole envelope to the model would spend its window on the catalogue it was
    asked to choose from.

    Nothing is loosened by exposing it: the refusals that bound a request are
    written elsewhere and unchanged -- an illegal pair, a request spanning two
    Datastreams, a row limit. This only says what exists.
    """
    from core.analyze_workbench import WorkbenchNotFound, compose_query_facets  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    project_id = (project_id or "").strip()
    view_id = (semantic_view_id or "").strip()
    version_id = (semantic_view_version_id or "").strip()
    if not project_id or not view_id or not version_id:
        raise _tool_error(
            "missing_param",
            "project_id, semantic_view_id and semantic_view_version_id are required.",
        )

    identity = _identity()
    with analyze_connection(identity) as conn:
        decision = resolve_strict_resource_access(
            identity, conn, project_id=project_id, minimum_capability="view", hold_access=True
        )
        if not decision.allowed or not decision.org_id:
            raise _not_found()
        try:
            facets = compose_query_facets(
                conn,
                org_id=str(decision.org_id),
                project_id=project_id,
                semantic_view_id=view_id,
                semantic_view_version_id=version_id,
            )
        except WorkbenchNotFound:
            raise _not_found() from None

    time = facets.get("time") or {}
    return {
        "semantic_view_version_id": version_id,
        "status": facets.get("status"),
        "executable": facets.get("executable"),
        # Identity AND label: the agent composes with the id and speaks the label.
        "measures": [
            {"id": m.get("concept_id"), "version_id": m.get("version_id"), "label": m.get("label")}
            for m in (facets.get("measures") or [])
        ],
        "dimensions": [
            {"id": d.get("concept_id"), "version_id": d.get("version_id"), "label": d.get("label")}
            for d in (facets.get("dimensions") or [])
        ],
        "time_grains": time.get("grains") or [],
        "time_grains_unavailable_reason": time.get("grains_unavailable_reason"),
        "comparisons": time.get("comparisons") or [],
        # A COUNT, not the pairs. The legality of one pair is the compiler's
        # answer at composition time, and it names its own refusal there.
        "combinable_pairs": len(facets.get("pairs") or []),
        "row_limit_default": (facets.get("limits") or {}).get("default_row_limit"),
    }



def _explore_analyze_query(
    project_id: str,
    semantic_view_id: str,
    semantic_view_version_id: str,
    measures: list,
    dimensions: list,
    time_grain: str = "",
    last_n_days: int = 0,
    name: str = "",
):
    """Compose ONE question of one's own, then run it -- governed exactly as a pinned one.

    The pinned path answers a question somebody already asked. This one lets a
    caller try a measure against a dimension and see what comes out, which is how
    an insight is found rather than confirmed. Both paths end in the same object:
    an immutable Query Spec version and a Result that carries its evidence.

    NOTHING IS RELAXED TO MAKE IT POSSIBLE. The spec goes through the same
    `validate_query_spec` the console's Explore uses, so an illegal pair, an
    unknown member, an unsupported grain and a row limit are refused there, by
    name, before anything executes. A composed question that would span two
    Datastreams meets the same refusal as a pinned one.

    The version it writes is immutable and replayable: an exploration that found
    something can be reopened, pinned to an Answerable Topic, or handed to a
    Golden Question without being re-typed.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs import (  # noqa: PLC0415
        QuerySpecNotFound,
        QuerySpecRefused,
        create_query_spec_version,
        validate_query_spec,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    project_id = (project_id or "").strip()
    view_id = (semantic_view_id or "").strip()
    version_id = (semantic_view_version_id or "").strip()
    if not project_id or not view_id or not version_id:
        raise _tool_error(
            "missing_param",
            "project_id, semantic_view_id and semantic_view_version_id are required.",
        )
    if not measures:
        raise _tool_error("missing_param", "an analytical request needs at least one measure.")

    def _members(entries) -> list:
        out = []
        for entry in entries or []:
            if isinstance(entry, str):
                out.append({"id": entry, "version_id": ""})
            elif isinstance(entry, dict) and entry.get("id"):
                out.append(
                    {"id": str(entry["id"]), "version_id": str(entry.get("version_id") or "")}
                )
        return out

    spec: dict = {
        "measures": _members(measures),
        "dimensions": _members(dimensions),
        "filters": [],
        "sort": [],
        "comparison": "none",
    }
    # A time member is named by the caller through `dimensions`; the grain and the
    # window are the two things a bare dimension cannot carry.
    if time_grain and last_n_days > 0 and spec["dimensions"]:
        spec["time"] = {
            **spec["dimensions"][0],
            "grain": time_grain,
            "range": {"kind": "last_n_days", "days": int(last_n_days)},
        }

    identity = _identity()
    with analyze_connection(identity) as conn:
        decision = resolve_strict_resource_access(
            identity, conn, project_id=project_id, minimum_capability="edit", hold_access=True
        )
        if not decision.allowed or not decision.org_id:
            raise _not_found()
        try:
            validated = validate_query_spec(
                conn,
                project_id=project_id,
                semantic_view_id=view_id,
                semantic_view_version_id=version_id,
                payload=spec,
            )
            created = create_query_spec_version(
                conn,
                org_id=str(decision.org_id),
                project_id=project_id,
                validated=validated,
                actor=identity,
                name=name or None,
            )
            conn.commit()
        except QuerySpecNotFound:
            raise _not_found() from None
        except QuerySpecRefused as exc:
            # A refused composition is an ANSWER, not a tool failure: it names the
            # member or the rule that refused, which is what lets a caller try
            # another combination instead of guessing.
            raise _tool_error(
                "query_spec_refused",
                str(exc),
                refusals=[r.as_dict() for r in getattr(exc, "refusals", [])] or None,
            ) from None

    composed_version = created.get("id") or created.get("query_spec_version_id")
    result = _execute_analyze_query_spec(project_id, str(composed_version))
    return result


def _execute_analyze_query_spec(project_id: str, query_spec_version_id: str):
    """Produce one Result and its finalized AI Path in one scoped transaction."""
    from core import ai_path_recorder, query_execution  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    project_id = (project_id or "").strip()
    version_id = (query_spec_version_id or "").strip()
    if not project_id or not version_id:
        raise _tool_error(
            "missing_param", "project_id and query_spec_version_id are required."
        )

    identity = _identity()
    with analyze_connection(identity) as conn:
        try:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability="edit",
                hold_access=True,
            )
            if not decision.allowed or not decision.org_id:
                raise _not_found()
            org_id = str(decision.org_id)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT spec, semantic_view_version_id
                    FROM app.query_spec_versions
                    WHERE id = %s AND org_id = %s AND project_id = %s
                    """,
                    (version_id, org_id, project_id),
                )
                version = cur.fetchone()
            if version is None:
                raise _not_found()
            spec, semantic_view_version_id = version

            def execute(path_id: str) -> dict[str, Any]:
                attempt = query_execution.accept_execution(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    query_spec_version_id=version_id,
                    actor=identity,
                )
                document = spec if isinstance(spec, dict) else {}
                # STORY 66.9 -- THE SAME EXECUTOR, NOT A SECOND ONE. A
                # `multi-source-plan.v1` version carries no `measures[]`, so the
                # single-source runner would have resolved no member and written
                # an `unavailable` Result for a perfectly valid cross-source
                # analysis -- silently, with the model told "the source could not
                # produce this result". The dispatch is on the contract the
                # document declares, and it calls the module Console calls, with
                # the attempt and the AI Path this door owns.
                if document.get("contract_version") == _MULTI_SOURCE_PLAN_CONTRACT:
                    from core import multi_source_execution  # noqa: PLC0415

                    return multi_source_execution.execute_plan(
                        conn,
                        org_id=org_id,
                        project_id=project_id,
                        query_spec_version_id=version_id,
                        actor=identity,
                        attempt=attempt,
                        ai_path_id=path_id,
                        defer_terminalization=True,
                    )
                return query_execution.run_execution(
                    conn,
                    attempt=attempt,
                    org_id=org_id,
                    project_id=project_id,
                    spec=document,
                    semantic_view_version_id=str(semantic_view_version_id),
                    ai_path_id=path_id,
                    defer_terminalization=True,
                )

            result = ai_path_recorder.record_result_execution(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                tool_name=ai_path_recorder.RESULT_EXECUTION_TOOL_NAME,
                execute=execute,
                arguments={"query_spec_version_id": query_spec_version_id},
            )
            result = query_execution.complete_deferred_result(conn, result)
            text, summary, meta = _compose_answer_on_connection(
                conn,
                identity=identity,
                org_id=org_id,
                project_id=project_id,
                result_id=str(result["result_id"]),
                tool_name=ai_path_recorder.RESULT_EXECUTION_TOOL_NAME,
                include_ai_path_walk=True,
            )
            tool_result = _result_tool_result(text, summary, meta)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return tool_result


def _discover_analyze_matches(
    project_id: str, context_offset: int = 0, context_kind: str = "all"
) -> dict[str, Any]:
    """Project the bounded governed matching catalog for model composition.

    The model sees identities, labels, keys and measure choices. It never sees
    samples or full Result rows, and it never infers a join from same-looking
    physical columns: only the server's governed catalog is returned.
    """
    from core.datastream_matches import MatchesUnavailable, discover_matches  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    project_id = (project_id or "").strip()
    if not project_id:
        raise _tool_error("missing_param", "project_id is required.")
    if isinstance(context_offset, bool) or not isinstance(context_offset, int):
        raise _tool_error("invalid_param", "context_offset is a whole number.")
    if context_offset < 0:
        raise _tool_error("invalid_param", "context_offset is not negative.")
    if context_kind not in {"all", "business_domains", "golden_questions", "requested_skills"}:
        raise _tool_error("invalid_param", "context_kind is not supported.")
    identity = _identity()
    decision = _guard_project_view(project_id, identity)
    try:
        with analyze_connection(identity) as conn:
            catalog = discover_matches(conn, project_id=project_id)
            semantic_view_version_ids = sorted(
                {
                    str((match.get("explore_together") or {}).get("view_version_id") or "")
                    for match in catalog.get("matches") or []
                    if (match.get("explore_together") or {}).get("view_version_id")
                }
            )
            try:
                context_options = _discover_analysis_context_options(
                    conn,
                    project_id=project_id,
                    semantic_view_version_ids=semantic_view_version_ids,
                    offset=context_offset,
                )
            except Exception as exc:  # noqa: BLE001 -- optional guidance never hides matches.
                logger.error(
                    "analyze_render_mcp: context option discovery failed: %s",
                    type(exc).__name__,
                )
                context_options = _unavailable_analysis_context_options(
                    semantic_view_version_ids, offset=context_offset
                )
            _select_analysis_context_kind(context_options, context_kind)
    except MatchesUnavailable as exc:
        raise _tool_error(
            "matches_unavailable", "The governed match catalog is unavailable."
        ) from exc

    matches = []
    for match in catalog.get("matches") or []:
        left = match.get("left") or {}
        right = match.get("right") or {}
        key = match.get("common_key")
        matches.append(
            {
                "authority": match.get("authority"),
                "execution_safety": match.get("execution_safety"),
                "analysis": match.get("analysis"),
                "sources": [
                    {
                        "datastream_id": side.get("datastream_id"),
                        "name": side.get("name"),
                        "mapping_version_id": side.get("mapping_version_id"),
                        "published_execution_id": side.get("published_execution_id"),
                        "output_version_id": side.get("output_version_id"),
                        "measures": side.get("measures") or [],
                    }
                    for side in (left, right)
                ],
                "common_key": key,
                "key_paths": match.get("key_paths") or [],
                "relationship": match.get("relationship"),
                "compose_request": match.get("explore_together"),
                "next_action": match.get("next_action"),
            }
        )
    response = {
        "schema_version": "analyze-match-catalog.v1",
        "project_id": project_id,
        "organization_id": str(decision.org_id),
        "matches": matches,
        "counts": catalog.get("counts") or {},
        "bounds": {
            **(catalog.get("bounds") or {}),
            "max_response_bytes": _MCP_MATCH_CATALOG_MAX_BYTES,
            "response_bytes": 0,
        },
        "empty_reason": catalog.get("empty_reason"),
        "analysis_context_options": context_options,
    }
    return _bound_analyze_match_catalog(response)


def _discover_analysis_context_options(
    conn,
    *,
    project_id: str,
    semantic_view_version_ids: list[str],
    offset: int = 0,
) -> dict[str, Any]:
    """Return compact exact pins the model may use with the discovered plans.

    This deliberately does not reuse the Console list endpoints: those responses
    contain descriptions or bodies that are useful to a human workspace but not
    needed to compose an exact request. The compiler remains the authority and
    revalidates every selected id/version.
    """
    domains: list[tuple[Any, ...]] = []
    questions: list[tuple[Any, ...]] = []
    domain_truncated = False
    question_truncated = False
    if semantic_view_version_ids:
        with conn.cursor() as cur:
            # Story 49.2: the pin options resolve through the authority, so the
            # domains a compiler will ACCEPT and the domains this catalogue OFFERS
            # are the same set. The NAME comes from the identity rather than from
            # a legacy version row -- the authority's ledger carries no name
            # column, and the identity is what a person reads in the picker.
            cur.execute(
                f"""
                WITH ranked AS (
                    SELECT DISTINCT svv.id AS semantic_view_version_id,
                           d.id AS domain_id, v.version_number, d.name,
                           row_number() OVER (
                               PARTITION BY svv.id ORDER BY d.name, d.id
                           ) AS option_rank
                      FROM app.semantic_view_versions svv
                      JOIN app.projects p ON p.id = svv.project_id
                      JOIN {catalogue.DOMAIN_SOURCE} d
                        ON d.org_id = p.org_id AND d.status = 'active'
                      JOIN {catalogue.DOMAIN_VERSION_SOURCE} v
                        ON v.domain_id = d.id AND v.org_id = d.org_id
                       AND v.version_number = (
                           SELECT max(v2.version_number)
                             FROM {catalogue.DOMAIN_VERSION_SOURCE} v2
                            WHERE v2.domain_id = d.id AND v2.org_id = d.org_id
                       )
                     WHERE svv.project_id = %s AND svv.id = ANY(%s)
                       AND svv.business_domain_refs ? d.id
                )
                SELECT semantic_view_version_id, domain_id, version_number, name
                  FROM ranked
                 ORDER BY option_rank, semantic_view_version_id, name, domain_id
                 LIMIT %s OFFSET %s
                """,
                (
                    project_id,
                    semantic_view_version_ids,
                    _MCP_CONTEXT_OPTIONS_LIMIT + 1,
                    offset,
                ),
            )
            domains = cur.fetchall()
        domain_truncated = len(domains) > _MCP_CONTEXT_OPTIONS_LIMIT
        domains = domains[:_MCP_CONTEXT_OPTIONS_LIMIT]

        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ranked AS (
                    SELECT v.semantic_view_version_id, q.id AS question_id,
                           v.id AS version_id, v.version_number, q.title,
                           q.lifecycle, v.business_domain_id,
                           v.business_domain_version_number,
                           row_number() OVER (
                               PARTITION BY v.semantic_view_version_id
                               ORDER BY q.title, q.id
                           ) AS option_rank
                      FROM app.golden_questions q
                      JOIN app.golden_question_versions v
                        ON v.id = q.current_version_id
                       AND v.org_id = q.org_id AND v.project_id = q.project_id
                      JOIN app.semantic_view_versions svv
                        ON svv.id = v.semantic_view_version_id
                       AND svv.project_id = v.project_id
                     WHERE q.project_id = %s AND v.semantic_view_version_id = ANY(%s)
                       AND q.lifecycle = 'active'
                       AND svv.business_domain_refs ? v.business_domain_id
                )
                SELECT semantic_view_version_id, question_id, version_id,
                       version_number, title, lifecycle, business_domain_id,
                       business_domain_version_number
                  FROM ranked
                 ORDER BY option_rank, semantic_view_version_id, title, question_id
                 LIMIT %s OFFSET %s
                """,
                (
                    project_id,
                    semantic_view_version_ids,
                    _MCP_CONTEXT_OPTIONS_LIMIT + 1,
                    offset,
                ),
            )
            questions = cur.fetchall()
        question_truncated = len(questions) > _MCP_CONTEXT_OPTIONS_LIMIT
        questions = questions[:_MCP_CONTEXT_OPTIONS_LIMIT]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.name, max(v.version_number),
                   CASE WHEN p.project_id IS NULL THEN 'platform' ELSE 'project' END
              FROM app.procedures p
              JOIN app.procedures_versions v ON v.procedure_id = p.id
             WHERE (p.project_id IS NULL OR p.project_id = %s)
               AND p.status = 'active'
             GROUP BY p.id, p.name, p.project_id
             ORDER BY CASE WHEN p.project_id IS NULL THEN 1 ELSE 0 END,
                      p.name, p.id
             LIMIT %s OFFSET %s
            """,
            (project_id, _MCP_CONTEXT_OPTIONS_LIMIT + 1, offset),
        )
        skill_rows = cur.fetchall()
    skill_truncated = len(skill_rows) > _MCP_CONTEXT_OPTIONS_LIMIT
    skill_rows = skill_rows[:_MCP_CONTEXT_OPTIONS_LIMIT]

    by_view = {
        view_id: {
            "semantic_view_version_id": view_id,
            "business_domains": [],
            "golden_questions": [],
        }
        for view_id in semantic_view_version_ids
    }
    for view_id, domain_id, version_number, name in domains:
        by_view[str(view_id)]["business_domains"].append(
            {
                "id": str(domain_id),
                "version_number": int(version_number),
                "version_id": f"{domain_id}:{version_number}",
                "name": str(name)[:160],
            }
        )
    for (
        view_id,
        question_id,
        version_id,
        version_number,
        title,
        lifecycle,
        domain_id,
        domain_version,
    ) in questions:
        by_view[str(view_id)]["golden_questions"].append(
            {
                "id": str(question_id),
                "version_id": str(version_id),
                "version_number": int(version_number),
                "title": str(title)[:160],
                "lifecycle": str(lifecycle),
                "business_domain_id": str(domain_id),
                "business_domain_version_number": int(domain_version),
            }
        )

    from core.multi_source_plan import MAX_REQUESTED_SKILLS  # noqa: PLC0415

    return {
        "contract_version": "analysis-context-options.v1",
        "state": "available",
        "semantic_views": list(by_view.values()),
        "requested_skills": [
            {
                "procedure_id": str(procedure_id),
                "version_number": int(version_number),
                "version_id": f"{procedure_id}@{version_number}",
                "name": str(name)[:160],
                "scope": str(scope),
            }
            for procedure_id, name, version_number, scope in skill_rows
        ],
        "bounds": {
            "max_business_domains": _MCP_CONTEXT_OPTIONS_LIMIT,
            "max_golden_questions": _MCP_CONTEXT_OPTIONS_LIMIT,
            "max_requested_skills": _MCP_CONTEXT_OPTIONS_LIMIT,
            "max_selected_skills": MAX_REQUESTED_SKILLS,
            "context_offset": offset,
            "next_context_offset": offset + _MCP_CONTEXT_OPTIONS_LIMIT
            if domain_truncated or question_truncated or skill_truncated
            else None,
            "business_domains_truncated": domain_truncated,
            "golden_questions_truncated": question_truncated,
            "requested_skills_truncated": skill_truncated,
        },
    }


def _unavailable_analysis_context_options(
    semantic_view_version_ids: list[str], *, offset: int = 0,
) -> dict[str, Any]:
    from core.multi_source_plan import MAX_REQUESTED_SKILLS  # noqa: PLC0415

    return {
        "contract_version": "analysis-context-options.v1",
        "state": "unavailable",
        "reason": "unavailable",
        "semantic_views": [
            {
                "semantic_view_version_id": view_id,
                "business_domains": [],
                "golden_questions": [],
            }
            for view_id in semantic_view_version_ids
        ],
        "requested_skills": [],
        "bounds": {
            "max_business_domains": _MCP_CONTEXT_OPTIONS_LIMIT,
            "max_golden_questions": _MCP_CONTEXT_OPTIONS_LIMIT,
            "max_requested_skills": _MCP_CONTEXT_OPTIONS_LIMIT,
            "max_selected_skills": MAX_REQUESTED_SKILLS,
            "context_offset": offset,
            "next_context_offset": None,
            "business_domains_truncated": False,
            "golden_questions_truncated": False,
            "requested_skills_truncated": False,
        },
    }


def _select_analysis_context_kind(options: dict[str, Any], kind: str) -> None:
    """Project one independently pageable option kind without changing authority."""

    if kind == "all":
        return
    bounds = options["bounds"]
    if kind != "requested_skills":
        options["requested_skills"] = []
        bounds["requested_skills_truncated"] = False
    for view in options["semantic_views"]:
        if kind != "business_domains":
            view["business_domains"] = []
            bounds["business_domains_truncated"] = False
        if kind != "golden_questions":
            view["golden_questions"] = []
            bounds["golden_questions_truncated"] = False


def _bound_analyze_match_catalog(response: dict[str, Any]) -> dict[str, Any]:
    """Fit the model-visible catalog exactly, trimming guidance before matches."""

    options = response["analysis_context_options"]
    for view in options["semantic_views"]:
        for domain in view["business_domains"]:
            domain["name"] = str(domain.get("name") or "")[:160]
        for question in view["golden_questions"]:
            question["title"] = str(question.get("title") or "")[:160]
    for skill in options["requested_skills"]:
        skill["name"] = str(skill.get("name") or "")[:160]

    def measure() -> int:
        return len(
            json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )

    bounds = response["bounds"]
    option_bounds = options["bounds"]

    def preserve_context_continuation() -> None:
        continuations: list[dict[str, int | str]] = []
        offset = int(option_bounds.get("context_offset") or 0)
        if option_bounds["business_domains_truncated"]:
            count = sum(len(view["business_domains"]) for view in options["semantic_views"])
            if count:
                continuations.append(
                    {
                        "context_kind": "business_domains",
                        "context_offset": offset + count,
                    }
                )
        if option_bounds["golden_questions_truncated"]:
            count = sum(len(view["golden_questions"]) for view in options["semantic_views"])
            if count:
                continuations.append(
                    {
                        "context_kind": "golden_questions",
                        "context_offset": offset + count,
                    }
                )
        if option_bounds["requested_skills_truncated"]:
            count = len(options["requested_skills"])
            if count:
                continuations.append(
                    {
                        "context_kind": "requested_skills",
                        "context_offset": offset + count,
                    }
                )
        option_bounds["continuations"] = continuations

    while True:
        preserve_context_continuation()
        for _ in range(8):
            measured = measure()
            if bounds["response_bytes"] == measured:
                break
            bounds["response_bytes"] = measured
        stable = measure()
        bounds["response_bytes"] = stable
        if measure() == stable and stable <= _MCP_MATCH_CATALOG_MAX_BYTES:
            return response

        trimmed = False
        if len(options["requested_skills"]) > 1:
            options["requested_skills"].pop()
            option_bounds["requested_skills_truncated"] = True
            trimmed = True
        elif response["matches"]:
            response["matches"].pop()
            bounds["truncated"] = True
            response["counts"] = {
                **response["counts"],
                "returned": len(response["matches"]),
            }
            trimmed = True
        if not trimmed:
            raise RuntimeError("The empty MCP match catalog exceeds its response byte bound.")


def _compose_analyze_pivot(
    project_id: str,
    request: dict[str, Any],
    family: str = "table",
    name: str | None = None,
) -> dict[str, Any]:
    """Freeze one governed multi-source plan and its initial presentation.

    ``request`` is the exact closed ``multi-source-plan.v1`` input used by the
    Console. The server resolves and freezes every mapping, Output, relationship
    and measure identity. The presentation binds only fields the compiled plan
    exposes, so duplicated canonical measures keep their source-qualified names.

    The optional ``analysis_context`` carries exact governed ids only:
    Business Domain id+version, a compatible Golden Question version id and
    ``procedure_id@version`` requested Skill pins. They are inherited by the
    Result and render sidecar; requested Skills never masquerade as AI Path
    evidence that execution actually used them.
    """
    from core.match_profile import ProfileRefused, profile_match  # noqa: PLC0415
    from core.multi_source_plan import (  # noqa: PLC0415
        PlanRefused,
        compile_plan,
        store_plan_version,
    )
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415
    from core.visualization_specs import (  # noqa: PLC0415
        VISUALIZATION_SPEC_CONTRACT_VERSION,
        VISUALIZATION_SPEC_SCHEMA_VERSION,
        VisualizationNotFound,
        VisualizationSpecRefused,
        create_visualization_spec_version,
        validate_visualization_spec,
    )

    project_id = (project_id or "").strip()
    if not project_id or not isinstance(request, dict):
        raise _tool_error("missing_param", "project_id and a plan request are required.")
    identity = _identity()
    conn = None
    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability="edit",
                hold_access=True,
            )
            if not decision.allowed or not decision.org_id:
                raise _not_found()

            def profile(
                left: str,
                right: str,
                key_version_id: str,
                relationship_name: str,
                view_version_id: str,
            ):
                try:
                    return profile_match(
                        conn,
                        project_id=project_id,
                        left_datastream_id=left,
                        right_datastream_id=right,
                        common_key_version_id=key_version_id,
                        relationship_name=relationship_name,
                        view_version_id=view_version_id,
                        window=request.get("filters") or [],
                    )
                except ProfileRefused as exc:
                    raise PlanRefused(
                        "profile_unavailable",
                        "The selected match could not be measured, so it was not compiled.",
                        detail={"code": exc.code, "missing_link": exc.missing_link},
                    ) from exc

            compose_request = deepcopy(request)
            if "pivot" not in compose_request:
                raise PlanRefused(
                    "pivot_required",
                    "compose_analyze_pivot requires explicit Rows, Columns, Values and "
                    "total policies; MCP never invents analytical wells.",
                )
            compiled = compile_plan(
                conn, project_id=project_id, request=compose_request, profile_lookup=profile
            )
            stored = store_plan_version(
                conn,
                org_id=str(decision.org_id),
                project_id=project_id,
                compiled=compiled,
                actor=identity,
                name=name,
            )
            plan = compiled["plan"]
            pivot_request = plan["pivot"]
            dimensions = [*pivot_request["rows"], *pivot_request["columns"]]
            measures = list(pivot_request["values"])
            document = {
                "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
                "schema_version": VISUALIZATION_SPEC_SCHEMA_VERSION,
                "family": (family or "table").strip(),
                "bindings": {"measure": measures, "dimension": dimensions},
            }
            validated = validate_visualization_spec(
                conn,
                project_id=project_id,
                query_spec_version_id=stored["query_spec_version_id"],
                payload=document,
            )
            visualization = create_visualization_spec_version(
                conn,
                org_id=str(decision.org_id),
                project_id=project_id,
                validated=validated,
                actor=identity,
                proposed_by="model",
                name=name,
            )
            conn.commit()
    except PlanRefused as exc:
        raise _tool_error(exc.code, exc.message) from exc
    except VisualizationSpecRefused as exc:
        raise _tool_error(exc.code, json.dumps(exc.as_dict(), separators=(",", ":"))) from exc
    except VisualizationNotFound as exc:
        raise _not_found() from exc
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        raise

    return {
        "schema_version": "analyze-pivot-composition.v1",
        "project_id": project_id,
        **stored,
        "visualization_id": visualization["visualization_id"],
        "visualization_spec_version_id": visualization["id"],
        "visualization_content_hash": visualization["content_hash"],
        "family": visualization["family"],
        "result_fields": {"dimensions": dimensions, "measures": measures},
        "pivot": pivot_request,
        "analysis_context": plan.get("analysis_context"),
        "next": {
            "execute_tool": "execute_analyze_query_spec",
            "render_tool": RENDER_TOOL_NAME,
        },
    }


def _submit_analyze_feedback(
    context: dict[str, Any],
    target: dict[str, Any],
    polarity: str,
    comment: str | None,
    retry_key: str,
):
    """Authenticated app-only adapter over the canonical exact writer."""
    from core.analyze_feedback import FeedbackContextError, verify_feedback_context  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.feedback_review import (  # noqa: PLC0415
        FeedbackNotFound,
        FeedbackRefused,
        submit_exact_feedback,
    )

    try:
        try:
            claims = verify_feedback_context(context)
        except FeedbackContextError as exc:
            if exc.code != "feedback_context_expired" or not exc.verified_claims:
                raise
            claims = exc.verified_claims
        project_id = str(claims["project_id"])
    except (FeedbackContextError, KeyError, TypeError) as exc:
        raise _tool_error("invalid_context", "The feedback context is not valid.") from exc

    identity = _identity()
    decision = _guard_project_view(project_id, identity)
    try:
        with request_connection(identity) as conn:
            try:
                receipt = submit_exact_feedback(
                    conn,
                    org_id=str(decision.org_id),
                    project_id=project_id,
                    actor=identity,
                    payload={
                        "context": context,
                        "target": target,
                        "polarity": polarity,
                        "comment": comment,
                        "retry_key": retry_key,
                    },
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except FeedbackNotFound as exc:
        raise _not_found() from exc
    except FeedbackRefused as exc:
        raise _tool_error(exc.code, str(exc)) from exc
    from core.analyze_feedback import score_feedback_after_commit  # noqa: PLC0415

    score_feedback_after_commit(
        status=str(receipt.get("status")),
        trace_id=claims.get("w3c_trace_id"),
        polarity=polarity,
        comment=comment,
    )
    return receipt


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


# ---- Analytics catalog: compact governed crosses, never full data ------------
def discover_analyze_matches(
    project_id: str,
    context_offset: int = 0,
    context_kind: str = "all",
):
    """List bounded governed multi-Datastream opportunities for one Project.

    Returns exact source, mapping, published Output, common-key, relationship
    and measure identities, plus view-compatible Business Domain and active
    Golden Question options grouped by Semantic View version. Active Project
    and platform Skill versions are separate requested-intent options; they are
    not presented as Semantic View compatibility. The response contains no
    samples, matrix rows, Question documents or Skill bodies.
    Candidate paths are labelled as requiring governance and cannot be composed.
    When context options are truncated, follow one entry from
    `analysis_context_options.bounds.continuations`: pass both its
    `context_kind` and strictly greater `context_offset`. The governed
    match catalog remains unchanged.
    """
    return _discover_analyze_matches(project_id, context_offset, context_kind)


# ---- Analytics composition: freeze the same plan + spec as Console -----------
def compose_analyze_pivot(
    project_id: str,
    request: dict[str, Any],
    family: str = "table",
    name: str | None = None,
):
    """Freeze a governed multi-Datastream pivot and initial table/chart spec.

    Use only exact ids returned by ``discover_analyze_matches``. ``request``
    is the Console's closed plan input: members, edges, dimensions, grain,
    inclusion policy, filters, bounds and optional exact ``analysis_context``
    ids returned by discovery. Raw SQL, names and prose are refused by the
    compiler. The response gives the Query Spec and Visualization Spec
    ids required by ``execute_analyze_query_spec`` and
    ``render_analyze_result``. This prepares immutable artifacts and requires
    the host's server-confirmed operations capability.
    """
    return _compose_analyze_pivot(project_id, request, family, name)


# ---- The pre-query gate, measured on every Analyze data question (AD-18) -----
#
# `analyze-and-test.md`, amendment of 2026-09-01. Three real Analyze sessions
# ran `get_procedure` -> `get_knowledge` -> `execute_analyze_query_spec` and
# `get_context_adherence` answered "No adherence observation": the gate measured
# the report and card tools and nothing here. The hook is the one
# `reporting_mcp._apply_pre_query_gate` plays for those three -- best-effort,
# never raising, verdict echoed on `ToolResult.meta["gate"]` off the model
# channel -- and it runs in the PUBLIC wrappers, after the private body has
# committed the Result and its AI Path, so `observed_path_for` finds the path
# under the client's trace and the verdict rests on `observed_ai_path` rather
# than on the wall-clock inference. `_explore_analyze_query` delegates to
# `_execute_analyze_query_spec`; recording in the wrappers is what keeps one
# question from being measured twice under two names.
def _record_data_question(tool_result, data_tool: str, project_id: str):
    """Measure adherence for one delivered Analyze answer. NEVER raises."""
    try:
        from core import adherence as _adherence  # noqa: PLC0415
        from core.main import _current_identity  # noqa: PLC0415

        verdict = _adherence.record_data_query(
            data_tool,
            project_id=(project_id or "").strip() or None,
            trace_id=_adherence.current_exchange_trace_id(),
            identity=_current_identity(),
        )
        meta = dict(tool_result.meta or {})
        meta["gate"] = {
            "adherent": bool(verdict.get("adherent")),
            "context_tool": verdict.get("context_tool"),
            "session_kind": verdict.get("session_kind"),
            "adherence_basis": verdict.get("adherence_basis"),
        }
        tool_result.meta = meta
    except Exception as _gate_exc:  # noqa: BLE001 -- the gate must never break the tool
        logger.debug("%s: pre_query_gate_skipped: %s", data_tool, _gate_exc)
    return tool_result


# ---- Data tool: execute one governed Query Spec, producing one Result -------
def execute_analyze_query_spec(project_id: str, query_spec_version_id: str):
    """Execute one immutable Query Spec version and return its compact Result.

    The Result and its completed observed AI Path are committed together.
    Analytical refused/unavailable outcomes are delivered Results, not tool
    failures. This data tool advertises no app resource.
    """
    tool_result = _execute_analyze_query_spec(project_id, query_spec_version_id)
    return _record_data_question(tool_result, "execute_analyze_query_spec", project_id)


# ---- Data tools: read what is combinable, then compose one's own question ----
#
# The pinned path answers a question somebody already asked. These two let a
# caller browse a published View and try a measure against a dimension, which
# is how a lesson is found rather than confirmed. Both stay `insights`: the
# first reads, the second writes only an immutable Query Spec version and
# produces a Result, exactly as the pinned path does.
def list_analyze_facets(
    project_id: str, semantic_view_id: str, semantic_view_version_id: str
):
    """What a published Semantic View lets anyone ask, as a bounded list.

    Measures and dimensions with their exact version, the time grains, the
    comparisons, and how many pairs are combinable. The governed envelope is
    an order of magnitude larger and belongs to the application channel; this
    is the list a caller chooses from.
    """
    return _list_analyze_facets(project_id, semantic_view_id, semantic_view_version_id)


def explore_analyze_query(
    project_id: str,
    semantic_view_id: str,
    semantic_view_version_id: str,
    measures: list,
    dimensions: list,
    time_grain: str = "",
    last_n_days: int = 0,
    name: str = "",
):
    """Compose one analytical question and run it, governed as a pinned one.

    The spec goes through the same validator the console's Explore uses: an
    illegal pair, an unknown member, an unsupported grain or a request that
    would span two Datastreams is refused by name, before anything executes.
    What it writes is an immutable Query Spec version, so an exploration that
    found something can be pinned afterwards without being re-typed.
    """
    tool_result = _explore_analyze_query(
        project_id,
        semantic_view_id,
        semantic_view_version_id,
        measures,
        dimensions,
        time_grain,
        last_n_days,
        name,
    )
    return _record_data_question(tool_result, "explore_analyze_query", project_id)


# ---- Data tool: compact answer, Result identity, bounded evidence, NO widget ---
def analyze_result(project_id: str, result_id: str):
    """Answer from one immutable Result: compact summary, identity, bounded evidence.

    Returns the outcome, the headline counts, the exact Query Spec version and
    AI path, a provenance and freshness summary projected from the frozen
    manifest (`unavailable` where the manifest is silent), bounded evidence and
    an exact authenticated deep link to the Result workbench. It attaches NO
    widget resource: only the render tool advertises one. Large row data never
    enters the model-visible channel -- it travels in result `_meta`, bounded,
    behind a scoped opaque handle. Pure read; mutates nothing.
    """
    text, summary, meta = _answer(project_id, result_id, "analyze_result")
    tool_result = _result_tool_result(text, summary, meta)
    return _record_data_question(tool_result, "analyze_result", project_id)


# ---- App-only: the manifest of one Result, behind a handle --------------------
def app_read_result_manifest(project_id: str, handle: str):
    """Return the frozen schema and manifest for a Result handle (app-only read).

    Not a model-callable analytical tool: it exists for a mounted widget. The
    caller's Project access is resolved BEFORE the grant is loaded, and the
    grant can only narrow that decision.
    """
    from core.query_specs_api import analyze_connection  # noqa: PLC0415
    from core.result_slices import SliceRefused, read_manifest  # noqa: PLC0415

    identity = _identity()
    try:
        with analyze_connection(identity) as conn:
            return read_manifest(
                conn, handle_id=handle, identity=identity, project_id=project_id
            )
    except SliceRefused as exc:
        raise _tool_error(exc.code, exc.message) from exc


# ---- App-only: one bounded page of a frozen Result ----------------------------
def app_read_result_slice(
    project_id: str,
    handle: str,
    feedback_context: dict[str, Any] | None = None,
    columns: list[str] | None = None,
    cursor: str | None = None,
    offset: int | None = None,
    limit: int = 100,
):
    """Return one bounded page of an immutable Result (app-only read).

    Bounded on every axis: at most 500 rows and 256 KiB per page, allowlisted
    columns only, cursor and offset mutually exclusive. An over-bound limit is
    REFUSED, never silently reduced. Result bytes stay immutable; a feedback-
    capable delivery atomically appends eligibility before return. Not a
    model-callable analytical tool.
    """
    from core.analyze_feedback import (  # noqa: PLC0415
        SIDECAR_META_KEY,
        FeedbackContextError,
        remint_result_slice_feedback_context,
        verify_feedback_context,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415
    from core.result_app_grants import touch_handle  # noqa: PLC0415
    from core.result_slices import SliceRefused, read_slice  # noqa: PLC0415

    identity = _identity()
    try:
        with analyze_connection(identity) as conn:
            page = read_slice(
                conn,
                handle_id=handle,
                identity=identity,
                project_id=project_id,
                columns=columns,
                cursor=cursor,
                offset=offset,
                limit=limit,
            )
            if feedback_context is None:
                if not touch_handle(conn, handle_id=handle):
                    raise _not_found()
                conn.commit()
                return page
            try:
                claims = verify_feedback_context(feedback_context)
            except FeedbackContextError as exc:
                if exc.code != "feedback_context_expired" or not exc.verified_claims:
                    raise _not_found() from exc
                claims = exc.verified_claims
            if claims.get("project_id") != project_id:
                raise _not_found()
            sidecar = remint_result_slice_feedback_context(
                conn, claims=claims, page=page
            )
            # The idle bound is recorded by the GRANT module, never by the
            # reader: a reader that can write is a reader whose read cannot be
            # proved side-effect free (AC7).
            if not touch_handle(conn, handle_id=handle):
                raise _not_found()
            conn.commit()
            if sidecar is None:
                return page
            return _result_tool_result(
                phrase("result_slice_loaded"),
                page,
                {SIDECAR_META_KEY: sidecar},
            )
    except SliceRefused as exc:
        raise _tool_error(exc.code, exc.message) from exc
    except FeedbackContextError as exc:
        raise _not_found() from exc


# ---- App-only: append one exact human observation ----------------------------
def submit_analyze_feedback(
    context: dict[str, Any],
    target: dict[str, Any],
    polarity: str,
    comment: str | None,
    retry_key: str,
):
    """Record one reaction to the exact signed Result/datum/path-step target."""
    return _submit_analyze_feedback(context, target, polarity, comment, retry_key)


# ---- Render tool: the SAME summary, plus the ONE shared MCP App resource -------
#
# Registered only when the resource it binds actually resolves. Advertising an
# unresolvable resource is a broken widget, not a partial one -- and binding to
# `ui://core/daily-report` or a `ui://core/card-*` resource as a stand-in would
# re-create the connector-owned coupling Story 50.5 deleted, while looking like
# progress.
#
# THE CHART TEMPLATE DOOR INHERITS THAT CONDITION (story 72.7). It is a parameter
# of THIS tool -- never a tool of its own, because D3 of epic 72 settled that the
# assembled catalog stays at its budget -- so a deployment without the runtime
# offers neither the render nor the template path, and the bounded block in
# `analyze_result` disappears with them. A template door that opened onto some
# other drawing path would be the fallback renderer this surface has refused to
# build since Story 50.5.
def render_analyze_result(
    project_id: str,
    result_id: str,
    visualization_spec_version_id: str | None = None,
    visualization_template_version_id: str | None = None,
):
    """Render one immutable Result through the shared Visualization runtime.

    Returns the SAME concise model-facing summary as `analyze_result` for the
    same Result, and additionally attaches the shared MCP App resource. It
    performs no analysis of its own: it reads the same Result the data tool
    reads. A pinned Visualization Spec version selects the presentation; it
    never changes the answer, because a semantic change is a new execution and
    a new Result (AD-10). Send instead a Chart Template version from the
    `chart_templates` block of the answer to materialise a presentation for this
    Result; the two are exclusive, and an incompatible template is refused.
    """
    text, summary, meta = _render_answer(
        project_id,
        result_id,
        visualization_spec_version_id,
        visualization_template_version_id,
    )
    tool_result = _result_tool_result(text, summary, meta)
    return _record_data_question(tool_result, RENDER_TOOL_NAME, project_id)


def register(mcp) -> None:
    """Register the Analyze data tool, the two app-only readers and the render tool.

    Called once from `core.main`, BEFORE `validate_catalog()`, or the boot
    validator would not see these declarations -- and the whole point of AC12 is
    that it does.
    """
    from fastmcp.apps import AppConfig  # noqa: PLC0415

    from core.analyze_feedback import feedback_context_secret  # noqa: PLC0415
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # OAuth/networked deployments fail at boot rather than first user feedback.
    feedback_context_secret()

    register_profiled(
        mcp,
        discover_analyze_matches,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    register_profiled(
        mcp,
        compose_analyze_pivot,
        profile="operations",
        effect="prepare",
        data_class="operational",
        confirmation_mode="server",
    )

    register_profiled(
        mcp,
        execute_analyze_query_spec,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    register_profiled(
        mcp,
        list_analyze_facets,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    register_profiled(
        mcp,
        explore_analyze_query,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    register_profiled(
        mcp, analyze_result,
        profile="insights", effect="read",
        data_class="operational", confirmation_mode="none",
    )

    register_profiled(
        mcp, app_read_result_manifest,
        profile="insights", effect="read",
        data_class="operational", confirmation_mode="none",
        app=AppConfig(visibility=["app"]),
    )

    register_profiled(
        mcp, app_read_result_slice,
        profile="insights", effect="read",
        data_class="operational", confirmation_mode="none",
        app=AppConfig(visibility=["app"]),
    )

    register_profiled(
        mcp,
        submit_analyze_feedback,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
        app=AppConfig(visibility=["app"]),
    )

    render_analyze_result.__name__ = RENDER_TOOL_NAME

    if _visualization_runtime_available():
        register_profiled(
            mcp, render_analyze_result,
            profile="insights", effect="read",
            data_class="operational", confirmation_mode="none",
            app=AppConfig(resource_uri=VISUALIZATION_RUNTIME_URI),
        )
        REGISTRATION_STATE.update(
            {
                "render_tool_registered": True,
                "render_tool_resource_uri": VISUALIZATION_RUNTIME_URI,
                "render_tool_absent_reason": None,
            }
        )
    else:
        REGISTRATION_STATE.update(
            {
                "render_tool_registered": False,
                "render_tool_resource_uri": None,
                "render_tool_absent_reason": RENDER_TOOL_ABSENT_REASON,
            }
        )
        logger.warning(
            "analyze_render_mcp: render tool NOT registered (%s, owned by story %s)",
            RENDER_TOOL_ABSENT_REASON,
            RENDER_TOOL_OWNING_STORY,
        )


def _visualization_runtime_available() -> bool:
    """True only when `VISUALIZATION_RUNTIME_URI` resolves to a real bundle.

    Named by its CONSTANT, not by its literal, and not only for tidiness: Story
    50.5 asserts that no module outside `visualization_runtime_resource.py` and
    `main.py` contains that URI string at all, so that two modules can never
    register the same URI in a silent last-writer-wins. Spelling it here -- even
    inside a docstring -- would break that guard. Importing the constant is what
    keeps the tool and the resource one identifier instead of two spellings.


    Story 50.5's resource never raises: a missing bundle serves an honest
    not-built placeholder. That is right for a resource and wrong for a tool
    binding -- a tool that advertises a widget which renders "not built" is a
    broken widget. So the render tool checks the BUNDLE, not the registration.
    """
    from core.render_app_payload import (  # noqa: PLC0415
        RenderAppPayloadRefused,
        load_runtime_manifest,
    )
    from core.visualization_runtime_resource import runtime_bundle_path  # noqa: PLC0415

    try:
        load_runtime_manifest(runtime_bundle_path())
        return True
    except (RenderAppPayloadRefused, OSError):
        return False
