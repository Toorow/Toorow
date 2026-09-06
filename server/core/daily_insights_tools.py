"""Daily-insight MCP tool logic (Epic 35, Story 35.2).

Gate 35.0 decided A. These are the bounded operations the client-LLM scheduled task
calls: check J-1 readiness, discover what a project can render, preview a candidate
insight without publishing, and publish 0..3 insights atomically.

All logic lives here with INJECTED dependencies (availability, freshness, project scope,
existing slots, a connection, ``record_run``) so it is unit-testable offline and keeps the
contended ``server/core/main.py`` to a few thin wrappers. The module reuses, never rebuilds:
  - validation  -> ``daily_insights_schema.validate_published_insight`` (35.0);
  - catalogue   -> ``daily_insights_card_contract`` (35.1);
  - persistence -> ``daily_insights.record_run`` (35.3).

ASCII-only stdout (L-3).
"""

from __future__ import annotations

from core.daily_insights import InsightItem, record_run
from core.daily_insights_card_contract import (
    CARD_KEY_CONTRACT_VERSION,
    agent_card_catalog,
    recommend_card,
)
from core.daily_insights_schema import (
    SCHEMA_VERSION,
    authorship_block,
    enriched_payload_refusal,
    validate_published_insight,
)

#: Refus emis quand le catalogue de gabarits du projet n'a pas pu etre resolu.
#: Distinct de `template_unknown` : celui-la dit << ce projet ne publie pas
#: contre ce gabarit >>, celui-ci dit << je n'ai pas pu lire ce que ce projet
#: publie >>. Les confondre presenterait une panne comme une decision du projet.
CATALOG_UNRESOLVED = "catalog_unresolved"


def _catalog_refusal(catalog_reason: str | None) -> dict | None:
    """Refuse a publication whose template catalog is a fallback, not the project's.

    `core.main._publishable_card_templates` already hands down an EMPTY template
    set in that case, so gate 4 refuses on its own and this cannot fail open even
    if a caller forgets the argument. What this adds is the NAME of the fact: a
    `template_unknown` on an unreadable catalog reads as "this project does not
    publish that card", which is a claim about the project rather than about the
    outage.
    """

    if catalog_reason is None:
        return None
    return {
        "ok": False,
        "reasonCode": CATALOG_UNRESOLVED,
        "message": (
            "the project's card catalog could not be resolved "
            f"({catalog_reason}); nothing is published against the platform "
            "default set"
        ),
        "fieldPath": "card.template",
    }


#: Refus emis quand un `no_insight` arrive sans readiness resolue par le serveur.
#: Distinct de `data_not_ready` : celui-la dit << la journee n'etait pas prete >>,
#: celui-ci dit << je n'ai pas pu savoir si elle l'etait >>.
READINESS_UNRESOLVED = "readiness_unresolved"

#: Motif inscrit au journal quand un `no_insight` declare tombe un jour bloque.
DATA_NOT_READY = "data_not_ready"


