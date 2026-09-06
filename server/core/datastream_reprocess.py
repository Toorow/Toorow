"""toorow -- ``Reprocess``: replay landing -> candidate -> publication from what already arrived.

WHY THIS MODULE EXISTS. ``Reprocess`` is the one recovery verb nothing in this
build covered, and the ratified target says so in as many words
(``docs/product-architecture/datastream-workbench-and-wizard.md``, << `Reprocess`
is the verb the product intends to have and does not >>). ``Synchronize`` and
``Reload`` were RETIRED as recovery verbs on 2026-08-12 -- the schedule and the
day-by-day coverage of story 58.4 already deliver them -- which leaves exactly
one: reapply a chosen mapping to material this deployment ALREADY HOLDS, at zero
provider cost.

WHAT WAS ACTUALLY MISSING, and it is one call. ``bounded_recovery`` minted an
execution and enqueued NOTHING, so story 63.7 made all three verbs refuse rather
than lock a Datastream behind a run no worker would ever finish. The doc named
the repair precisely: << `datastream_change` mints the candidate execution and,
in the same transaction, enqueues one `candidate_materialization` activation job
[...] `bounded_recovery` mints the execution and enqueues **nothing** -- that
missing enqueue [...] is the difference >>.

Measuring the chain end to end changed which enqueue is the right one, and the
measurement is recorded here rather than in a commit message:

  * An activation job would stop at ``ready``. ``complete_candidate_from_adapter``
    advances a candidate to Ready and no further; the publication of that
    candidate is a separate, human-confirmed act. A reprocess that stops one step
    before the thing being repaired repairs nothing.
  * The retained-artifact replay ALREADY runs the whole chain, synchronously, in
    one transaction: ``inbound_reprocess`` -> ``ingest_inbound_file`` ->
    ``run_import``, which lands, promotes and calls ``commit_publication``. That
    is literally << landing -> candidate -> publication from what already
    arrived >>, and it has been in the repository since story 38.18.

So this module opens NO second replay engine. It composes the one that exists,
and adds the three things a DATASTREAM-level verb owes that a file-level one does
not: an eligibility decision that names its refusals, a mapping gate that is
either passed or explicitly skipped, and the OUTPUT VERSION -- which is the whole
point of the repair and which no managed-feed run has ever written.

THE OUTPUT-VERSION GAP, MEASURED. ``app.datastream_output_versions`` has exactly
two writers, both in ``datastream_activation``: ``publish_activate_mutation``
(the wizard's activation) and ``record_run_output_version`` -- and the second is
called from ONE place, ``queue.py``, the connector pull path. A managed-feed
Datastream therefore carries exactly one output version for its whole life: the
one the wizard wrote, whose ``relation_ref`` is
``review["artifact_ref"]``. When the candidate landed nothing under its own
execution, that artifact ref is the synthetic ``execution/<id>/candidate/relation``
-- a string ``query_execution._safe_identifier`` refuses by construction, because
it is not a SQL identifier. The rows are in the warehouse and the pointer to them
is unreadable, for ever, because ``app.datastream_output_versions`` is immutable
by trigger (migration 138, ``trg_datastream_output_versions_immutable``) and the
trigger is RIGHT: evidence of what was published does not get rewritten.

The only honest repair for an immutable wrong row is a NEW row that supersedes
it. ``query_execution`` reads ``ORDER BY created_at DESC LIMIT 1`` for the
(datastream, mapping version) pair, so a later version IS the head and the older
one is superseded without anything being mutated or deleted.

WHY THE REPLAY MUST NOT NO-OP. ``managed_feed_ledger._find_published_snapshot``
answers "unchanged" for the same bytes under the same governed bundle, and an
unchanged snapshot writes a ``noop`` ledger row with NO candidate and NO landing.
For a normal arrival that is exactly right. For a reprocess it is the failure
mode: the act being repaired is the PUBLICATION, not the data, so the bytes are
unchanged BY DEFINITION. ``ingest_inbound_file(force_new_execution=True)`` is the
existing switch for precisely this (it passes ``allow_snapshot_noop=False``), and
``inbound_reprocess`` already sets it. Reprocess inherits that, rather than
inventing a second way to say the same thing.

THE MAPPING GATE. A reprocess under a DIFFERENT mapping than the one that
published is a governed change of what the numbers mean, so it goes back through
the human gate: the AD-27 proposal states both mapping versions and a person
confirms it. A reprocess under the SAME mapping changes nothing about meaning,
so the gate is SKIPPED -- explicitly, attributed and dated on the durable
operation, never silently. An unrecorded skip and a gate that was never reached
look identical afterwards, and that is the whole reason this is written down.

THE REFUSALS, and each names the gesture that repairs it rather than its cause:

  * ``artifact_not_retained`` -- nothing arrived that we still hold, or the
    delivery's bytes are gone by retention policy. The gesture is to RE-IMPORT
    the file; reprocessing never asks the source for anything.
  * ``artifact_unreadable`` -- the row says the bytes are retained and the store
    disagrees. A storage fault reported as "nothing to reprocess" would read as a
    retention decision, which is a different fact about the world.
  * ``source_would_be_called`` -- this Datastream's mode has no artifact this
    deployment holds; replaying it would pull from the provider and spend. The
    gesture is the day-by-day coverage, which is bounded and names the spend.
  * ``run_in_flight`` -- a run already holds this Datastream's publication lock.
  * ``mapping_not_found`` -- the chosen mapping version is not this Datastream's.

The CALLER owns the transaction. ASCII-only source (AI-03).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# --- Eligibility states. `available` is the only one that dispatches. --------
STATE_AVAILABLE = "available"

REFUSAL_ARTIFACT_NOT_RETAINED = "artifact_not_retained"
REFUSAL_ARTIFACT_UNREADABLE = "artifact_unreadable"
REFUSAL_SOURCE_WOULD_BE_CALLED = "source_would_be_called"
REFUSAL_RUN_IN_FLIGHT = "run_in_flight"
REFUSAL_MAPPING_NOT_FOUND = "mapping_not_found"
REFUSAL_DATASTREAM_NOT_FOUND = "not_found"

#: Every refusal this verb can answer, so a reader can enumerate them without
#: grepping the module. The API maps each to a status; the console mirrors the
#: message, never the code.
REFUSAL_CODES: tuple[str, ...] = (
    REFUSAL_ARTIFACT_NOT_RETAINED,
    REFUSAL_ARTIFACT_UNREADABLE,
    REFUSAL_SOURCE_WOULD_BE_CALLED,
    REFUSAL_RUN_IN_FLIGHT,
    REFUSAL_MAPPING_NOT_FOUND,
    REFUSAL_DATASTREAM_NOT_FOUND,
)

# --- The mapping gate. Two states, and the skip is as recorded as the pass. ---
GATE_PASSED = "passed"
GATE_SKIPPED = "skipped"

#: The Datastream modes whose retained artifact this deployment HOLDS, and can
#: therefore replay without asking anybody for anything.
#:
#: It is a declaration and not a branch on a name: `connector_pull` and
#: `external_bq` are absent because replaying them re-reads a remote system --
#: which is a Reload, which was retired because `Day-by-day coverage` already
#: does it, bounded and with the spend named before the act. A mode added to this
#: set without an artifact resolver below refuses at `artifact_not_retained`,
#: which is the fail-closed direction.
REPLAYABLE_MODES: frozenset[str] = frozenset({"managed_feed"})

#: What a person is told when their Datastream's mode has nothing retained here.
_SOURCE_WOULD_BE_CALLED = (
    "Reprocessing replays material this deployment already holds, and this "
    "Datastream's data lives at its source: replaying it would call the source "
    "and spend on that account. Use Day-by-day coverage on the Runs tab, which "
    "collects the days you pick and names the connector, the account and each "
    "day before anything is spent."
)

_ARTIFACT_NOT_RETAINED = (
    "There is no retained delivery to reprocess for this Datastream: nothing "
    "arrived, or its bytes have passed their retention window. Re-import the "
    "file -- reprocessing never asks the source for it again."
)

_ARTIFACT_UNREADABLE = (
    "The retained delivery is recorded as held but its bytes could not be read "
    "back from storage. This is a storage fault, not a retention decision: "
    "retry, and re-import the file if it persists."
)


class ReprocessError(Exception):
    """A reprocess precondition failure carrying a stable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RetainedArtifact:
    """The material a reprocess would replay, named by what actually holds it.

    ``raw_import_id`` / ``quarantine_uri`` / ``content_hash`` address the retained
    delivery. ``published_relation`` is the relation the LAST successful import of
    this Datastream landed in, read from the ledger -- an observation, never a
    composed name, for the same reason the activation driver reads its landing
    rather than rebuilding it.
    """

    raw_import_id: str
    content_hash: str
    quarantine_uri: str
    filename: str | None
    published_relation: str | None
    published_ledger_id: str | None
    published_execution_id: str | None
    #: The `relation_ref` the CURRENT head of `datastream_output_versions` names
    #: -- what this reprocess will supersede. It is NOT the same fact as
    #: `published_relation`, and conflating them is the defect being repaired:
    #: `published_relation` is where the rows actually ARE (read from the import
    #: ledger), the head is what the product currently ANSWERS. In the production
    #: case they disagree -- the head is a synthetic path, the ledger names a
    #: promoted table -- which is precisely why the verb exists.
    superseded_relation: str | None = None


