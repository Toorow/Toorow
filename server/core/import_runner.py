"""toorow -- ``run_import``: the orchestration step of the import chain.

Drives parse -> open_import -> land -> rejection gate -> DQ gates -> publication,
against the 12.8 ledger contract and the 12.5 publication gates. It re-implements
neither: it consumes ``core.managed_feed_ledger`` and
``core.datastream_publication`` through their public seams.

The parse itself is NOT here -- ``import_contract.parse_for_contract`` owns it,
so this module is the sequence of governed steps and nothing else.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from core.context_event_import import (
    ROUTE_CONTEXT_EVENTS,
    event_row_validator,
    resolve_events_route,
)
from core.entity_reference_import import (
    ROUTE_ENTITY_REFERENCE,
    reference_row_validator,
    resolve_reference_route,
)
from core.file_source_resolution import (
    FileSourceProducer,
)
from core.file_source_template import (
    LANDING_PLAN_STORE,
    LANDING_WAREHOUSE_RELATION,
)
from core.import_contract import (
    parse_for_contract,
    version_contract,
)
from core.import_landing import (
    _WAREHOUSE_TYPES,
    _apply_governed_mapping,
    _canonical_fingerprint,
    _land_managed_rows,
    _parse_evidence,
    land_plan_store_rows,
)
from core.tabular_parsing import (
    _truncate_value,
    validate_upload_meta,
)
from core.tabular_types import (
    WRITE_MODE_APPEND,
    WRITE_MODE_REPLACE,
    AppendUnavailable,
    ColumnSpec,
    CsvExcelImportError,
    ParseResult,
    ParserReviewRequired,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orchestration layer (consumes 12.8 public contract; pg-gated in production).
# ---------------------------------------------------------------------------

# Success signal on a passing gate: the ledger is 'written' but NOT published. The
# physical row write + 12.5 DQ gates + pointer-swap commit_publication are Phase B.
OUTCOME_WRITTEN_PENDING_PUBLICATION = "written_pending_publication"


def _resolve_landing_target(
    producer: Any,
    *,
    source_metadata: dict[str, Any],
    publish_candidate: bool,
) -> tuple[str, str | None]:
    """Which target this import lands in, and the plan it lands in when it is one.

    The target is DECLARED by the Template (`landing_target`), never inferred from
    the rows: that is the capability `file-source-ingestion.md` names, and it is
    what lets the mediaplan profile run on this one engine while the plan's data
    model -- the versioned Postgres object every plan reader reads -- stays the
    plan store's.

    Anything that is not a file-source producer lands where it always landed.
    """
    if not isinstance(producer, FileSourceProducer):
        return LANDING_WAREHOUSE_RELATION, None

    contract = (producer.template or {}).get("contract") or {}
    target = contract.get("landing_target", LANDING_WAREHOUSE_RELATION)
    if target != LANDING_PLAN_STORE:
        return LANDING_WAREHOUSE_RELATION, None

    plan_id = (source_metadata or {}).get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise CsvExcelImportError(
            "plan_target_missing",
            "a template landing in the plan store must be run against a plan; "
            "no 'plan_id' was supplied with the import",
            repair={"supply_plan_id": True},
        )

    # ONE publication lifecycle per act. A plan version is published by the plan
    # store's own explicit gesture; driving the warehouse pointer-swap state
    # machine over it would be a second cycle for the same publication, and the
    # pointer it swaps does not exist for a plan.
    if publish_candidate:
        raise CsvExcelImportError(
            "plan_store_publication_is_governed_by_the_plan",
            "a plan-store import creates a CANDIDATE version and never publishes; "
            "publication stays the plan's own explicit act",
            repair={"publish_via_plan_version": True},
        )

    return LANDING_PLAN_STORE, plan_id.strip()


def _resolve_import_route(
    producer: Any,
    *,
    mapping_payload: dict[str, Any] | None,
    projection_plan: dict[str, Any],
    source_metadata: dict[str, Any],
    publish_candidate: bool,
    conn,
    project_id: str,
) -> dict[str, Any]:
    """ONE router for every import (epic 68): three declared destinations.

    facts     -- the warehouse raw landing (the default);
    events    -- datastream-owned context events (Story 68.4), declared by the
                 pinned MAPPING's event roles;
    reference -- versioned MDM attributes (Story 68.5), declared by the pinned
                 MAPPING rather than by a template.

    The two mapping-declared routes are mutually exclusive by construction: a
    reference mapping has exactly one designation IN its grain and no measures;
    an events mapping declares date/type/label roles. A payload that somehow
    satisfied both would be two answers to "what is this file", so the events
    route is resolved FIRST and the reference route is not even asked -- an
    explicit precedence beats a silent one, the same call the template
    declaration already gets.

    The route is DECLARED, never inferred from the rows, and it is resolved in
    ONE place so the destinations can never drift apart: the Template declares
    its landing target (``_resolve_landing_target``), the pinned mapping
    declares reference columns (68.2's ``designates_object_kind``, re-verified
    against the live registry by ``resolve_reference_route``). A template
    declaration wins over the mapping's: the two never co-declare today, and
    an explicit precedence beats a silent one.

    Called BEFORE the pinned mapping is applied and BEFORE the ledger opens:
    the reference route supplies its per-row key validator to the mapping
    pass, and a misdeclared route refuses without minting a ledger row and a
    candidate.
    """
    landing_target, plan_id = _resolve_landing_target(
        producer,
        source_metadata=source_metadata,
        publish_candidate=publish_candidate,
    )
    events = None
    reference = None
    mapping_declared = (
        landing_target == LANDING_WAREHOUSE_RELATION
        and mapping_payload is not None
        and not isinstance(producer, FileSourceProducer)
    )
    if mapping_declared:
        events = resolve_events_route(
            mapping_payload=mapping_payload,
            projection_plan=projection_plan,
            publish_candidate=publish_candidate,
        )
    if mapping_declared and events is None:
        reference = resolve_reference_route(
            conn,
            project_id=project_id,
            mapping_payload=mapping_payload,
            projection_plan=projection_plan,
            publish_candidate=publish_candidate,
        )
    route = landing_target
    if events is not None:
        route = ROUTE_CONTEXT_EVENTS
    elif reference is not None:
        route = ROUTE_ENTITY_REFERENCE
    return {
        "route": route,
        "events": events,
        # The TEMPLATE's target, kept beside the route: the reference route
        # overrides where rows go without rewriting what the template declared.
        "landing_target": landing_target,
        "plan_id": plan_id,
        "reference": reference,
    }


def _route_row_validator(events_route, reference_route):
    """The per-row veto of whichever mapping-declared route this import took.

    One function so `_apply_governed_mapping` stays route-agnostic: it asks
    "may this row land?" and records the answer, without knowing what an events
    file or a reference file is. The two routes are mutually exclusive (the
    router picks one), so this never composes two vetoes.
    """
    if events_route is not None:
        return event_row_validator(events_route)
    if reference_route is not None:
        return reference_row_validator(reference_route)
    return None


class _GovernedDispatchWalk:
    """The governed dispatch state of ONE import, advanced monotonically (AI-287).

    Lifted out of `run_import` as the first named step of a 869-line function.
    It was a closure over `nonlocal dispatch` with eight call sites, which is the
    shape that makes a long function unreadable: the state it advances is
    invisible from any one of them, and nothing could exercise the ordering rule
    without driving a whole import.

    WHAT IT GUARANTEES, and it is a real rule rather than plumbing: a dispatch
    NEVER goes backwards. A replay, a retry or a late callback can ask for
    `landing` when the row already reached `ready`, and honouring that would
    rewind the durable coordinator of a cross-store write -- the warehouse
    candidate would then be described by a state that precedes its own
    existence. Out-of-order requests are DROPPED, not raised: the caller asked
    for a state the walk has already passed, which is a no-op, not an error.

    An unknown state name is refused for the opposite reason. It cannot be
    compared to the order, so it can be neither honoured nor safely ignored --
    silently dropping it would stall a dispatch with nothing said.

    FOUR STATES ARE OFF THE GRAPH ON PURPOSE: `promoting`, `reconcile_required`,
    `rejected` and `failed`. They are not steps forward, they are EXITS -- an
    import that stops here, or a cross-store write whose outcome is uncertain and
    which a reconciliation must settle. `leave` writes them unconditionally, and
    that is why `advance` may refuse an unknown name without refusing them.

    THE FOUR WERE FOUND BY THE EXTRACTION, and they are the reason the strictness
    is worth having. The closure this replaces tested
    `if state in order and current in order and ...`, so a state ABSENT from the
    order matched no clause and was written with no check at all. Four of the
    eight call sites were passing through a rule that never looked at them, and
    nothing said so -- the ordering guard read as if it covered every state.

    `current` is None until an intent is recorded, and that is the non-publish
    path: an import that publishes no candidate has no governed dispatch at all,
    and the returned payload says `dispatch: None` rather than inventing one.
    """

    #: The governed order. Every state a dispatch can occupy, and the only
    #: sequence in which it may occupy them.
    _ORDER = {
        "pending": 0,
        "landing": 1,
        "landed": 2,
        "validating": 3,
        "ready": 4,
        "published": 5,
    }

    def __init__(
        self,
        conn=None,
        *,
        dispatch: dict[str, Any] | None = None,
        dispatch_id: str | None = None,
        datastream_id: str | None = None,
        project_id: str | None = None,
    ):
        self._conn = conn
        self._dispatch = dispatch
        self._dispatch_id = dispatch_id
        self._datastream_id = datastream_id
        self._project_id = project_id

    @property
    def current(self) -> dict[str, Any] | None:
        """The dispatch row as last written, or None when none was recorded."""
        return self._dispatch

    @property
    def state(self) -> str:
        return str((self._dispatch or {}).get("state") or "pending")

    def adopt(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        """Take *dispatch* as the truth, whatever the walk believed.

        NOT an advance, and named apart from one on purpose. `advance` refuses to
        go backwards because a request to rewind is a bug; this says the durable
        coordinator was RE-READ -- reconciliation, or a fresh intent after a
        failed one -- and Postgres is the authority on what the state is. A walk
        that refused an adopted row would keep believing a state the database has
        already contradicted.
        """
        self._dispatch = dispatch
        return dispatch

    #: The exits. Not steps of the walk, so no ordering applies to them.
    _EXITS = frozenset({"promoting", "reconcile_required", "rejected", "failed"})

    def leave(self, state: str, **evidence: Any) -> dict[str, Any]:
        """Write an off-graph EXIT state -- unconditionally, and say so.

        An exit is not a step, so no ordering question arises: an import that is
        rejected is rejected whatever it had reached. Separate from `advance`
        because a reader has to be able to see, at the call site, that this one
        leaves the governed sequence rather than moving along it.
        """
        if state not in self._EXITS:
            raise CsvExcelImportError(
                "dispatch_state_unknown",
                f"{state!r} is not a governed dispatch exit",
            )
        return self._write(state, **evidence)

    def advance(self, state: str, **evidence: Any) -> dict[str, Any]:
        """Move to *state* when that is forward, and return the dispatch row."""
        if state not in self._ORDER:
            raise CsvExcelImportError(
                "dispatch_state_unknown",
                f"{state!r} is not a governed dispatch state",
            )
        current = self.state
        if current == state:
            return self._dispatch or {}
        if current in self._ORDER and self._ORDER[current] > self._ORDER[state]:
            return self._dispatch or {}
        return self._write(state, **evidence)

    def _write(self, state: str, **evidence: Any) -> dict[str, Any]:
        from core.managed_file_dispatch import advance_dispatch  # noqa: PLC0415

        self._dispatch = advance_dispatch(
            self._conn,
            dispatch_id=self._dispatch_id,
            datastream_id=self._datastream_id,
            project_id=self._project_id,
            state=state,
            **evidence,
        )
        # The commit belongs to the write: Postgres is the durable coordinator of
        # the cross-store write, so each governed step must be visible to a
        # concurrent reconciliation before the warehouse side of it begins.
        self._conn.commit()
        return self._dispatch



def _open_governed_dispatch(
    conn,
    *,
    publish_candidate: bool,
    source_metadata: dict[str, Any],
    bundle: dict[str, Any],
    ledger: dict[str, Any],
    ledger_id: str,
    execution: dict[str, Any],
    execution_id: str,
    datastream_id: str,
    project_id: str,
    import_contract_id,
) -> tuple["_GovernedDispatchWalk", str | None, dict[str, Any] | None]:
    """Open the durable coordinator, and say whether this import is ALREADY settled.

    Third step lifted out of `run_import` (AI-287), and the one that made the
    function unreadable from its own top: it both STARTS the cross-store write
    and can END the import, and those two outcomes were interleaved with the rest
    of the sequence over ninety lines.

    Returns ``(walk, dispatch_id, settled)``. ``settled`` is None when the caller
    must go on; otherwise it IS the payload to return, because the durable
    coordinator says this import already reached a terminal state -- published,
    or awaiting a reconciliation nobody may bypass.

    PostgreSQL is that coordinator, which is why the commit is here rather than
    at the end: the immutable intent must be durable before a warehouse candidate
    can exist at all.

    An import that publishes no candidate records no intent: the walk comes back
    empty, `walk.current` stays None, and the payload says ``dispatch: None``
    rather than inventing one. That is why the walk exists on BOTH paths -- a
    second variable kept in step with it is the shape that rots.

    THE ONE EXCEPTION FLOW HERE RECOMPOSES STATE, and it is the reason this is a
    step rather than a fragment: a reconciliation that itself fails leaves the
    connection unusable, so the rollback and the FRESH intent that follows are
    not error handling, they are the second half of the same decision. Splitting
    them across a boundary would let a caller observe a dispatch that no longer
    exists.

    `_mfl` is re-imported rather than received: `run_import` imports the MODULE so
    that `core.managed_feed_ledger.*` stays patchable, and a helper handed the
    outcome constant as an argument would read it before a test replaced it.
    """
    import core.managed_feed_ledger as _mfl  # noqa: PLC0415

    walk = _GovernedDispatchWalk()
    if not publish_candidate:
        return walk, None, None

    from core.managed_file_dispatch import record_intent  # noqa: PLC0415

    raw_import_id = str(source_metadata.get("raw_import_id") or "").strip()
    if not raw_import_id:
        raise CsvExcelImportError(
            "dispatch_raw_import_missing",
            "a governed publication requires immutable raw-import evidence",
        )
    dispatch = record_intent(
        conn,
        raw_import_id=raw_import_id,
        ledger_id=ledger_id,
        execution_id=execution_id,
        datastream_id=datastream_id,
        project_id=project_id,
        bundle=bundle,
    )
    dispatch_id = dispatch["id"]
    conn.commit()

    walk = _GovernedDispatchWalk(
        conn,
        dispatch=dispatch,
        dispatch_id=dispatch_id,
        datastream_id=datastream_id,
        project_id=project_id,
    )

    if dispatch.get("state") in {"promoting", "reconcile_required"}:
        from core.managed_file_dispatch import reconcile_dispatch  # noqa: PLC0415

        try:
            dispatch = walk.adopt(reconcile_dispatch(
                conn,
                dispatch_id=dispatch_id,
                datastream_id=datastream_id,
                project_id=project_id,
            ))
            conn.commit()
        except Exception:
            conn.rollback()
            dispatch = walk.adopt(record_intent(
                conn,
                raw_import_id=raw_import_id,
                ledger_id=ledger_id,
                execution_id=execution_id,
                datastream_id=datastream_id,
                project_id=project_id,
                bundle=bundle,
            ))
    if dispatch.get("state") == "published":
        return walk, dispatch_id, {
            "ledger": ledger,
            "execution": execution,
            "import_contract_id": import_contract_id,
            "no_op": False,
            "replay": True,
            "row_count": ledger.get("row_count") or 0,
            "rejected_count": ledger.get("rejected_row_count") or 0,
            "blocked": False,
            "reason": None,
            "outcome": _mfl.OUTCOME_PUBLISHED,
            "published": True,
            "dispatch": walk.current,
        }
    if dispatch.get("state") == "reconcile_required":
        return walk, dispatch_id, {
            "ledger": ledger,
            "execution": execution,
            "import_contract_id": import_contract_id,
            "no_op": False,
            "replay": True,
            "row_count": ledger.get("row_count") or 0,
            "rejected_count": ledger.get("rejected_row_count") or 0,
            "blocked": True,
            "reason": "dispatch_reconciliation_required",
            "outcome": "dispatch_reconciliation_required",
            "published": False,
            "dispatch": walk.current,
        }
    return walk, dispatch_id, None


def _observe_landed_candidate(
    *,
    walk: "_GovernedDispatchWalk",
    execution_id: str,
    project_id: str,
    landing_relation: str,
    landed: dict[str, Any],
    warehouse_columns,
    accepted_row_count: int,
    expected_content_fingerprint: str,
    expected_schema_fingerprint: str,
) -> tuple[str, str]:
    """OBSERVE what reached the warehouse, and refuse it if it is not the projection.

    Fourth step lifted out of `run_import` (AI-287). Its whole responsibility is
    one rule: the candidate a publication is about to promote must be, row for
    row and column for column, the governed typed projection the mapping
    produced -- not merely a table that exists under the right execution id.

    A divergence is NOT a failure of this import, which is why it leaves the walk
    for `reconcile_required` and not `failed`: the two stores disagree, so no
    automatic answer is safe and a reconciliation has to settle it. The state is
    written BEFORE the raise, never after -- an exception that escaped first
    would leave a candidate no reader could account for.

    Returns the OBSERVED fingerprints, and they are what the rest of the sequence
    carries: the expected pair is a claim, and only these two are evidence.
    """
    from core.raw_landing import inspect_candidate  # noqa: PLC0415

    actual_landing_relation = str(landed.get("table") or landing_relation)
    observed_candidate = inspect_candidate(
        landing_relation.rsplit(".", 1)[-1],
        execution_id,
        columns=warehouse_columns,
        project_id=project_id,
        backend=landed.get("backend"),
    )
    candidate_fingerprint = observed_candidate["content_fingerprint"]
    candidate_schema_fingerprint = observed_candidate["schema_fingerprint"]
    if (
        observed_candidate["row_count"] != accepted_row_count
        or candidate_fingerprint != expected_content_fingerprint
        or candidate_schema_fingerprint != expected_schema_fingerprint
    ):
        walk.leave(
            "reconcile_required",
            error_code="candidate_evidence_diverged",
            reconciliation_evidence={
                "expected_rows": accepted_row_count,
                "observed_rows": observed_candidate["row_count"],
                "expected_content_fingerprint": expected_content_fingerprint,
                "observed_content_fingerprint": candidate_fingerprint,
                "expected_schema_fingerprint": expected_schema_fingerprint,
                "observed_schema_fingerprint": candidate_schema_fingerprint,
            },
        )
        raise CsvExcelImportError(
            "candidate_evidence_diverged",
            "warehouse candidate differs from the governed typed projection",
            repair={"reconcile_dispatch": True},
        )
    walk.advance(
        "landed",
        candidate_content_fingerprint=candidate_fingerprint,
        candidate_schema_fingerprint=candidate_schema_fingerprint,
        landing_relation=actual_landing_relation,
        row_count=observed_candidate["row_count"],
    )
    return candidate_fingerprint, candidate_schema_fingerprint


def _fail_execution_and_discard_candidate(
    conn,
    *,
    execution,
    current_state,
    execution_id: str,
    project_id: str,
    actor: str,
    landing_relation: str,
    error_code: str,
    error_detail: str | None,
):
    """The candidate side of a refusal, in ONE order, for BOTH gates -- AI-287.

    The rejection gate and the DQ gates each end an import by doing the same two
    things to the candidate -- move the execution to FAILED, then take its rows
    out of the warehouse -- and they disagreed on where the ledger write sat
    between them and on what to do with an execution whose state was unreadable.
    Two answers to one question is a defect whichever answer is right, so both
    live here now and neither gate decides for itself.

    THE ORDER: the candidate is DISCARDED before the ledger records the outcome.

    Only one boundary in these gates can be crossed by a crash. `advance_state`
    and `mark_outcome` are Postgres writes on the caller's connection and NEITHER
    commits -- the transaction becomes durable at `walk.leave`, which does.
    `discard_candidate` is a write to the OTHER store, effective the moment it
    returns and rolled back by nothing. So the only question the order answers is
    which of the two stores may be ahead of the other when the process dies, and
    the warehouse must never be the one behind: a ledger row marked `rejected` or
    `failed` while unconfirmed rows still sit under that execution id is the
    residue `_refuse_on_dq_gates` calls its load-bearing effect, and nothing ever
    revisits a terminal ledger row to clean it. The reverse leftover is benign --
    an import whose warehouse side is empty and whose ledger never closed is
    re-runnable, and no reader can mistake it for confirmed data.

    THE FAILED GUARD, and why an unreadable state is not a licence to skip.
    `advance_state` is the single writer of `app.datastream_executions.state`
    (AI-223) and it re-reads the live row: given no expectation it still refuses
    an invalid transition itself. A caller that skipped the call because it could
    not read a state locally would be deciding, on no evidence, that the
    execution stays where it is -- and the payload would then carry a refusal the
    execution row does not corroborate, which is exactly the disagreement between
    the two stores these gates exist to prevent. So the ONLY state that skips is
    the one that is already the destination; anything else, including None, is
    handed to the machine that owns the answer.

    Returns the execution: the one advanced, or the observed one untouched. Never
    None -- the payload carries it and a caller reads it.
    """
    from core.datastream_publication import STATE_FAILED, advance_state  # noqa: PLC0415
    from core.raw_landing import discard_candidate  # noqa: PLC0415

    if current_state != STATE_FAILED:
        execution = advance_state(
            execution_id,
            current_state,
            STATE_FAILED,
            actor,
            conn,
            project_id=project_id,
            error_code=error_code,
            error_detail=error_detail,
        )
    discard_candidate(
        landing_relation.rsplit(".", 1)[-1],
        execution_id,
        project_id=project_id,
    )
    return execution


def _refuse_on_rejection_gate(
    conn,
    *,
    gate_issue: dict[str, Any],
    walk: "_GovernedDispatchWalk",
    publish_candidate: bool,
    execution,
    execution_id: str,
    ledger_id: str,
    ledger_after,
    project_id: str,
    actor: str,
    landing_relation: str,
    import_contract_id,
    replay: bool,
    accepted: int,
    rejected: int,
) -> dict[str, Any]:
    """The rejection gate refused: fail the execution, DISCARD the candidate, say why.

    Fifth step lifted out of `run_import` (AI-287), and it is the SIBLING of
    `_refuse_on_dq_gates` -- the same four things that must happen together, on
    the other of the two gates. Leaving one of two identical refusals inline is
    the defect this repository calls treating the instance instead of the class:
    the next reader of either one would have had to discover the pairing again.

    The two are NOT merged, and the difference is why: this one is conditional on
    `publish_candidate` throughout, because a rejection can refuse an import that
    never opened a governed dispatch at all, while the DQ gates only ever run on
    a publication. Two callers, two shapes; one function pretending otherwise
    would carry a flag through every line.

    WHAT THE TWO NO LONGER DECIDE SEPARATELY: the candidate side of the refusal
    -- the FAILED transition and the discard, their order, and what an unreadable
    execution state means -- is `_fail_execution_and_discard_candidate`, once, for
    both gates. This gate used to skip the transition when it could not read a
    state and to discard before marking the ledger; its sibling did neither. The
    reasoning for the single answer is written there and not repeated here.
    """
    import core.managed_feed_ledger as _mfl  # noqa: PLC0415

    if publish_candidate:
        execution = _fail_execution_and_discard_candidate(
            conn,
            execution=execution,
            current_state=(execution or {}).get("state"),
            execution_id=execution_id,
            project_id=project_id,
            actor=actor,
            landing_relation=landing_relation,
            error_code=gate_issue.get("code"),
            error_detail=gate_issue.get("detail"),
        )
    _mfl.mark_outcome(
        ledger_id=ledger_id,
        project_id=project_id,
        outcome=_mfl.OUTCOME_REJECTED,
        actor=actor,
        error_code=gate_issue.get("code"),
        error_detail=gate_issue.get("detail"),
        conn=conn,
    )
    if publish_candidate:
        # `leave`, not `advance`: a rejected import stops here whatever step
        # it had reached, so no ordering question arises. The old closure
        # wrote it through the ordered path, where it matched no rule at all.
        walk.leave("rejected", error_code=gate_issue.get("code"))
    return {
        "ledger": ledger_after,
        "execution": execution,
        "import_contract_id": import_contract_id,
        "no_op": False,
        "replay": replay,
        "row_count": accepted,
        "rejected_count": rejected,
        "blocked": True,
        "reason": "rejection_threshold_exceeded",
        "gate_issue": gate_issue,
        "outcome": _mfl.OUTCOME_REJECTED,
        "published": False,
        "dispatch": walk.current,
    }


def _refuse_on_dq_gates(
    conn,
    *,
    dq_issues,
    walk: "_GovernedDispatchWalk",
    current_state,
    execution_id: str,
    ledger_id: str,
    ledger_after,
    observed_execution,
    project_id: str,
    actor: str,
    landing_relation: str,
    import_contract_id,
    replay: bool,
    accepted: int,
    rejected: int,
) -> dict:
    """The DQ gates refused: fail the execution, DISCARD the candidate, say why.

    Second step lifted out of `run_import` (AI-287). It is a whole step rather
    than a fragment -- it ends the import -- and the four things it does have to
    happen together, which is why they were tangled in the first place:

      1. the execution moves to FAILED, carrying the issues as its error detail;
      2. the warehouse CANDIDATE is discarded -- this is the load-bearing one. A
         candidate left behind is unconfirmed data sitting in the warehouse under
         an execution id, and the next publication would find it;
      3. the LEDGER records the same outcome, so the two stores agree;
      4. the dispatch LEAVES the governed walk (`failed` is an exit, not a step).

    Steps 1 and 2 are `_fail_execution_and_discard_candidate`, shared with the
    rejection gate: their order and the FAILED guard are ONE answer for both
    gates, and the reasoning lives there. Step 2 preceding step 3 is that answer
    -- it used to follow it here -- and it is why the numbering above changed.

    Nothing here is conditional on anything the caller still has to decide, which
    is what makes the extraction safe: the caller's only remaining job is to
    return what this returns.

    `current_state` is passed rather than re-read: `advance_state` refuses a
    transition from a state the row is no longer in, so the value must be the one
    the caller observed, not one this function fetches a moment later.
    """
    import core.managed_feed_ledger as _mfl  # noqa: PLC0415

    detail = json.dumps(dq_issues, sort_keys=True)
    # The ALREADY OBSERVED execution is kept when no transition takes place.
    # Setting it to None would be a silent behaviour change: the returned payload
    # carries the execution, and a caller reads it.
    execution = _fail_execution_and_discard_candidate(
        conn,
        execution=observed_execution,
        current_state=current_state,
        execution_id=execution_id,
        project_id=project_id,
        actor=actor,
        landing_relation=landing_relation,
        error_code="dq_gate_failed",
        error_detail=detail,
    )
    _mfl.mark_outcome(
        ledger_id=ledger_id,
        project_id=project_id,
        outcome=_mfl.OUTCOME_FAILED,
        actor=actor,
        error_code="dq_gate_failed",
        error_detail=detail,
        conn=conn,
    )
    walk.leave("failed", error_code="dq_gate_failed")
    return {
        "ledger": ledger_after,
        "execution": execution,
        "import_contract_id": import_contract_id,
        "no_op": False,
        "replay": replay,
        "row_count": accepted,
        "rejected_count": rejected,
        "blocked": True,
        "reason": "dq_gate_failed",
        "gate_issues": dq_issues,
        "outcome": _mfl.OUTCOME_FAILED,
        "published": False,
        "dispatch": walk.current,
    }


def _promote_candidate_or_reconcile(
    conn,
    *,
    walk: "_GovernedDispatchWalk",
    dispatch_id: str | None,
    project_id: str,
    execution_id: str,
    actor: str,
    landing_relation: str,
    landed: dict[str, Any],
    warehouse_columns,
    accepted_row_count: int,
    candidate_evidence: dict[str, Any],
    candidate_content_fingerprint: str,
    candidate_schema_fingerprint: str,
) -> dict[str, Any]:
    """Promote the candidate into the durable relation, or hand it to reconciliation.

    Sixth step lifted out of `run_import` (AI-287), and the FIRST half of the
    cross-store write. `begin_managed_file_promotion` announces the intent in
    Postgres and `promote_candidate` performs the warehouse merge, in that order
    and never the other: an announced promotion whose merge fails is recoverable,
    while a merge nobody announced is data that arrived under no record.

    Every failure in this half means the same thing -- the warehouse outcome is
    UNCERTAIN -- so it raises one type, `ManagedFileDispatchError`, rather than
    letting the provider's exception escape. That translation is deliberate: the
    caller must not be able to distinguish a timeout from a schema refusal here,
    because both leave exactly the same question for a reconciliation to answer.

    Returns the promotion evidence, which the pointer swap then has to carry.

    THE EXIT IS WRITTEN THROUGH THE WALK, and that is the whole of AI-287's first
    contradiction: this seam used to call `advance_dispatch` directly, so `_EXITS`
    never saw `reconcile_required` here and the ordering door it guards was open
    on one side. One machine, one door -- the same principle AI-223 applied to
    `advance_state`. `leave` also owns the commit, which is why none follows.
    """
    from core.datastream_publication import (  # noqa: PLC0415
        begin_managed_file_promotion,
    )
    from core.managed_file_dispatch import ManagedFileDispatchError  # noqa: PLC0415
    from core.raw_landing import promote_candidate  # noqa: PLC0415

    try:
        begin_managed_file_promotion(
            execution_id,
            project_id,
            actor,
            conn,
            dispatch_id=dispatch_id,
            candidate_evidence=candidate_evidence,
        )
        promotion = promote_candidate(
            landing_relation.rsplit(".", 1)[-1],
            execution_id,
            columns=warehouse_columns,
            project_id=project_id,
            backend=landed.get("backend"),
            idempotency_column="execution_id",
            idempotency_value=execution_id,
            expected_rows=accepted_row_count,
            expected_content_fingerprint=candidate_content_fingerprint,
            expected_schema_fingerprint=candidate_schema_fingerprint,
        )
    except Exception as exc:
        walk.leave(
            "reconcile_required",
            error_code="dispatch_promotion_reconciliation_required",
            reconciliation_evidence={
                "phase": "warehouse_promotion",
                "promotion_error": type(exc).__name__,
                "execution_id": execution_id,
            },
        )
        raise ManagedFileDispatchError(
            "dispatch_promotion_reconciliation_required"
        ) from exc
    return promotion


def _commit_publication_or_reconcile(
    conn,
    *,
    walk: "_GovernedDispatchWalk",
    dispatch_id: str | None,
    project_id: str,
    execution_id: str,
    ledger_id: str,
    actor: str,
    candidate_evidence: dict[str, Any],
    promotion: dict[str, Any],
) -> dict[str, Any]:
    """Swap the published pointer, or hand the pair to reconciliation.

    Seventh step lifted out of `run_import` (AI-287), and the SECOND half of the
    cross-store write. The warehouse already holds the rows; this is the moment
    Postgres starts calling them the published ones.

    Unlike its sibling, this one re-raises the ORIGINAL exception rather than
    translating it. That is not an oversight: a promotion failure says only "the
    warehouse is uncertain", while a publication failure has a code the caller
    acts on, and flattening it would turn a governed refusal into an opaque
    reconciliation. What the two DO share is that the dispatch is marked and
    committed before anything propagates -- the reconciliation evidence carries
    the promotion, so a settling read knows the warehouse half already happened.

    BOTH OUTCOMES GO THROUGH THE WALK -- AI-287's first contradiction, resolved
    the way AI-223 resolved its own: one machine, one door. This seam wrote
    `reconcile_required` and `published` with a direct `advance_dispatch`, so
    `_EXITS` and `_ORDER` never saw either, and the module carried two spellings
    of the same exit -- `walk.leave("reconcile_required")` in the two landing
    seams, a raw call here and in the promotion seam. `leave` owns the commit,
    which is why the explicit one is gone with it.

    On the success side `commit_publication` has ALREADY set the dispatch row to
    `published` inside its own atomic group (`datastream_publication.py`, the
    `UPDATE app.managed_file_dispatches` guarded on `promoting`/
    `reconcile_required`), and it returns no `dispatch` key of its own. So
    `walk.advance("published")` re-reads rather than rewrites -- `advance_dispatch`
    returns the existing row unchanged when the state already matches -- and the
    walk ends up believing what Postgres committed. The `dispatch` key is still
    honoured first, because a payload that carries the row is a read the walk
    should ADOPT rather than fetch again.
    """
    from core.datastream_publication import commit_publication  # noqa: PLC0415

    try:
        publication = commit_publication(
            execution_id,
            project_id,
            actor,
            conn,
            ledger_id=ledger_id,
            dispatch_id=dispatch_id,
            candidate_evidence={
                **candidate_evidence,
                "warehouse_promotion": promotion,
            },
        )
        published_dispatch = publication.get("dispatch")
        if published_dispatch is not None:
            walk.adopt(published_dispatch)
        else:
            walk.advance("published")
    except Exception as exc:
        walk.leave(
            "reconcile_required",
            error_code=getattr(exc, "code", "publication_reconciliation_required"),
            reconciliation_evidence={
                "phase": "postgres_publication",
                "promotion": promotion,
                "error": type(exc).__name__,
                "execution_id": execution_id,
            },
        )
        raise
    return publication


def _publish_governed_candidate(
    conn,
    *,
    walk: "_GovernedDispatchWalk",
    dispatch_id: str | None,
    project_id: str,
    actor: str,
    execution,
    execution_id: str,
    ledger_id: str,
    ledger_after,
    import_contract_id,
    landing_relation: str,
    landed: dict[str, Any],
    warehouse_columns,
    accepted_row_count: int,
    rejected_row_count: int,
    candidate_content_fingerprint: str,
    candidate_schema_fingerprint: str,
    expected_content_fingerprint: str,
    expected_schema_fingerprint: str,
    bundle_fingerprint: str,
    projection_plan: dict[str, Any],
    force_empty_publish: bool,
    replay: bool,
) -> dict[str, Any]:
    """Validate, gate, promote, publish -- the governed publication, in order.

    Eighth step lifted out of `run_import` (AI-287), and the one the action item
    called porteuse: the last hundred and eighty lines of a function that already
    read as a sequence everywhere else. It chains the two halves of the
    cross-store write and owns the three refusals between them.

    The order is the contract, and each step earns its place:

      1. `validating` -- the execution leaves `loading`, so a concurrent reader
         cannot mistake a candidate under inspection for one ready to publish;
      2. the 12.5 DQ GATES, against the OBSERVED candidate;
      3. a refusal ends the import here, through `_refuse_on_dq_gates`;
      4. `ready` carries positive DQ evidence, which migration 194 freezes;
      5. the warehouse merge, then the Postgres pointer swap -- two calls, two
         reconciliation phases, never one transaction.

    Nothing between steps 4 and 5 is conditional on anything the caller still
    decides, which is what lets this be one step rather than a flag the caller
    threads through the sequence.
    """
    import core.managed_feed_ledger as _mfl  # noqa: PLC0415
    from core.datastream_publication import (  # noqa: PLC0415
        STATE_LOADING,
        STATE_READY,
        STATE_VALIDATING,
        advance_state,
        run_dq_gates,
    )

    current_state = (execution or {}).get("state")
    if current_state == STATE_LOADING:
        execution = advance_state(
            execution_id,
            STATE_LOADING,
            STATE_VALIDATING,
            actor,
            conn,
            project_id=project_id,
            content_hash=candidate_content_fingerprint,
            row_count=accepted_row_count,
        )
        current_state = execution.get("state")
    walk.advance("validating")

    dq_issues = run_dq_gates(
        execution_id,
        project_id,
        conn,
        force_empty_publish=force_empty_publish,
        validated_content_hash=expected_content_fingerprint,
        plan_source_schema_hash=projection_plan.get("source_schema_hash"),
        current_capability_fingerprint=(
            projection_plan.get("capability_fingerprint")
        ),
        landing_schema_hash=candidate_schema_fingerprint,
        plan_declared_schema_hash=expected_schema_fingerprint,
    )
    if dq_issues:
        return _refuse_on_dq_gates(
            conn,
            dq_issues=dq_issues,
            walk=walk,
            current_state=current_state,
            execution_id=execution_id,
            ledger_id=ledger_id,
            ledger_after=ledger_after,
            observed_execution=execution,
            project_id=project_id,
            actor=actor,
            landing_relation=landing_relation,
            import_contract_id=import_contract_id,
            replay=replay,
            accepted=accepted_row_count,
            rejected=rejected_row_count,
        )

    if current_state == STATE_VALIDATING:
        execution = advance_state(
            execution_id,
            STATE_VALIDATING,
            STATE_READY,
            actor,
            conn,
            project_id=project_id,
        )
    dq_evidence = {
        "status": "passed",
        "execution_id": execution_id,
        "row_count": accepted_row_count,
        "expected_content_fingerprint": expected_content_fingerprint,
        "observed_content_fingerprint": candidate_content_fingerprint,
        "expected_schema_fingerprint": expected_schema_fingerprint,
        "observed_schema_fingerprint": candidate_schema_fingerprint,
    }
    walk.advance("ready", dq_evidence=dq_evidence)
    candidate_evidence = {
        "candidate_content_fingerprint": candidate_content_fingerprint,
        "candidate_schema_fingerprint": candidate_schema_fingerprint,
        "dispatch_bundle_fingerprint": bundle_fingerprint,
        "landing_relation": str(landed.get("table") or landing_relation),
    }

    promotion = _promote_candidate_or_reconcile(
        conn,
        walk=walk,
        dispatch_id=dispatch_id,
        project_id=project_id,
        execution_id=execution_id,
        actor=actor,
        landing_relation=landing_relation,
        landed=landed,
        warehouse_columns=warehouse_columns,
        accepted_row_count=accepted_row_count,
        candidate_evidence=candidate_evidence,
        candidate_content_fingerprint=candidate_content_fingerprint,
        candidate_schema_fingerprint=candidate_schema_fingerprint,
    )
    publication = _commit_publication_or_reconcile(
        conn,
        walk=walk,
        dispatch_id=dispatch_id,
        project_id=project_id,
        execution_id=execution_id,
        ledger_id=ledger_id,
        actor=actor,
        candidate_evidence=candidate_evidence,
        promotion=promotion,
    )
    return {
        "ledger": publication.get("ledger") or ledger_after,
        "execution": publication["execution"],
        "publication_log_id": publication["publication_log_id"],
        "prior_execution_id": publication["prior_execution_id"],
        "import_contract_id": import_contract_id,
        "no_op": False,
        "replay": replay,
        "row_count": accepted_row_count,
        "rejected_count": rejected_row_count,
        "landed_row_count": landed.get("rows", 0),
        "landing": {
            "table": landed.get("table"),
            "backend": landed.get("backend"),
            "isolated_execution": execution_id,
        },
        "blocked": False,
        "reason": None,
        "outcome": _mfl.OUTCOME_PUBLISHED,
        "published": True,
        "dispatch": walk.current,
    }


def _land_mapping_declared_route(
    conn,
    *,
    route: str,
    savepoint: str,
    land: Callable[[], dict[str, Any]],
    landing_summary: Callable[[dict[str, Any]], dict[str, Any]],
    landed_row_count: Callable[[dict[str, Any]], int],
    accepted_rows: list[dict[str, Any]],
    rejected_rows_parsed: list[Any],
    rejection_detail: list[dict[str, Any]],
    ledger: dict[str, Any],
    ledger_id: str,
    ledger_result: dict[str, Any],
    execution: Any,
    execution_id: str,
    import_contract_id: Any,
    project_id: str,
    content_hash: str,
    actor: str,
) -> dict[str, Any]:
    """A mapping-declared route, from accepted rows to the honest terminal.

    ONE function for the two routes epic 68 adds -- events (68.4) and reference
    (68.5) -- because everything between the accepted rows and the terminal is
    the same sequence, and two copies of it would drift the first time either
    one gained a step. What differs is passed in: ``land`` writes the rows
    through that route's own governed writers, and ``landing_summary`` says
    what it wrote.

    The SAME sequence the warehouse route runs, reusing every governed seam of
    it -- the ledger's ``record_rows``, the UNMOCKED rejection gate,
    ``mark_outcome`` -- and changing ONLY the landing itself:

      1. the write runs inside a SAVEPOINT. It shares the caller's transaction
         (there is no second store on either route), so a rejection-gate
         refusal takes every row back with one ROLLBACK TO SAVEPOINT -- the
         same honesty the warehouse route gets from ``discard_candidate``;
      2. the ledger records rows + rejections exactly as for a warehouse
         landing, and the rejection gate runs on the recorded counts;
      3. a refused import ends ``rejected`` with a FAILED execution and ZERO
         rows committed on the route;
      4. a passing import ends with the execution ``collected`` -- a run that
         moved data and stopped, since no warehouse pointer exists to swap --
         and the ledger ``published``. The write inside the landing IS
         this route's publication act, and the ledger's unchanged-snapshot
         oracle only trusts 'published' rows: this mark is what makes a
         byte-identical re-import an honest ``noop`` rather than a second
         landing.

    A replay of a 'written' ledger row RE-RUNS the landing instead of trusting
    the recorded counts: each route's own idempotence (the per-entity content
    hash, the delete-window) makes the re-run a no-op when the first landing
    committed, and completes it when it did not.
    """
    import core.managed_feed_ledger as _mfl  # noqa: PLC0415
    from core.datastream_publication import (  # noqa: PLC0415
        STATE_COLLECTED,
        STATE_CREATED,
        STATE_FAILED,
        STATE_LOADING,
        advance_state,
    )

    replay = bool(ledger_result.get("replay"))

    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {savepoint}")
    landed = land()

    if replay and ledger.get("outcome") == _mfl.OUTCOME_WRITTEN:
        ledger_after = ledger
    else:
        ledger_after = _mfl.record_rows(
            ledger_id=ledger_id,
            project_id=project_id,
            landing_relation=str(landed["table"]),
            accepted_row_count=len(accepted_rows),
            content_hash=content_hash,
            rejected_rows=rejection_detail,
            actor=actor,
            conn=conn,
        )

    gate_issue = _mfl.evaluate_rejection_gate_for_ledger(ledger_id, project_id, conn)
    if gate_issue:
        with conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        execution = advance_state(
            execution_id,
            (execution or {}).get("state"),
            STATE_FAILED,
            actor,
            conn,
            project_id=project_id,
            error_code=gate_issue.get("code"),
            error_detail=gate_issue.get("detail"),
        )
        ledger_after = _mfl.mark_outcome(
            ledger_id=ledger_id,
            project_id=project_id,
            outcome=_mfl.OUTCOME_REJECTED,
            actor=actor,
            error_code=gate_issue.get("code"),
            error_detail=gate_issue.get("detail"),
            conn=conn,
        )
        return {
            "ledger": ledger_after,
            "execution": execution,
            "import_contract_id": import_contract_id,
            "no_op": False,
            "replay": replay,
            "row_count": len(accepted_rows),
            "rejected_count": len(rejected_rows_parsed),
            "blocked": True,
            "reason": "rejection_threshold_exceeded",
            "gate_issue": gate_issue,
            "outcome": _mfl.OUTCOME_REJECTED,
            "published": False,
            "route": route,
            "dispatch": None,
        }

    with conn.cursor() as cur:
        cur.execute(f"RELEASE SAVEPOINT {savepoint}")

    current_state = (execution or {}).get("state")
    if current_state == STATE_CREATED:
        execution = advance_state(
            execution_id,
            STATE_CREATED,
            STATE_LOADING,
            actor,
            conn,
            project_id=project_id,
        )
        current_state = execution.get("state")
    if current_state == STATE_LOADING:
        execution = advance_state(
            execution_id,
            STATE_LOADING,
            STATE_COLLECTED,
            actor,
            conn,
            project_id=project_id,
            content_hash=content_hash,
            row_count=len(accepted_rows),
        )
    ledger_after = _mfl.mark_outcome(
        ledger_id=ledger_id,
        project_id=project_id,
        outcome=_mfl.OUTCOME_PUBLISHED,
        actor=actor,
        conn=conn,
    )
    return {
        "ledger": ledger_after,
        "execution": execution,
        "import_contract_id": import_contract_id,
        "no_op": False,
        "replay": replay,
        "row_count": len(accepted_rows),
        "rejected_count": len(rejected_rows_parsed),
        "landed_row_count": landed_row_count(landed),
        "landing": {
            "table": landed["table"],
            "backend": landed["backend"],
            "isolated_execution": execution_id,
            **landing_summary(landed),
        },
        "blocked": False,
        "reason": None,
        "outcome": _mfl.OUTCOME_PUBLISHED,
        "published": True,
        "route": route,
        "dispatch": None,
    }


def _land_declared_non_fact_route(
    conn,
    *,
    events_route: dict[str, Any] | None,
    reference_route: dict[str, Any] | None,
    accepted_rows: list[dict[str, Any]],
    rejected_rows_parsed: list[Any],
    rejection_detail: list[dict[str, Any]],
    ledger: dict[str, Any],
    ledger_id: str,
    ledger_result: dict[str, Any],
    execution: Any,
    execution_id: str,
    import_contract_id: Any,
    project_id: str,
    datastream_id: str,
    mapping_version_id: str,
    content_hash: str,
    actor: str,
) -> dict[str, Any]:
    """WHICH non-fact route the mapping declared, and how it is landed.

    THE TWO MAPPING-DECLARED ROUTES LAND HERE (Stories 68.4 and 68.5), on the
    same ledger row, with the same rejection evidence -- and NEITHER opens a
    warehouse relation. The caller reaches this step after the dispatch walk
    (which both routes forbid, so `walk` is None there) and before
    `allocate_landing_relation`, because allocating a raw relation for a file
    that carries a calendar or an entity catalogue would be exactly the silent
    fact-landing these stories exist to refuse.

    WHY IT IS A STEP OF ITS OWN (AI-330, criterion 7). `run_import` came out of
    AI-287 at 526 lines behind eight named steps, and grew back to 676: 116 of
    the 151 lines added were this branch, arriving in two stories a day apart
    (`01271194` the reference route, +46; `f43cb998` the events route, +70).
    The sequence they share was already a named step -- `_land_mapping_declared_
    route`, which this one calls -- so what was left inline was ONLY the
    adapter: which route, which savepoint, which governed writer, what it wrote,
    and how many rows that is. That is a complete answer to one question, it
    needs no state from any later step, and it ends the import: the boundary
    AD-42 asks for, not a cut on a flag.
    """
    from core.context_event_import import (  # noqa: PLC0415
        land_context_event_rows,
    )
    from core.entity_reference_import import (  # noqa: PLC0415
        land_entity_reference_rows,
        project_org_id,
    )

    org_id = project_org_id(conn, project_id)
    if events_route is not None:
        route_name = ROUTE_CONTEXT_EVENTS
        savepoint = "sp_context_events_landing"

        def _land() -> dict[str, Any]:
            return land_context_event_rows(
                conn,
                route=events_route,
                rows=accepted_rows,
                project_id=project_id,
                datastream_id=datastream_id,
                org_id=org_id,
                execution_id=execution_id,
                mapping_version_id=mapping_version_id,
                actor=actor,
            )

        def _summary(landed: dict[str, Any]) -> dict[str, Any]:
            return {
                "events_written": landed["events_written"],
                "events_replaced": landed["events_replaced"],
                "event_types": landed["event_types"],
                "window": landed["window"],
                "event_configuration_version_id": landed[
                    "event_configuration_version_id"
                ],
            }

        def _count(landed: dict[str, Any]) -> int:
            return int(landed["events_written"])
    else:
        route_name = ROUTE_ENTITY_REFERENCE
        savepoint = "sp_entity_reference_landing"

        def _land() -> dict[str, Any]:
            return land_entity_reference_rows(
                conn,
                org_id=org_id,
                project_id=project_id,
                registry=reference_route["registry"],
                key_target=reference_route["key_target"],
                attribute_targets=reference_route["attribute_targets"],
                attribute_types=reference_route.get("attribute_types"),
                rows=accepted_rows,
                actor=actor,
                execution_id=execution_id,
                mapping_version_id=mapping_version_id,
                ledger_id=ledger_id,
                datastream_id=datastream_id,
            )

        def _summary(landed: dict[str, Any]) -> dict[str, Any]:
            return {
                "registry_id": landed["registry_id"],
                "nodes_created": landed["nodes_created"],
                "versions_minted": landed["versions_minted"],
                "entities_unchanged": landed["entities_unchanged"],
            }

        def _count(landed: dict[str, Any]) -> int:
            return int(landed["versions_minted"])

    return _land_mapping_declared_route(
        conn,
        route=route_name,
        savepoint=savepoint,
        land=_land,
        landing_summary=_summary,
        landed_row_count=_count,
        accepted_rows=accepted_rows,
        rejected_rows_parsed=rejected_rows_parsed,
        rejection_detail=rejection_detail,
        ledger=ledger,
        ledger_id=ledger_id,
        ledger_result=ledger_result,
        execution=execution,
        execution_id=execution_id,
        import_contract_id=import_contract_id,
        project_id=project_id,
        content_hash=content_hash,
        actor=actor,
    )


def run_import(
    data: bytes,
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
    projection_plan: dict[str, Any],
    actor: str,
    idempotency_key: str,
    source_metadata: dict[str, Any],
    contract: dict[str, Any],
    conn,  # psycopg connection; caller-managed
    raw_schema: str | None = None,
    write_mode: str = WRITE_MODE_REPLACE,
    force_empty_publish: bool = False,
    preferences: dict[str, Any] | None = None,
    producer: Callable[[bytes], "ParseResult"] | None = None,
    mapping_payload: dict[str, Any] | None = None,
    dispatch_bundle: dict[str, Any] | None = None,
    publish_candidate: bool = False,
    allow_snapshot_noop: bool = True,
) -> dict[str, Any]:
    """Drive a CSV/Excel upload through the 12.8 ledger + 12.5 DQ gates.

    File-source producer path (Story 22.12, AD-1): when ``producer`` is supplied,
    the PARSE STEP delegates to it (a ``file_source_producer.produce`` closure
    returning a ``ParseResult`` with rows keyed by canonical field ids) instead of
    ``parse_csv``/``parse_excel``. Everything downstream -- the 12.8 ledger
    (``open_import``/``record_rows``), the rejection gate, and the landing
    relation -- is REUSED UNCHANGED; no second ledger or landing path is
    introduced. The file-source template is the versioned contract (Story 22.11),
    so this path threads ``source_metadata['import_contract_id']`` (optional)
    rather than versioning a CSV/Excel contract.

    This is the ORCHESTRATION step (the second half of the import flow). The caller
    must have already called ``build_preview`` to confirm the file is valid and the
    operator has reviewed the column mapping.

    The parsing contract is VERSIONED (AC2) via ``version_contract`` and its
    ``cic_<ULID>`` id is threaded onto the ledger row (``import_contract_id``).

    Raises:
      AppendUnavailable         -- write_mode='append' is not supported.
      CsvExcelImportError       -- parse failure (encoding, format, structural).
      ImportPayloadConflict     -- same idempotency key, different payload (-> 409).
      ImportInProgress          -- another import is already active (-> 409).
      InvalidImportContract     -- contract format does not match the actual bytes.

    Returns a dict with, on the passing-gate happy path::

      {
        "ledger": {...},        # the mfl_<ULID> ledger row ('written')
        "execution": {...},     # the dse_<ULID> candidate
        "import_contract_id": "cic_<ULID>",
        "no_op": False,
        "replay": bool,
        "row_count": int,
        "rejected_count": int,
        "blocked": False,
        "reason": None,
        "outcome": "written_pending_publication",  # HONEST: not yet published
        "published": False,                        # physical write + swap are Phase B
      }

    On an empty-blocked / rejection-blocked / no-op path the same keys are present with
    ``blocked``/``no_op`` set and ``published=False``.
    """
    # AC5: Append unavailable (before ANY parsing / ledger write).
    if write_mode == WRITE_MODE_APPEND:
        raise AppendUnavailable()

    # Confirmation and unattended replay re-validate bytes independently of any
    # earlier preview or transport scan. Preview is evidence, never authority.
    validate_upload_meta(data)

    result, fmt = parse_for_contract(
        data,
        contract=contract,
        source_metadata=source_metadata,
        mapping_version_id=mapping_version_id,
        producer=producer,
    )

    blocking_issues = [issue for issue in result.issues if issue.blocking]
    if blocking_issues:
        raise ParserReviewRequired(blocking_issues)

    # THE ROUTER (epic 68): ONE import, three declared destinations -- facts
    # (the warehouse), events (68.4 plugs into the same router), reference
    # (68.5). Resolved before the pinned mapping is applied: the reference
    # route hands its per-row key validator to the mapping pass, so a row that
    # cannot name its entity is rejected in the SAME enumeration as the type
    # coercions, with line numbers both rejection classes agree on.
    import_route = _resolve_import_route(
        producer,
        mapping_payload=mapping_payload,
        projection_plan=projection_plan,
        source_metadata=source_metadata,
        publish_candidate=publish_candidate,
        conn=conn,
        project_id=project_id,
    )
    reference_route = import_route["reference"]
    events_route = import_route["events"]

    # Story 38.13: transport bytes are not a Universal candidate. Apply the exact
    # pinned mapping/projection bundle before a warehouse relation is opened.
    # Epic 22 producers already return canonical typed rows through their own
    # versioned extension seam; they are not reinterpreted here.
    if mapping_payload is not None and not isinstance(producer, FileSourceProducer):
        result = _apply_governed_mapping(
            result,
            mapping_payload=mapping_payload,
            projection_plan=projection_plan,
            plan_version_id=plan_version_id,
            mapping_version_id=mapping_version_id,
            row_validator=_route_row_validator(events_route, reference_route),
        )
    elif publish_candidate and producer is None:
        raise CsvExcelImportError(
            "dispatch_mapping_missing",
            "governed dispatch requires the pinned mapping payload",
            repair={"reload_pinned_bundle": True},
        )
    elif publish_candidate and isinstance(producer, FileSourceProducer):
        result = producer.bind_dispatch(
            result,
            projection_plan=projection_plan,
            plan_version_id=plan_version_id,
            mapping_version_id=mapping_version_id,
        )

    accepted_rows = result.rows
    rejected_rows_parsed = result.rejected
    content_hash = result.content_hash

    # AC4: Empty-file / zero-row guard (checked BEFORE opening the ledger so we never
    # mint a useless candidate for a trivially blocked import).
    allow_empty = (preferences or {}).get("allow_empty_publication", False)
    if len(accepted_rows) == 0:
        if not (force_empty_publish and allow_empty):
            return {
                "ledger": None,
                "execution": None,
                "import_contract_id": None,
                "no_op": False,
                "replay": False,
                "row_count": 0,
                "rejected_count": len(rejected_rows_parsed),
                "blocked": True,
                "reason": "empty_import",
                "outcome": "empty_import_blocked",
                "published": False,
            }

    # Epic 22 owns all media-specific placement/discriminator/gate behavior.
    # The Universal dispatcher sees one versioned extension method and no rule.
    if isinstance(producer, FileSourceProducer):
        result, extension_block = producer.prepare_for_landing(
            result,
            filename=source_metadata.get("filename"),
        )
        accepted_rows = result.rows
        if extension_block is not None:
            return {
                "ledger": None,
                "execution": None,
                "import_contract_id": None,
                "no_op": False,
                "replay": False,
                "row_count": len(accepted_rows),
                "rejected_count": len(rejected_rows_parsed),
                "blocked": True,
                "published": False,
                **extension_block,
            }

    # WHERE this import lands -- read from the ONE router resolved above, never
    # recomputed here: two calls to the same resolver are two places the
    # destinations could drift apart.
    landing_target = import_route["landing_target"]
    plan_id = import_route["plan_id"]

    # Import the module (not individual names) so tests can patch
    # ``core.managed_feed_ledger.*`` reliably.
    import core.managed_feed_ledger as _mfl  # noqa: PLC0415

    # AC2: version (or dedup) the parsing contract and thread its id onto the ledger.
    # File-source path (22.12): the versioned contract is the file-source template
    # (Story 22.11), not a CSV/Excel contract -- thread its id if the caller supplied
    # one; never version a CSV/Excel contract here.
    if producer is not None:
        # Epic 22 Template identity is itself the immutable parsing/adaptation
        # contract. Persist its exact id, never a mutable active lookup.
        import_contract_id = (
            producer.template_id
            if isinstance(producer, FileSourceProducer)
            else source_metadata.get("import_contract_id")
        )
    else:
        import_contract_id = version_contract(
            contract,
            datastream_id=datastream_id,
            project_id=project_id,
            actor=actor,
            conn=conn,
        )

    # Freeze the complete governed bundle into the immutable ledger identity.
    # Identical bytes under any changed plan/mapping/template/parser/projection
    # must reprocess rather than hit the content-only no-op path.
    bundle = {
        "plan_version_id": plan_version_id,
        "mapping_version_id": mapping_version_id,
        "import_contract_id": import_contract_id,
        "projection_fingerprint": _canonical_fingerprint(projection_plan),
        "candidate_columns": [
            {"name": column.name, "type": _WAREHOUSE_TYPES[column.detected_type]}
            for column in result.columns
        ]
        + [
            {"name": "execution_id", "type": "STRING"},
            {"name": "plan_version_id", "type": "STRING"},
            {"name": "mapping_version_id", "type": "STRING"},
            {"name": "project_id", "type": "STRING"},
        ],
        **(dispatch_bundle or {}),
    }
    if isinstance(producer, FileSourceProducer):
        bundle.update(
            {
                "template_id": producer.template_id,
                "template_content_hash": producer.template.get("content_hash"),
                "template_confirmation_operation_id": (
                    producer.confirmation_operation_id
                ),
            }
        )
    bundle_fingerprint = _canonical_fingerprint(bundle)
    source_metadata = {
        **source_metadata,
        "dispatch_bundle": bundle,
        "dispatch_bundle_fingerprint": bundle_fingerprint,
    }

    # Delegate to the 12.8 ledger (open_import mints the ledger row + candidate BEFORE
    # any row is written -- NFR14).
    ledger_result = _mfl.open_import(
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
        feed_format=fmt,
        projection_plan=projection_plan,
        actor=actor,
        idempotency_key=idempotency_key,
        source_metadata={
            **source_metadata,
            "parsed_row_count": len(accepted_rows),
            "rejected_row_count": len(rejected_rows_parsed),
            "format": fmt,
            "parser_evidence": _parse_evidence(result),
        },
        content_hash=content_hash,
        conn=conn,
        write_mode=WRITE_MODE_REPLACE,
        import_contract_id=import_contract_id,
        allow_snapshot_noop=allow_snapshot_noop,
    )

    if ledger_result.get("no_op"):
        return {
            **ledger_result,
            "import_contract_id": import_contract_id,
            "row_count": 0,
            "rejected_count": 0,
            "blocked": False,
            "reason": None,
            "outcome": "noop",
            "published": False,
        }

    ledger = ledger_result["ledger"]
    ledger_id = ledger["id"]
    execution = ledger_result.get("execution") or {}
    execution_id = execution.get("id")
    if not execution_id:
        raise CsvExcelImportError(
            "dispatch_execution_missing",
            "the import ledger has no scoped candidate execution",
            repair={"reconcile_dispatch": True},
        )

    # The durable coordinator opens here, and it may already hold the answer.
    walk, dispatch_id, settled = _open_governed_dispatch(
        conn,
        publish_candidate=publish_candidate,
        source_metadata=source_metadata,
        bundle=bundle,
        ledger=ledger,
        ledger_id=ledger_id,
        execution=execution,
        execution_id=execution_id,
        datastream_id=datastream_id,
        project_id=project_id,
        import_contract_id=import_contract_id,
    )
    if settled is not None:
        return settled

    # A retry never repeats a cross-store write. A written import resumes from
    # its persisted execution; an opened import has an uncertain warehouse
    # outcome and is retained for deterministic reconciliation.
    if ledger_result.get("replay") and ledger.get("outcome") == _mfl.OUTCOME_OPENED:
        return {
            "ledger": ledger,
            "execution": execution,
            "import_contract_id": import_contract_id,
            "no_op": False,
            "replay": True,
            "row_count": ledger.get("row_count") or 0,
            "rejected_count": ledger.get("rejected_row_count") or 0,
            "blocked": True,
            "reason": "dispatch_reconciliation_required",
            "outcome": "dispatch_reconciliation_required",
            "published": False,
            "dispatch": walk.current,
        }

    # Build the per-row rejection payloads for record_rows.
    rejection_detail = [
        {
            "row_number": rr.row_number,
            "field_name": rr.field_name,
            "rule": rr.rule,
            "reason": rr.reason,
            "rejected_value": _truncate_value(rr.rejected_value),
        }
        for rr in rejected_rows_parsed
    ]

    if events_route is not None or reference_route is not None:
        return _land_declared_non_fact_route(
            conn,
            events_route=events_route,
            reference_route=reference_route,
            accepted_rows=accepted_rows,
            rejected_rows_parsed=rejected_rows_parsed,
            rejection_detail=rejection_detail,
            ledger=ledger,
            ledger_id=ledger_id,
            ledger_result=ledger_result,
            execution=execution,
            execution_id=execution_id,
            import_contract_id=import_contract_id,
            project_id=project_id,
            datastream_id=datastream_id,
            mapping_version_id=mapping_version_id,
            content_hash=content_hash,
            actor=actor,
        )

    # Allocate the landing relation (one per datastream, project-scoped raw).
    # A plan-store target has no warehouse relation to allocate: its landing is a
    # new version of a plan that already exists, and naming it here keeps the
    # ledger's evidence just as specific as a `schema.table`.
    if landing_target == LANDING_PLAN_STORE:
        landing_relation = f"plan_store:{plan_id}"
    else:
        landing_relation = _mfl.allocate_landing_relation(
            datastream_id=datastream_id,
            project_id=project_id,
            raw_schema=raw_schema,
        )

    provenance_specs = [
        ColumnSpec("execution_id", len(result.columns), "text"),
        ColumnSpec("plan_version_id", len(result.columns) + 1, "text"),
        ColumnSpec("mapping_version_id", len(result.columns) + 2, "text"),
        ColumnSpec("project_id", len(result.columns) + 3, "text"),
    ]
    landing_rows = [
        {
            **row,
            "execution_id": execution_id,
            "plan_version_id": plan_version_id,
            "mapping_version_id": mapping_version_id,
            "project_id": project_id,
        }
        for row in accepted_rows
    ]
    warehouse_columns = [
        (spec.name, _WAREHOUSE_TYPES[spec.detected_type])
        for spec in [*result.columns, *provenance_specs]
    ]
    from core.raw_landing import (  # noqa: PLC0415
        candidate_rows_fingerprint,
        candidate_schema_fingerprint,
    )

    expected_candidate_fingerprint = candidate_rows_fingerprint(
        landing_rows, columns=warehouse_columns
    )
    expected_candidate_schema_fingerprint = candidate_schema_fingerprint(
        warehouse_columns
    )

    if publish_candidate:
        from core.datastream_publication import (  # noqa: PLC0415
            STATE_CREATED,
            STATE_LOADING,
            advance_state,
        )

        current_state = execution.get("state")
        if current_state == STATE_CREATED:
            execution = advance_state(
                execution_id,
                STATE_CREATED,
                STATE_LOADING,
                actor,
                conn,
                project_id=project_id,
            )
        walk.advance("landing")

    # The physical row write. This used to be a comment saying "Phase B goes
    # here", and the consequence was that an upload -- and every email ingress,
    # which shares this path -- recorded a ledger, counted its rows, passed its
    # DQ gates and landed NOTHING. The counts were real and the data was not.
    #
    # It goes through `raw_landing.land_raw_rows`, the same seam the connectors
    # use, rather than the DuckDB-only helper in `google_sheets_sync`. Three
    # things come from reusing it instead of writing a second lander:
    #   * BigQuery and DuckDB are already routed there, so this is not
    #     DuckDB-only the way the Sheets path still is;
    #   * an active `candidate_execution` scope is honoured automatically, which
    #     is what lets a managed-feed candidate be isolated by execution at all;
    #   * a partial streaming failure raises with its per-row errors rather than
    #     silently dropping rows.
    #
    # Governed candidate columns keep the mapping's declared physical types;
    # provenance columns remain text. The write never guesses from cell values.
    if ledger_result.get("replay") and ledger.get("outcome") == _mfl.OUTCOME_WRITTEN:
        landed = {
            "rows": ledger.get("row_count") or 0,
            "table": ledger.get("landing_relation"),
            "backend": None,
            "execution_id": execution_id,
        }
        ledger_after = ledger
    else:
        from core.raw_landing import candidate_execution  # noqa: PLC0415

        try:
            if landing_target == LANDING_PLAN_STORE:
                # The plan store is transactional Postgres on the caller's own
                # connection, not the warehouse: there is no candidate execution
                # to isolate, because the version IS the isolation -- it lands as
                # a candidate and the published version stays untouched.
                landed = land_plan_store_rows(
                    rows=accepted_rows,
                    plan_id=plan_id,
                    actor=actor,
                    conn=conn,
                    source_note=source_metadata.get("filename") or None,
                )
            else:
                with candidate_execution(execution_id):
                    landed = _land_managed_rows(
                        landing_relation=landing_relation,
                        rows=landing_rows,
                        project_id=project_id,
                        columns=[*result.columns, *provenance_specs],
                    )
        except Exception:
            if publish_candidate:
                walk.leave(
                    "reconcile_required",
                    error_code="warehouse_write_outcome_uncertain",
                    reconciliation_evidence={
                        "execution_id": execution_id,
                        "landing_relation": landing_relation,
                    },
                )
            raise

        actual_landing_relation = str(landed.get("table") or landing_relation)
        ledger_after = _mfl.record_rows(
            ledger_id=ledger_id,
            project_id=project_id,
            landing_relation=actual_landing_relation,
            accepted_row_count=len(accepted_rows),
            content_hash=content_hash,
            rejected_rows=rejection_detail,
            actor=actor,
            conn=conn,
        )
    candidate_fingerprint = expected_candidate_fingerprint
    candidate_schema_fingerprint = expected_candidate_schema_fingerprint
    if publish_candidate:
        candidate_fingerprint, candidate_schema_fingerprint = (
            _observe_landed_candidate(
                walk=walk,
                execution_id=execution_id,
                project_id=project_id,
                landing_relation=landing_relation,
                landed=landed,
                warehouse_columns=warehouse_columns,
                accepted_row_count=len(accepted_rows),
                expected_content_fingerprint=expected_candidate_fingerprint,
                expected_schema_fingerprint=expected_candidate_schema_fingerprint,
            )
        )

    # Evaluate the UNMOCKED rejection gate against the ledger's recorded counts.
    gate_issue = _mfl.evaluate_rejection_gate_for_ledger(ledger_id, project_id, conn)
    if gate_issue:
        return _refuse_on_rejection_gate(
            conn,
            gate_issue=gate_issue,
            walk=walk,
            publish_candidate=publish_candidate,
            execution=execution,
            execution_id=execution_id,
            ledger_id=ledger_id,
            ledger_after=ledger_after,
            project_id=project_id,
            actor=actor,
            landing_relation=landing_relation,
            import_contract_id=import_contract_id,
            replay=ledger_result.get("replay", False),
            accepted=len(accepted_rows),
            rejected=len(rejected_rows_parsed),
        )

    if publish_candidate:
        return _publish_governed_candidate(
            conn,
            walk=walk,
            dispatch_id=dispatch_id,
            project_id=project_id,
            actor=actor,
            execution=execution,
            execution_id=execution_id,
            ledger_id=ledger_id,
            ledger_after=ledger_after,
            import_contract_id=import_contract_id,
            landing_relation=landing_relation,
            landed=landed,
            warehouse_columns=warehouse_columns,
            accepted_row_count=len(accepted_rows),
            rejected_row_count=len(rejected_rows_parsed),
            candidate_content_fingerprint=candidate_fingerprint,
            candidate_schema_fingerprint=candidate_schema_fingerprint,
            expected_content_fingerprint=expected_candidate_fingerprint,
            expected_schema_fingerprint=expected_candidate_schema_fingerprint,
            bundle_fingerprint=bundle_fingerprint,
            projection_plan=projection_plan,
            force_empty_publish=force_empty_publish,
            replay=ledger_result.get("replay", False),
        )

    # Passing gate: the candidate is written but NOT published. The physical row
    # write is real now (`landing` below reports the backend and the table it
    # reached); the pointer-swap commit_publication remains deliberately separate
    # -- DO NOT claim a publish here.
    return {
        "ledger": ledger_after,
        "execution": ledger_result.get("execution"),
        "import_contract_id": import_contract_id,
        "no_op": False,
        "replay": ledger_result.get("replay", False),
        "row_count": len(accepted_rows),
        "rejected_count": len(rejected_rows_parsed),
        # What was actually landed, and where. `row_count` above is what the
        # parse accepted; these two are only equal when the write really
        # happened, and a caller can now tell the difference.
        "landed_row_count": landed.get("rows", 0),
        "landing": {
            "table": landed.get("table"),
            "backend": landed.get("backend"),
            "isolated_execution": landed.get("execution_id"),
            # Present only on a plan-store landing: the candidate version this
            # import created, so the caller can report it without a second read.
            **(
                {"plan_version": landed["plan_version"]}
                if landed.get("plan_version") is not None
                else {}
            ),
        },
        "blocked": False,
        "reason": None,
        "outcome": OUTCOME_WRITTEN_PENDING_PUBLICATION,
        "published": False,
    }