def _resolve_no_insight_status(
    readiness_state: dict | None, coverage: dict | None
) -> tuple[str, dict | None, dict | None]:
    """Decide what a declared ``no_insight`` is actually allowed to record.

    ``no_insight`` is the only status that ASSERTS something about the day: "I looked,
    and there was nothing worth saying". A blocked day produces the same silence for an
    entirely different reason -- nobody could look. `proactive-assertions.md` forbids
    reporting the first where the honest answer is the second (`Incomplete if`, from
    decision 3: a detector must separate `no anomaly` from `insufficient observations`).
    The daily-insight run is the same shape, one surface over.

    ``readiness()`` already separates the two states, and its docstring says so in as many
    words. What was missing is that ``publish`` took the agent's word for which one it was
    -- the defect story 53.4 refused for `confidence` and `authorship`, where a claim was
    certified by its own author.

    Returns ``(status, coverage, refusal)``:

      - readiness unresolved -> refusal. A durable claim about a day is not written on an
        unverified reading, exactly as `_catalog_refusal` refuses one on an unread catalog;
      - readiness ``blocked`` -> the run records ``blocked`` with the server-measured
        reasons in its coverage manifest, where ``run_journal`` already reads
        ``{reason, reason_code}``. The declared status is kept alongside rather than
        erased: what the agent believed is itself worth reading;
      - readiness ``ready`` -> ``no_insight`` stands. Editorial silence on a day that WAS
        ready is the one judgment only the agent can make, and the server does not
        second-guess it.
    """

    if readiness_state is None:
        return (
            "no_insight",
            coverage,
            {
                "ok": False,
                "reasonCode": READINESS_UNRESOLVED,
                "message": (
                    "'no_insight' asserts that the day held nothing worth saying; "
                    "readiness could not be resolved, so the server cannot tell that "
                    "apart from a day whose data was never ready. Check readiness for "
                    "this date and record 'blocked' if it is blocked."
                ),
                "fieldPath": "status",
            },
        )

    if readiness_state.get("status") != "blocked":
        return "no_insight", coverage, None

    reasons = [str(r) for r in (readiness_state.get("reasons") or []) if r]
    return (
        "blocked",
        {
            **(coverage or {}),
            "reason": "; ".join(reasons) or "J-1 data was not ready",
            "reason_code": DATA_NOT_READY,
            "declared_status": "no_insight",
        },
        None,
    )


# ---------------------------------------------------------------------------
# 1. Readiness (J-1)
# ---------------------------------------------------------------------------