@dataclass
class ReprocessPlan:
    """The decision, its evidence, and -- when it refuses -- the gesture that repairs.

    ``state`` is ``available`` or one of ``REFUSAL_CODES``. ``mapping_gate`` is
    filled on both branches, because "would this have needed a human?" is a fact
    about the request and not about whether it succeeded.
    """

    state: str
    message: str | None = None
    mode: str | None = None
    datastream_id: str | None = None
    project_id: str | None = None
    org_id: str | None = None
    artifact: RetainedArtifact | None = None
    from_mapping_version_id: str | None = None
    to_mapping_version_id: str | None = None
    plan_version_id: str | None = None
    mapping_gate: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.state == STATE_AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        artifact = self.artifact
        return {
            "state": self.state,
            "message": self.message,
            "mode": self.mode,
            "datastream_id": self.datastream_id,
            "from_mapping_version_id": self.from_mapping_version_id,
            "to_mapping_version_id": self.to_mapping_version_id,
            "calls_source": False,
            "artifact": (
                {
                    "raw_import_id": artifact.raw_import_id,
                    "content_hash": artifact.content_hash,
                    "filename": artifact.filename,
                    "published_relation": artifact.published_relation,
                    "published_execution_id": artifact.published_execution_id,
                    "superseded_relation": artifact.superseded_relation,
                }
                if artifact is not None
                else None
            ),
            "mapping_gate": dict(self.mapping_gate),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def decide_mapping_gate(
    *,
    published_mapping_version_id: str | None,
    chosen_mapping_version_id: str | None,
    actor: str,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """PURE: does this reprocess owe a human gate, or is the gate explicitly skipped?

    The comparison is the only thing that decides it, and it is deliberately not
    "did anything about the Datastream change": a reprocess reapplies A MAPPING to
    retained bytes, so the mapping version is the entire surface on which the
    meaning of the republished numbers can move.

    Both branches are attributed and dated. A skip that leaves no row is
    indistinguishable, a week later, from a gate that was never reached -- and
    "nobody looked" and "somebody established there was nothing to look at" are
    different facts.
    """
    same = bool(
        published_mapping_version_id
        and chosen_mapping_version_id
        and published_mapping_version_id == chosen_mapping_version_id
    )
    return {
        "state": GATE_SKIPPED if same else GATE_PASSED,
        "reason": (
            "mapping_unchanged"
            if same
            else "mapping_changed_since_the_publication_being_repaired"
        ),
        "explanation": (
            "The mapping that published is the mapping being reapplied, so what "
            "the numbers mean does not move and no second human decision is owed."
            if same
            else "The chosen mapping differs from the one that published: this "
            "reprocess changes what the republished numbers mean, so it is "
            "confirmed by a person who read both versions."
        ),
        "from_mapping_version_id": published_mapping_version_id,
        "to_mapping_version_id": chosen_mapping_version_id,
        "decided_by": actor,
        "decided_at": decided_at or _now(),
    }


# ---------------------------------------------------------------------------
# Reading what is retained (pure reads; never a provider call, never a mutation).
# ---------------------------------------------------------------------------


def _load_datastream(conn, datastream_id: str, project_id: str | None) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, org_id, source_kind, current_plan_version_id,
                   current_mapping_version_id, current_published_execution_id
              FROM app.datastreams
             WHERE id = %s AND (%s::text IS NULL OR project_id = %s::text)
            """,
            (datastream_id, project_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "datastream_id": row[0],
        "project_id": row[1],
        "org_id": row[2],
        "mode": row[3],
        "plan_version_id": row[4],
        "mapping_version_id": row[5],
        "published_execution_id": row[6],
    }


def _last_landed_import(conn, datastream_id: str, project_id: str) -> dict[str, Any] | None:
    """The most recent import that actually LANDED, with the relation it landed in.

    ``outcome = 'published'`` and a non-NULL ``landing_relation`` together are the
    honest marker that rows of this Datastream are readable somewhere. A ``noop``
    row proves the opposite of a landing, and a row without a relation names no
    address -- neither can tell a reader where the data is.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, execution_id, landing_relation, content_hash,
                   plan_version_id, mapping_version_id, row_count
              FROM app.managed_feed_import_ledger
             WHERE datastream_id = %s AND project_id = %s
               AND outcome = 'published'
               AND landing_relation IS NOT NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "ledger_id": row[0],
        "execution_id": row[1],
        "landing_relation": row[2],
        "content_hash": row[3],
        "plan_version_id": row[4],
        "mapping_version_id": row[5],
        "row_count": row[6],
    }


def _retained_delivery(
    conn, *, datastream_id: str, content_hash: str | None
) -> dict[str, Any] | None:
    """The delivery whose bytes this deployment still holds, addressed by content.

    BY HASH FIRST, and "the newest retained one" only when no publication names a
    hash. The rule is the activation driver's (``_managed_feed_source_ref``) and
    it holds for the same reason: replaying the most recent arrival would
    reprocess a file nobody reviewed, under an audit trail naming the one they
    did.

    ``quarantine_deleted_at IS NULL`` is the retention fact. A row whose bytes
    were deleted by policy is not a candidate, and reporting it as one would send
    a person to a storage fault for a decision the product made on purpose.
    """
    with conn.cursor() as cur:
        if content_hash:
            cur.execute(
                """
                SELECT id, filename, content_hash, quarantine_uri, state
                  FROM app.inbound_raw_imports
                 WHERE datastream_id = %s AND content_hash = %s
                   AND quarantine_uri IS NOT NULL AND quarantine_deleted_at IS NULL
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (datastream_id, content_hash),
            )
            row = cur.fetchone()
            if row is not None:
                return {
                    "raw_import_id": row[0],
                    "filename": row[1],
                    "content_hash": row[2],
                    "quarantine_uri": row[3],
                    "state": row[4],
                }
        cur.execute(
            """
            SELECT id, filename, content_hash, quarantine_uri, state
              FROM app.inbound_raw_imports
             WHERE datastream_id = %s
               AND quarantine_uri IS NOT NULL AND quarantine_deleted_at IS NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (datastream_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "raw_import_id": row[0],
        "filename": row[1],
        "content_hash": row[2],
        "quarantine_uri": row[3],
        "state": row[4],
    }


def _current_head_relation(
    conn, *, datastream_id: str, project_id: str, mapping_version_id: str | None
) -> str | None:
    """The `relation_ref` the analytical read answers with today, or None.

    The SAME query `query_execution` runs -- newest version for the (mapping,
    Datastream, project) triple -- because "what will this reprocess supersede?"
    has to be answered by the reader, not by a second opinion about which row is
    the head.
    """
    if not mapping_version_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """SELECT relation_ref FROM app.datastream_output_versions
                WHERE mapping_version_id=%s AND datastream_id=%s AND project_id=%s
                ORDER BY created_at DESC LIMIT 1""",
            (mapping_version_id, datastream_id, project_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _mapping_belongs(conn, *, mapping_version_id: str, datastream_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.datastream_mapping_versions WHERE id=%s AND datastream_id=%s",
            (mapping_version_id, datastream_id),
        )
        return cur.fetchone() is not None


def _active_execution(conn, *, datastream_id: str, project_id: str) -> str | None:
    from core.datastream_publication import ACTIVE_STATES  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.datastream_executions "
            "WHERE datastream_id=%s AND project_id=%s AND state = ANY(%s) LIMIT 1",
            (datastream_id, project_id, list(ACTIVE_STATES)),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def evaluate_reprocess_plan(
    conn,
    *,
    datastream_id: str,
    project_id: str | None = None,
    chosen_mapping_version_id: str | None = None,
    actor: str = "system",
    verify_bytes: bool = True,
) -> ReprocessPlan:
    """Decide whether this Datastream can be reprocessed, and say why when it cannot.

    A pure READ. It creates nothing, enqueues nothing and calls no provider --
    which is what makes it safe to run behind a screen that has to decide whether
    to offer the verb at all.

    ``verify_bytes`` reads the retained object back and compares its digest to the
    recorded ``content_hash``. It is on by default because the alternative is
    offering a gesture that will fail at the last step, after a person has
    confirmed it; the caller may switch it off when the store is not reachable
    from its process and a later step will verify anyway.
    """
    row = _load_datastream(conn, datastream_id, project_id)
    if row is None:
        return ReprocessPlan(
            state=REFUSAL_DATASTREAM_NOT_FOUND,
            message="Datastream not found.",
            datastream_id=datastream_id,
        )

    mode = str(row["mode"] or "")
    chosen = (chosen_mapping_version_id or "").strip() or row["mapping_version_id"]
    base = {
        "mode": mode,
        "datastream_id": row["datastream_id"],
        "project_id": row["project_id"],
        "org_id": row["org_id"],
        "plan_version_id": row["plan_version_id"],
        "to_mapping_version_id": chosen,
    }

    if mode not in REPLAYABLE_MODES:
        return ReprocessPlan(
            state=REFUSAL_SOURCE_WOULD_BE_CALLED, message=_SOURCE_WOULD_BE_CALLED, **base
        )
    if chosen and not _mapping_belongs(
        conn, mapping_version_id=chosen, datastream_id=row["datastream_id"]
    ):
        return ReprocessPlan(
            state=REFUSAL_MAPPING_NOT_FOUND,
            message=(
                "The chosen mapping version does not belong to this Datastream. "
                "Pick one of its versions on the Mapping tab."
            ),
            **base,
        )

    landed = _last_landed_import(conn, row["datastream_id"], row["project_id"])
    delivery = _retained_delivery(
        conn,
        datastream_id=row["datastream_id"],
        content_hash=(landed or {}).get("content_hash"),
    )
    if delivery is None:
        return ReprocessPlan(
            state=REFUSAL_ARTIFACT_NOT_RETAINED, message=_ARTIFACT_NOT_RETAINED, **base
        )

    if verify_bytes:
        readable, why = _bytes_are_readable(
            quarantine_uri=str(delivery["quarantine_uri"]),
            content_hash=str(delivery["content_hash"] or ""),
        )
        if not readable:
            return ReprocessPlan(
                state=REFUSAL_ARTIFACT_UNREADABLE,
                message=why or _ARTIFACT_UNREADABLE,
                **base,
            )

    in_flight = _active_execution(
        conn, datastream_id=row["datastream_id"], project_id=row["project_id"]
    )
    if in_flight:
        return ReprocessPlan(
            state=REFUSAL_RUN_IN_FLIGHT,
            message=(
                "A run of this Datastream is still in flight and holds its "
                "publication lock. Wait for it to finish, or resolve it on the "
                "Runs tab, then reprocess."
            ),
            **base,
        )

    published_mapping = (landed or {}).get("mapping_version_id") or row["mapping_version_id"]
    return ReprocessPlan(  # noqa: RET504 -- the shape is the return value

        state=STATE_AVAILABLE,
        message=None,
        artifact=RetainedArtifact(
            raw_import_id=str(delivery["raw_import_id"]),
            content_hash=str(delivery["content_hash"] or ""),
            quarantine_uri=str(delivery["quarantine_uri"]),
            filename=delivery.get("filename"),
            published_relation=(landed or {}).get("landing_relation"),
            published_ledger_id=(landed or {}).get("ledger_id"),
            published_execution_id=(landed or {}).get("execution_id"),
            superseded_relation=_current_head_relation(
                conn,
                datastream_id=row["datastream_id"],
                project_id=row["project_id"],
                mapping_version_id=chosen,
            ),
        ),
        from_mapping_version_id=published_mapping,
        mapping_gate=decide_mapping_gate(
            published_mapping_version_id=published_mapping,
            chosen_mapping_version_id=chosen,
            actor=actor,
        ),
        **base,
    )


def _bytes_are_readable(*, quarantine_uri: str, content_hash: str) -> tuple[bool, str | None]:
    """Read the retained object back and prove it is the object the record names.

    Two different failures, two different sentences. Unreachable storage is a
    fault a retry may fix; a digest that no longer matches is the one case where
    continuing would be actively wrong -- the reprocess would republish bytes
    that are not the bytes the evidence describes, under an audit trail claiming
    they are. ``inbound_reprocess`` states the same rule for the same reason; this
    is the read that lets a SCREEN refuse before a person confirms, rather than
    at the last step after they did.
    """
    from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415
    from core.inbound_raw_imports import content_hash as digest_of  # noqa: PLC0415

    try:
        payload = open_quarantine_store().get(quarantine_uri)
    except Exception as exc:  # noqa: BLE001 -- a storage fault, named as one
        logger.warning("reprocess: retained object unreadable uri=%s: %s", quarantine_uri, exc)
        return False, _ARTIFACT_UNREADABLE
    if content_hash and digest_of(payload) != content_hash:
        return False, (
            "The retained delivery no longer matches the content hash recorded "
            "when it arrived, so reprocessing it would republish different bytes "
            "than the evidence describes. Re-import the file."
        )
    return True, None


# ---------------------------------------------------------------------------
# The act. Runs INSIDE `operations.execute_operation` -- one durable operation.
# ---------------------------------------------------------------------------


def dispatch_reprocess(
    conn,
    *,
    operation_id: str,
    org_id: str,
    project_id: str,
    datastream_id: str,
    actor: str,
    reason: str | None,
    chosen_mapping_version_id: str | None,
    trace_id: str | None = None,
):
    """Replay the retained artifact and publish a NEW output version. ONE act.

    Returns a ``MutationResult``, so a refusal is still part of the durable
    record: a person asked for this, and "we refused, here is why" is an outcome
    rather than an absence.

    The replay body is ``inbound_reprocess._replay_one`` -- the SAME body a
    file-level reprocess uses. That is deliberate and it is the module's own
    rule: << a scope is not a second engine any more than a reprocess is >>. If
    the Datastream-level verb went down its own path, the two would drift on
    exactly the things this repository keeps finding drifted -- the scan that
    runs again, the channel that is read rather than assumed, the reason that
    survives.
    """
    from core.operations import MutationResult, _canonical_hash  # noqa: PLC0415

    plan = evaluate_reprocess_plan(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        chosen_mapping_version_id=chosen_mapping_version_id,
        actor=actor,
    )
    if not plan.available or plan.artifact is None:
        logger.warning(
            "reprocess: refused ds=%s state=%s", datastream_id, plan.state
        )
        refused = {"kind": "reprocess", "reason": plan.state, **plan.as_dict()}
        return MutationResult(
            outcome="failed",
            before_hash=None,
            after_hash=None,
            result=refused,
            outbox_payload={
                "event": "datastream.reprocess.refused",
                "datastream_id": datastream_id,
                "reason": plan.state,
            },
        )

    replayed = replay_retained_artifact(
        conn,
        plan=plan,
        operation_id=operation_id,
        actor=actor,
        reason=reason or "reprocess",
        trace_id=trace_id,
    )

    output_version_id = None
    if replayed.get("status") == "landed":
        output_version_id = record_reprocessed_output_version(
            conn,
            org_id=org_id,
            plan=plan,
            execution_id=replayed.get("execution_id"),
            relation=replayed.get("relation"),
            operation_id=operation_id,
            actor=actor,
        )

    result = {
        "kind": "reprocess",
        "datastream_id": datastream_id,
        "project_id": plan.project_id,
        "origin": "bounded_reprocess",
        "calls_source": False,
        "raw_import_id": plan.artifact.raw_import_id,
        "superseded_output_relation": plan.artifact.superseded_relation,
        "mapping_gate": plan.mapping_gate,
        "from_mapping_version_id": plan.from_mapping_version_id,
        "to_mapping_version_id": plan.to_mapping_version_id,
        "execution_id": replayed.get("execution_id"),
        "import_ledger_id": replayed.get("import_ledger_id"),
        "relation_ref": replayed.get("relation"),
        "output_version_id": output_version_id,
        "status": replayed.get("status"),
        "error_code": replayed.get("error_code"),
        "reason": reason or None,
    }
    succeeded = replayed.get("status") == "landed" and bool(output_version_id)
    logger.info(
        "reprocess: ds=%s status=%s execution=%s relation=%s output_version=%s gate=%s",
        datastream_id,
        replayed.get("status"),
        replayed.get("execution_id"),
        replayed.get("relation"),
        output_version_id,
        plan.mapping_gate.get("state"),
    )
    return MutationResult(
        outcome="succeeded" if succeeded else "failed",
        before_hash=None,
        after_hash=_canonical_hash(result) if succeeded else None,
        result=result,
        outbox_payload={
            "event": "datastream.reprocess.replayed",
            "datastream_id": datastream_id,
            "execution_id": replayed.get("execution_id"),
            "output_version_id": output_version_id,
        },
    )


def replay_retained_artifact(
    conn,
    *,
    plan: ReprocessPlan,
    operation_id: str,
    actor: str,
    reason: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Drive the existing file replay, and observe where it landed.

    ``message_id`` is DERIVED from the durable operation id, which is what makes a
    retried confirm replay the original import instead of minting a second
    execution for one confirmed act -- the two-level idempotency
    ``inbound_reprocess`` already documents.

    The relation is READ from the ledger row the replay wrote, never rebuilt. A
    rebuilt name drifts the day the naming rule moves on one side only; an
    observation cannot.
    """
    from core.inbound_reprocess import _replay_one  # noqa: PLC0415

    artifact = plan.artifact
    if artifact is None:  # pragma: no cover -- guarded by the caller
        raise ReprocessError(REFUSAL_ARTIFACT_NOT_RETAINED, _ARTIFACT_NOT_RETAINED)

    raw = _raw_import_row(conn, artifact.raw_import_id)
    replay = _replay_one(
        conn,
        raw_import_id=artifact.raw_import_id,
        datastream_id=str(plan.datastream_id),
        project_id=str(plan.project_id),
        raw=raw,
        quarantine_uri=artifact.quarantine_uri,
        # One act, one message id. `reprocess:<operation>` cannot collide with a
        # transport's own message id, and it is stable across a durable replay.
        message_id=f"reprocess:{operation_id}",
        actor=actor,
        reason=reason,
        # The mapping the proposal pinned. `None` would mean "whatever is in
        # force at this instant", and a governed act does not resolve its own
        # inputs after a person confirmed different ones.
        target_mapping_version_id=plan.to_mapping_version_id,
        store=None,
        trace_id=trace_id,
        # WHY the execution this replay mints exists. Stamped through the import
        # path rather than written here, because `stamp_origin` is the only
        # function that validates the key and refuses a verb with no engine.
        run_origin="bounded_reprocess",
    )
    result = replay.get("result") or {}
    ledger_id = result.get("import_ledger_id")
    landed = _ledger_landing(conn, ledger_id, str(plan.project_id)) if ledger_id else {}
    return {
        "status": result.get("status"),
        "error_code": result.get("error_code"),
        "import_ledger_id": ledger_id,
        "execution_id": landed.get("execution_id"),
        "relation": landed.get("landing_relation"),
        "row_count": landed.get("row_count"),
    }


def _raw_import_row(conn, raw_import_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT filename, media_type_declared FROM app.inbound_raw_imports WHERE id=%s",
            (raw_import_id,),
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover -- read one statement after it was found
        raise ReprocessError(REFUSAL_ARTIFACT_NOT_RETAINED, _ARTIFACT_NOT_RETAINED)
    return {"filename": row[0], "media_type_declared": row[1]}


def _ledger_landing(conn, ledger_id: str, project_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT execution_id, landing_relation, row_count, outcome "
            "FROM app.managed_feed_import_ledger WHERE id=%s AND project_id=%s",
            (ledger_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    return {
        "execution_id": row[0],
        "landing_relation": row[1],
        "row_count": row[2],
        "outcome": row[3],
    }


def record_reprocessed_output_version(
    conn,
    *,
    org_id: str,
    plan: ReprocessPlan,
    execution_id: str | None,
    relation: str | None,
    operation_id: str,
    actor: str,
) -> str | None:
    """Append the output version that names where the reprocessed rows can be READ.

    THE POINT OF THE WHOLE VERB, and the half nothing covered. A managed-feed run
    has no output-version writer at all: ``record_run_output_version`` resolves
    its address from a CONNECTOR manifest (``stage_relation_resolver``), and a
    managed feed has no connector, so it records nothing and says so in the log.
    The wizard's activation wrote the only row such a Datastream ever gets, and
    when its candidate landed nothing under its own execution that row's
    ``relation_ref`` is the synthetic ``execution/<id>/candidate/relation`` --
    which ``query_execution._safe_identifier`` refuses, for ever.

    Appending is the ONLY repair available, and it is also the right one. The
    table is immutable by trigger (migration 138) and that trigger is correct:
    evidence of what was published is not rewritten. ``query_execution`` reads
    ``ORDER BY created_at DESC LIMIT 1`` per (datastream, mapping version), so the
    new row becomes the head and the wrong one is superseded -- present, dated,
    and no longer answered.

    ``created_at`` IS WRITTEN, NOT DEFAULTED, and it is `clock_timestamp()`. The
    column's default is ``NOW()``, which in Postgres is
    ``transaction_timestamp()`` -- one instant for the whole transaction. The head
    is decided by ``ORDER BY created_at DESC``, so two versions written in ONE
    transaction TIE and the head becomes whichever row the planner returns first:
    measured here, a reprocess that appended the right row still answered the
    wrong one. It is the same defect migration 223 fixed on the step spans, for
    the same reason, and it is not hypothetical for this verb -- an execution and
    its output version can be written in one transaction.

    Returns the new version id, or ``None`` when there is nothing true to record.
    """
    from ulid import ULID  # noqa: PLC0415

    if not execution_id or not relation:
        logger.info(
            "reprocess: output_version_not_recorded ds=%s reason=%s",
            plan.datastream_id,
            "no execution" if not execution_id else "no observed relation",
        )
        return None

    with conn.cursor() as cur:
        cur.execute(
            """SELECT id FROM app.datastream_outputs
                WHERE project_id=%s AND datastream_id=%s
                ORDER BY created_at LIMIT 1""",
            (plan.project_id, plan.datastream_id),
        )
        output = cur.fetchone()
        if output is None:
            # Never invented here. `app.datastream_outputs` is the Datastream's
            # published identity and it is minted at activation; a reprocess that
            # created one would be naming an Output nobody published.
            logger.info(
                "reprocess: output_version_not_recorded ds=%s reason=no published Output",
                plan.datastream_id,
            )
            return None
        output_id = output[0]

        cur.execute(
            "SELECT plan_version_id, mapping_version_id FROM app.datastream_executions "
            "WHERE id=%s AND project_id=%s",
            (execution_id, plan.project_id),
        )
        versions = cur.fetchone()
    plan_version_id = (versions[0] if versions else None) or plan.plan_version_id
    mapping_version_id = (versions[1] if versions else None) or plan.to_mapping_version_id

    version_id = f"dsov_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_output_versions
               (id,output_id,org_id,project_id,datastream_id,execution_id,
                plan_version_id,mapping_version_id,relation_ref,
                grain_evidence,evidence,created_by,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,
                       clock_timestamp())
               ON CONFLICT (output_id,execution_id) DO NOTHING
               RETURNING id""",
            (
                version_id,
                output_id,
                org_id,
                plan.project_id,
                plan.datastream_id,
                execution_id,
                plan_version_id,
                mapping_version_id,
                relation,
                # Both evidence columns are NOT NULL with a CHECK that demands a
                # JSON OBJECT (`app.safe_preconfiguration_evidence`, migration
                # 134): a bare array is refused by the database, and so is the
                # column's own `'[]'` DEFAULT.
                json.dumps({"stage": "published", "relation": relation}, sort_keys=True),
                json.dumps(
                    {
                        "recorded_by": "reprocess",
                        "origin": "bounded_reprocess",
                        "operation_id": operation_id,
                        "supersedes_relation": plan.artifact.superseded_relation
                        if plan.artifact
                        else None,
                        "mapping_gate": plan.mapping_gate.get("state"),
                        "mapping_gate_decided_at": plan.mapping_gate.get("decided_at"),
                    },
                    sort_keys=True,
                ),
                actor,
            ),
        )
        inserted = cur.fetchone()
    if inserted is None:
        logger.info(
            "reprocess: output_version_already_recorded ds=%s execution=%s",
            plan.datastream_id,
            execution_id,
        )
        return None
    logger.info(
        "reprocess: output_version_recorded ds=%s relation=%s supersedes=%s",
        plan.datastream_id,
        relation,
        plan.artifact.superseded_relation if plan.artifact else None,
    )
    return str(inserted[0])