def readiness(
    *,
    insight_date: str,
    freshness_date: str | None,
    dq_blocking: list[str] | None = None,
) -> dict:
    """Decide whether J-1 data is ready for the target ``insight_date``.

    Returns a DISTINCT state: ``ready`` or ``blocked``. ``blocked`` is not "no insight" --
    it means data is not ready, so the task must stop, not publish. Reasons are actionable.
    """

    reasons: list[str] = []
    if not freshness_date:
        reasons.append("no resolved freshness; expected datastreams have not completed")
    elif freshness_date < insight_date:
        reasons.append(
            f"latest data {freshness_date} is behind target {insight_date} (J-1 not complete)"
        )
    for issue in dq_blocking or []:
        reasons.append(f"data quality: {issue}")

    status = "ready" if not reasons else "blocked"
    return {
        "status": status,
        "insight_date": insight_date,
        "freshness_date": freshness_date,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 2. Capability discovery
# ---------------------------------------------------------------------------


def capabilities(
    *,
    available_metrics: set[str],
    available_dimensions: set[str],
    catalog: list[dict] | None = None,
    catalog_reason: str | None = None,
) -> dict:
    """What can this project render right now? -- the 35.1 catalogue + recommendation.

    Zero duplicated vocabulary: the catalogue and recommendation are derived from the
    existing card registry (``cards.py``) via the 35.1 contract.

    Story 52.1: ``catalog`` is the Project's OWN resolved catalog
    (``answerable_topics.resolve_catalog_with_reason``), threaded from the caller that
    holds the connection -- this module and the 35.1 contract stay pure. Omitted, the
    platform default set applies, which is what this function did for a whole epic
    while the `catalog` parameter existed one layer down and nothing passed it.
    ``catalog_reason`` travels with it: an agent must be able to tell "this project
    answers these questions" from "I could not read them, here are the standard ones".
    """

    # Story 50.6 (AC4) -- these three lists were unbounded IN CODE. The catalog is
    # bounded in practice by the card registry, but `metrics` and `dimensions` come
    # from the project's resolved semantic surface: a wide project put an unbounded
    # list straight into the model-visible channel. Bound them and SAY what was
    # withheld -- "12 metrics" must never read as "all the metrics". The full lists
    # stay reachable through the app channel, so nothing is lost, only routed.
    from core.model_channel import bounded_head  # noqa: PLC0415

    metrics_head, metrics_withheld = bounded_head(sorted(available_metrics))
    dimensions_head, dimensions_withheld = bounded_head(sorted(available_dimensions))
    entries = agent_card_catalog(available_metrics, available_dimensions, catalog=catalog)
    return {
        "contractVersion": CARD_KEY_CONTRACT_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        # `resolved` = the catalogue below is this project's own. `defaults_only` =
        # it is the platform fallback, and `catalogReason` says why.
        "catalogStatus": "resolved" if catalog_reason is None else "defaults_only",
        "catalogReason": catalog_reason,
        "metrics": metrics_head,
        "metrics_total": len(available_metrics),
        "metrics_withheld": metrics_withheld,
        "dimensions": dimensions_head,
        "dimensions_total": len(available_dimensions),
        "dimensions_withheld": dimensions_withheld,
        # The model-visible catalog answers the question the tool asks -- WHICH
        # cards can this project render -- in four fields per entry. The twelve-field
        # registry entry (widget_uri, required/optional metrics and dimensions,
        # composition, fallback_rank) is the widget's business, so it travels in
        # `catalog_detail` and the model-channel split routes it to `_meta`.
        # Nothing is lost; the wide half simply stops being read by a model that
        # cannot act on it.
        "catalog": [
            {
                "id": entry.get("id"),
                "kind": entry.get("kind"),
                "satisfiable": entry.get("satisfiable"),
                "answers_question": entry.get("answers_question"),
            }
            for entry in entries
        ],
        "catalog_detail": entries,
        # The recommendation reads the SAME catalogue: recommending a question the
        # project retired, from the same call that omits it from the catalogue, is
        # two answers to one question.
        "recommended": recommend_card(available_metrics, available_dimensions, catalog=catalog),
    }


# ---------------------------------------------------------------------------
# 3. Preview (no persistence)
# ---------------------------------------------------------------------------


def preview(
    *,
    payload: dict,
    available_metrics: set[str],
    available_dimensions: set[str],
    available_templates: set[str],
    resolvable_evidence: set[str],
    freshness_date: str | None,
    has_project_access: bool,
    existing_slots: set[int],
    catalog_reason: str | None = None,
) -> dict:
    """Validate a candidate payload WITHOUT publishing anything.

    Runs the 35.0 fail-closed validator against the project's resolved availability,
    freshness and scope. Never persists, never renders a warehouse write. Returns the
    validation verdict so the agent can fix the spec before publishing.

    ``catalog_reason`` (story 53.4) is non-None when the template list is the
    platform fallback rather than this project's catalog; the preview then refuses
    and says so, instead of validating against questions the project may have
    retired.
    """

    refusal = _catalog_refusal(catalog_reason)
    if refusal is not None:
        return {**refusal, "slot": payload.get("slot"), "authorship": authorship_block(payload)}

    result = validate_published_insight(
        payload,
        available_metrics=available_metrics,
        available_dimensions=available_dimensions,
        available_templates=available_templates,
        resolvable_evidence=resolvable_evidence,
        freshness_date=freshness_date,
        has_project_access=has_project_access,
        existing_slots=existing_slots,
    )
    return {
        "ok": result.ok,
        "reasonCode": result.reason_code,
        "message": result.message,
        "fieldPath": result.field_path,
        "slot": payload.get("slot"),
        # STORY 53.4 -- CE QUI EST ECRIT ET CE QUI EST MESURE NE SE DEVINENT PAS.
        #
        # La provenance est DERIVEE au meme endroit pour les trois surfaces qui
        # la portent : cette preview, le payload publie (`publish` ci-dessous) et
        # la route Console qui le rend. Elle ne vivait qu'ici, c'est-a-dire au
        # seul endroit qu'aucune surface ne lit -- le champ existait et ne
        # servait a personne.
        "authorship": authorship_block(payload),
    }


# ---------------------------------------------------------------------------
# 4. Publish (atomic, all-or-nothing, idempotent)
# ---------------------------------------------------------------------------


def publish(
    *,
    project_id: str,
    insight_date: str,
    status: str,
    item_payloads: list[dict] | None = None,
    validate_ctx: dict,
    catalog_reason: str | None = None,
    conn,
    render_snapshot_ids: dict[int, str] | None = None,
    render_fn=None,
    result_fn=None,
    confidence_fn=None,
    identity: str | None = None,
    trace_id: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    coverage: dict | None = None,
    host: str | None = None,
    prompt_version: str | None = None,
    readiness_state: dict | None = None,
    record_run_fn=record_run,
) -> dict:
    """Validate every item fail-closed, then persist the run + items atomically.

    All-or-nothing: if ANY item fails validation, NOTHING is persisted and the first
    failing ``reason_code`` is returned (``ok=False``). On success, delegates to the
    transactional 35.3 ``record_run`` (idempotent per date/slot) and returns a short
    write-ack.

    ``status`` other than ``published`` (``no_insight`` / ``blocked`` / ``failed``) records
    a run with zero items -- a run that did not run is simply an ABSENT row, kept distinct.

    ``catalog_reason`` (story 53.4) non-None means the template list is the platform
    fallback, not this project's: nothing is written. A durable, shareable artifact is
    not published on the strength of a catalog read that failed.

    ``readiness_state`` is the server's own ``readiness()`` reading for this date. It is
    required to record ``no_insight`` and ignored for every other status -- see
    ``_resolve_no_insight_status`` for why that one status cannot be taken on faith.

    ``result_fn`` is AI-294's governed evidence path (design 2026-08-17): called per
    VALIDATED item with the enriched payload, it derives a Query Spec behind the card
    and executes it, returning either the lineage ``{query_spec_version_id, result_id,
    outcome}`` or the named refusal ``{result_unavailable_reason, missing_link}``.
    Both land on the item (migration 281) and in the ack's ``results`` -- a refusal
    NEVER blocks publication, it makes the insight honestly not shareable, with the
    missing link named.

    ``confidence_fn`` is the DERIVATION of the confidence a surface prints, called
    per validated item with the enriched payload and returning the block
    ``core.insight_confidence.derive_insight_confidence`` produces. It replaces the
    level the model declared about its own claim, which
    ``proactive-assertions.md`` refuses; the declared word survives beside it as
    ``authorship.declaredConfidence`` and drives nothing. Absent ``confidence_fn``,
    the block is ``None`` and every surface reads ``unmeasurable`` -- never a
    default level.
    """

    item_payloads = item_payloads or []
    snapshot_ids = render_snapshot_ids or {}

    # A declared `no_insight` is checked against what the server measured BEFORE anything
    # is written: on a blocked day it is recorded as `blocked`, with its reasons.
    corrected_from: str | None = None
    if status == "no_insight":
        status, coverage, readiness_refusal = _resolve_no_insight_status(
            readiness_state, coverage
        )
        if readiness_refusal is not None:
            return {**readiness_refusal, "slot": None, "published": False}
        if status != "no_insight":
            corrected_from = "no_insight"

    if status == "published":
        refusal = _catalog_refusal(catalog_reason)
        if refusal is not None:
            return {
                **refusal,
                "slot": item_payloads[0].get("slot") if item_payloads else None,
                "published": False,
            }
        # Validate each payload against the project's resolved facts (fail-closed).
        for payload in item_payloads:
            slot = payload.get("slot")
            verdict = validate_published_insight(payload, **validate_ctx)
            if not verdict.ok:
                return {
                    "ok": False,
                    "reasonCode": verdict.reason_code,
                    "message": verdict.message,
                    "slot": slot,
                    "published": False,
                }
        # Render happens only AFTER every item validated (no orphan snapshots on rejection).
        items = []
        results_ack: list[dict] = []
        for p in item_payloads:
            slot = int(p["slot"])
            # STORY 53.4 (AC5/AC6) -- the PERSISTED artifact carries its own
            # provenance. The payload is self-contained by design (Epic 35 §7) and
            # is what every reader gets; a provenance block that lived only in the
            # preview response left the surface that draws `confidence` guessing,
            # which is the clause `proactive-assertions.md:162` forbids. Derived
            # AFTER validation, so `authorship_declared` still refuses an agent
            # that writes its own. NOT idempotent once `derivedConfidence` is
            # attached: `authorship_block(enriched)` would differ, so a future
            # re-validation of a PERSISTED payload would be refused as
            # `authorship_declared`. Nothing re-validates today; whoever adds a
            # re-validation must strip the server-written block first.
            enriched = {**p, "authorship": authorship_block(p)}
            # THE CONFIDENCE IS MEASURED HERE OR IT IS NOT MEASURED AT ALL.
            # `proactive-assertions.md` refuses a level declared by the author of
            # the claim; `core.insight_confidence` derives one from the rows that
            # carry the members this insight CITED, over the insight's own period.
            # It has to happen at publication because the rows exist here and are
            # gone by read time, and it happens AFTER validation for the same
            # reason `frozenCard` does: a server-written key must not be
            # something the agent can supply. No `confidence_fn` -> the block
            # stays `None`, which every surface draws as `unmeasurable`. A caller
            # that cannot measure never yields a level nobody stands behind.
            if confidence_fn is not None:
                enriched["authorship"] = {
                    **enriched["authorship"],
                    "derivedConfidence": confidence_fn(enriched),
                }
            snap_id = snapshot_ids.get(slot)
            if snap_id is None and render_fn is not None:
                # STORY 35 L3 -- the render hands back its ENVELOPE as well as its id.
                # The snapshot table is purged at 30 days and the lineage column is
                # `ON DELETE SET NULL`, so an id alone gives the insight a month of
                # evidence and then leaves the model-authored prose standing alone.
                # Epic 35 §7 asked for the envelope in the payload for exactly that
                # reason: the two objects do not have the same lifetime.
                snap_id, envelope = render_fn(enriched)
                if envelope:
                    enriched["frozenCard"] = envelope
            oversize = enriched_payload_refusal(enriched)
            if oversize is not None:
                # Refused whole, never truncated (§8). The budget parameter existed
                # since 35.0 and nothing ever passed it; the embedded envelope is the
                # one part of the payload the agent does not size, so it fires here.
                return {
                    "ok": False,
                    "reasonCode": oversize.reason_code,
                    "message": oversize.message,
                    "slot": slot,
                    "published": False,
                }
            # AI-294 -- the publication produces a Result, or names why it could
            # not. Runs on the same connection as `record_run`, so lineage and
            # item commit together and a rejected run discards both.
            lineage: dict | None = None
            if result_fn is not None:
                lineage = result_fn(enriched)
                from core.daily_insight_result import lineage_ack  # noqa: PLC0415

                results_ack.append(lineage_ack(lineage, slot))
            lineage = lineage or {}
            items.append(
                InsightItem(
                    slot=slot,
                    payload=enriched,
                    render_snapshot_id=snap_id,
                    query_spec_version_id=lineage.get("query_spec_version_id"),
                    result_id=lineage.get("result_id"),
                    result_unavailable_reason=lineage.get("result_unavailable_reason"),
                )
            )
    else:
        items = []
        results_ack = []

    run_id = record_run_fn(
        project_id=project_id,
        insight_date=insight_date,
        status=status,
        items=items,
        period_from=period_from,
        period_to=period_to,
        coverage=coverage,
        host=host,
        prompt_version=prompt_version,
        contract_version=SCHEMA_VERSION,
        identity=identity,
        trace_id=trace_id,
        conn=conn,
    )
    ack = {
        "ok": True,
        "runId": run_id,
        "status": status,
        "publishedSlots": sorted(int(p["slot"]) for p in item_payloads)
        if status == "published"
        else [],
    }
    if results_ack:
        # Per-slot Result lineage (AI-294): the agent learns which insight is
        # shareable through the one Share mechanism and which is not, by name.
        ack["results"] = sorted(results_ack, key=lambda r: r["slot"])
    if corrected_from is not None:
        # Corrected, never silently: the agent is told what the server recorded instead
        # of what it declared, so a task can log it and stop calling the day empty.
        ack["statusCorrectedFrom"] = corrected_from
    return ack
