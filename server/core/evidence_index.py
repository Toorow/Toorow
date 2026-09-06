"""The reference-only Evidence index (Story 49.5).

Governance is not a second writer and this module does not make it one. Nothing
here writes, mutates or deletes a Data Run, a mapping, a publication, a Render,
an AI Path, an Evaluation Run, a governed version, an approval or an audit
event. It registers *references* to them — identity, scope, owner, time,
integrity and typed edges — and resolves everything a screen displays through
the owner, at read time, after the owner has authorized the caller.

Three properties the rest of the file exists to hold:

1. **An Evidence Record is immutable.** Registering the same owner artifact
   twice is a no-op. Registering a *different* payload under the same source
   identity is refused (:class:`EvidenceConflict`) rather than merged: silently
   updating an indexed fact would make the index unfalsifiable.
2. **A correlation is not an edge.** Two records sharing an ``operation_id`` are
   not thereby part of one trace. A trace traverses ``app.evidence_links`` and
   nothing else, so a shared identifier, an equal timestamp or a similar label
   can never merge two anchors.
3. **Unavailable is not empty, and a partial count is not a total.** An adapter
   that cannot prove complete visible coverage returns ``None`` as its total and
   degrades its coverage state. A zero here would be a measurement nobody took.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence

from ulid import ULID

logger = logging.getLogger(__name__)

#: Hard ceiling on one collection page. The useful default is smaller: a bound
#: is a refusal to disclose more, not a target to fill.
MAX_PAGE = 200
DEFAULT_PAGE = 50
#: Hard ceiling on one trace traversal. A graph that hits it reports `partial`.
MAX_GRAPH_NODES = 120
#: Hard ceiling on one backfill pass, per producer.
MAX_BACKFILL_BATCH = 500

RECORD_KINDS = ("evidence_trace", "object_version", "audit_event")
LENS_RECORD_KIND = {
    "lineage-provenance": "evidence_trace",
    "versions-approvals": "object_version",
    "audit-activity": "audit_event",
}

CORRELATION_KINDS = (
    "w3c_trace",
    "operation",
    "pull",
    "virtual_pull",
    "execution",
    "publication",
    "result",
    "render",
    "ai_path",
    "evaluation_run",
    "confirmation",
    "audit",
)

OWNER_WORKSPACES = ("data", "governance", "analyze", "context-hub", "test")

RELATIONS = (
    "derives_from",
    "supersedes",
    "published_by",
    "approved_by",
    "produced",
    "consumed_by",
    "anchored_by",
    "references",
)


class EvidenceIndexError(RuntimeError):
    """The index could not answer, and will not guess."""


class EvidenceConflict(EvidenceIndexError):
    """A different payload arrived under an already-indexed source identity."""


class EvidenceCursorInvalid(EvidenceIndexError):
    """A cursor that is malformed, or scoped to another Project/lens/filter."""


class EvidenceFilterInvalid(EvidenceIndexError):
    """A filter outside the allowlist, or outside its own value domain."""


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _mint(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _fetch(conn: Any, query: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        if cur.description is None:
            return []
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _sha_or_none(value: Any) -> str | None:
    """Accept an owner's integrity hash only in the shape the column allows.

    An owner that stores a short digest, a base64 checksum or an ETag is not
    lying — it simply is not the 64-hex content hash this column contracts. It
    becomes NULL rather than a value that fails a CHECK at insert time and takes
    a whole backfill batch down with it.
    """
    text = str(value or "").strip().lower()
    if len(text) != 64:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def is_w3c_trace_id(value: Any) -> bool:
    """W3C Trace Context: 16 bytes as 32 lowercase hex, all-zero invalid.

    Validated because the source *claims* the format. Nothing is derived FROM
    the value: a trace id stays an opaque correlation, never a clock and never a
    tenant discriminator.
    """
    text = str(value or "").strip()
    if len(text) != 32 or text == "0" * 32:
        return False
    return all(character in "0123456789abcdef" for character in text)


# ---------------------------------------------------------------------------
# The normalized reference event — the ONLY thing the projector accepts.
#
# It is deliberately not a provider payload, not a raw row and not a free-form
# metadata blob. A projector that accepts arbitrary JSON becomes the generic
# event store the architecture document names as a failure mode.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkSpec:
    """One typed edge. Either to another indexed record, or to an exact owner."""

    relation: str
    to_source_identity_key: str | None = None
    to_owner_workspace: str | None = None
    to_owner_object_type: str | None = None
    to_owner_object_id: str | None = None
    to_owner_version_id: str | None = None
    ordinal: int | None = None
    integrity_hash: str | None = None

    def __post_init__(self) -> None:
        if self.relation not in RELATIONS:
            raise EvidenceIndexError(f"unregistered relation: {self.relation!r}")
        internal = self.to_source_identity_key is not None
        external = self.to_owner_object_id is not None
        if internal == external:
            raise EvidenceIndexError("a link has exactly one destination")
        if external and (
            self.to_owner_workspace not in OWNER_WORKSPACES or not self.to_owner_object_type
        ):
            raise EvidenceIndexError("an external link needs an exact owner workspace and type")


@dataclass(frozen=True, slots=True)
class ReferenceEvent:
    """What a producer publishes. Closed by construction."""

    producer: str
    record_kind: str
    owner_workspace: str
    owner_object_type: str
    owner_object_id: str
    occurred_at: Any
    source_identity_key: str
    owner_version_id: str | None = None
    is_anchor: bool = False
    observed_at: Any = None
    integrity_hash: str | None = None
    redaction_class: str = "reference_only"
    correlations: tuple[tuple[str, str], ...] = ()
    links: tuple[LinkSpec, ...] = ()

    def __post_init__(self) -> None:
        if self.record_kind not in RECORD_KINDS:
            raise EvidenceIndexError(f"unregistered record kind: {self.record_kind!r}")
        if self.owner_workspace not in OWNER_WORKSPACES:
            raise EvidenceIndexError(f"unregistered owner workspace: {self.owner_workspace!r}")
        if self.is_anchor and self.record_kind != "evidence_trace":
            raise EvidenceIndexError("only an Evidence Trace record can anchor a trace")
        for kind, _ in self.correlations:
            if kind not in CORRELATION_KINDS:
                raise EvidenceIndexError(f"unregistered correlation kind: {kind!r}")

    @property
    def payload_hash(self) -> str:
        """The identity of what was claimed, not of when it was indexed.

        `indexed_at` is deliberately absent: a replay one hour later describes
        the same fact and must be a no-op, not a conflict.
        """
        return _hash(
            {
                "producer": self.producer,
                "record_kind": self.record_kind,
                "owner": [
                    self.owner_workspace,
                    self.owner_object_type,
                    self.owner_object_id,
                    self.owner_version_id,
                ],
                "is_anchor": self.is_anchor,
                "occurred_at": _iso(self.occurred_at),
                "observed_at": _iso(self.observed_at),
                "integrity_hash": _sha_or_none(self.integrity_hash),
                "redaction_class": self.redaction_class,
                "correlations": sorted(self.correlations),
                "links": sorted(
                    [
                        [
                            link.relation,
                            link.to_source_identity_key,
                            link.to_owner_workspace,
                            link.to_owner_object_type,
                            link.to_owner_object_id,
                            link.to_owner_version_id,
                            link.ordinal,
                        ]
                        for link in self.links
                    ],
                    key=canonical_json,
                ),
            }
        )


# ---------------------------------------------------------------------------
# The producer registry.
#
# Every currently applicable producer appears below EXACTLY ONCE, in one of
# three dispositions: indexed, future-owner-unavailable, or explicitly excluded
# with a reason. A hidden hardcoded subset is what the previous Evidence surface
# had, and it is why three unrelated tables could look like a complete index.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProducerContract:
    producer: str
    record_kind: str
    owner_workspace: str
    owner_object_type: str
    label: str
    #: `indexed` | `future_owner` | `excluded`
    disposition: str = "indexed"
    creates_anchor: bool = False
    correlation_kinds: tuple[str, ...] = ()
    redaction_class: str = "reference_only"
    #: Deterministic, bounded, Project-scoped. Ordered so a watermark is stable.
    backfill_sql: str | None = None
    row_to_event: Callable[[Mapping[str, Any]], ReferenceEvent] | None = None
    #: Why this producer cannot be indexed. Required unless `disposition` is
    #: `indexed`; it is what the coverage report says out loud.
    reason_code: str | None = None
    reason: str | None = None

    @property
    def is_indexed(self) -> bool:
        return self.disposition == "indexed"


def _identity(*parts: Any) -> str:
    """A producer's natural identity, flattened deterministically."""
    return "|".join("" if part is None else str(part) for part in parts)


# --- Data: executions, stages, phases, publications ------------------------

_DATA_EXECUTIONS_SQL = """
    SELECT e.id, e.datastream_id, e.project_id, e.plan_version_id, e.mapping_version_id,
           e.state, e.content_hash, e.created_at, e.updated_at, e.state_changed_at
      FROM app.datastream_executions e
     WHERE e.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR e.id > %(cursor)s::text)
     ORDER BY e.id
     LIMIT %(limit)s
"""


def _data_execution_event(row: Mapping[str, Any]) -> ReferenceEvent:
    execution_id = str(row["id"])
    datastream_id = str(row["datastream_id"])
    return ReferenceEvent(
        producer="data_execution",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-execution",
        owner_object_id=execution_id,
        owner_version_id=None,
        is_anchor=True,
        occurred_at=row.get("created_at"),
        observed_at=row.get("state_changed_at") or row.get("updated_at"),
        integrity_hash=row.get("content_hash"),
        source_identity_key=_identity("data_execution", execution_id),
        correlations=(("execution", execution_id),),
        links=(
            LinkSpec(
                relation="references",
                to_owner_workspace="data",
                to_owner_object_type="datastream",
                to_owner_object_id=datastream_id,
            ),
            LinkSpec(
                relation="derives_from",
                to_owner_workspace="data",
                to_owner_object_type="datastream-mapping-version",
                to_owner_object_id=datastream_id,
                to_owner_version_id=str(row["mapping_version_id"]),
            ),
            LinkSpec(
                relation="derives_from",
                to_owner_workspace="data",
                to_owner_object_type="datastream-plan-version",
                to_owner_object_id=datastream_id,
                to_owner_version_id=str(row["plan_version_id"]),
            ),
        ),
    )


#: The stage sequence is DECLARED by the owner's CHECK constraint, so its order
#: is proven and may become an ordinal. A timestamp ordering would not be.
_STAGE_ORDINAL = {"collected": 0, "mapped": 1, "processed": 2, "published": 3}

_DATA_STAGE_SQL = """
    SELECT s.id, s.project_id, s.datastream_id, s.execution_id, s.stage, s.phase_state,
           s.occurred_at, s.plan_version_id, s.mapping_version_id, s.schema_hash,
           s.artifact_ref, s.materialization_ref
      FROM app.datastream_execution_stage_evidence s
     WHERE s.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR s.id > %(cursor)s::text)
     ORDER BY s.id
     LIMIT %(limit)s
"""


def _data_stage_event(row: Mapping[str, Any]) -> ReferenceEvent:
    stage_id = str(row["id"])
    execution_id = str(row["execution_id"])
    stage = str(row["stage"])
    return ReferenceEvent(
        producer="data_execution_stage",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-execution-stage",
        owner_object_id=stage_id,
        occurred_at=row.get("occurred_at"),
        integrity_hash=row.get("schema_hash"),
        source_identity_key=_identity("data_execution_stage", stage_id),
        correlations=(("execution", execution_id),),
        links=(
            LinkSpec(
                relation="anchored_by",
                to_source_identity_key=_identity("data_execution", execution_id),
                ordinal=_STAGE_ORDINAL.get(stage),
            ),
        ),
    )


_DATA_PHASE_SQL = """
    SELECT p.id, p.project_id, p.datastream_id, p.execution_id, p.phase, p.phase_state,
           p.occurred_at, p.trace_ref, p.artifact_ref
      FROM app.datastream_execution_phase_evidence p
     WHERE p.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR p.id > %(cursor)s::text)
     ORDER BY p.id
     LIMIT %(limit)s
"""


def _data_phase_event(row: Mapping[str, Any]) -> ReferenceEvent:
    phase_id = str(row["id"])
    execution_id = str(row["execution_id"])
    correlations: list[tuple[str, str]] = [("execution", execution_id)]
    trace_ref = str(row.get("trace_ref") or "")
    if is_w3c_trace_id(trace_ref):
        correlations.append(("w3c_trace", trace_ref))
    return ReferenceEvent(
        producer="data_execution_phase",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-execution-phase",
        owner_object_id=phase_id,
        occurred_at=row.get("occurred_at"),
        source_identity_key=_identity("data_execution_phase", phase_id),
        correlations=tuple(correlations),
        links=(
            LinkSpec(
                relation="anchored_by",
                to_source_identity_key=_identity("data_execution", execution_id),
            ),
        ),
    )


_DATA_PUBLICATION_SQL = """
    SELECT l.id, l.project_id, l.datastream_id, l.execution_id, l.plan_version_id,
           l.mapping_version_id, l.content_hash, l.prior_execution_id,
           l.published_at, l.published_by
      FROM app.datastream_publication_log l
     WHERE l.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR l.id > %(cursor)s::text)
     ORDER BY l.id
     LIMIT %(limit)s
"""


def _data_publication_event(row: Mapping[str, Any]) -> ReferenceEvent:
    publication_id = str(row["id"])
    execution_id = str(row["execution_id"])
    datastream_id = str(row["datastream_id"])
    links = [
        LinkSpec(
            relation="derives_from",
            to_source_identity_key=_identity("data_execution", execution_id),
        ),
        LinkSpec(
            relation="references",
            to_owner_workspace="data",
            to_owner_object_type="datastream",
            to_owner_object_id=datastream_id,
        ),
    ]
    prior = row.get("prior_execution_id")
    if prior:
        # The owner NAMED the pre-swap execution. That is a pinned predecessor,
        # not a search for a plausible one.
        links.append(
            LinkSpec(
                relation="supersedes",
                to_source_identity_key=_identity("data_execution", str(prior)),
            )
        )
    return ReferenceEvent(
        producer="data_publication",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-publication",
        owner_object_id=publication_id,
        is_anchor=True,
        occurred_at=row.get("published_at"),
        integrity_hash=row.get("content_hash"),
        source_identity_key=_identity("data_publication", publication_id),
        correlations=(("publication", publication_id), ("execution", execution_id)),
        links=tuple(links),
    )


_DATA_MAPPING_PUBLICATION_SQL = """
    SELECT pl.id, pl.project_id, pl.datastream_id, pl.mapping_version_id,
           pl.prior_mapping_version_id, pl.command, pl.published_by, pl.created_at
      FROM app.datastream_mapping_publication_log pl
     WHERE pl.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR pl.id > %(cursor)s::text)
     ORDER BY pl.id
     LIMIT %(limit)s
"""


def _data_mapping_publication_event(row: Mapping[str, Any]) -> ReferenceEvent:
    """The Story 49.1 placeholder, migrated as what it actually is.

    It used to BE the Evidence Trace. It is one proven node: "this mapping
    version replaced that one, by this command". Its own workbench reports
    `partial` coverage, because the pull that fed it and the Result that used it
    are not proven by this row and are not invented from it.
    """
    entry_id = str(row["id"])
    datastream_id = str(row["datastream_id"])
    links = [
        LinkSpec(
            relation="references",
            to_owner_workspace="data",
            to_owner_object_type="datastream",
            to_owner_object_id=datastream_id,
        ),
        LinkSpec(
            relation="produced",
            to_owner_workspace="data",
            to_owner_object_type="datastream-mapping-version",
            to_owner_object_id=datastream_id,
            to_owner_version_id=str(row["mapping_version_id"]),
        ),
    ]
    prior = row.get("prior_mapping_version_id")
    if prior:
        links.append(
            LinkSpec(
                relation="supersedes",
                to_owner_workspace="data",
                to_owner_object_type="datastream-mapping-version",
                to_owner_object_id=datastream_id,
                to_owner_version_id=str(prior),
            )
        )
    return ReferenceEvent(
        producer="data_mapping_publication",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-mapping-publication",
        owner_object_id=entry_id,
        is_anchor=True,
        occurred_at=row.get("created_at"),
        source_identity_key=_identity("data_mapping_publication", entry_id),
        links=tuple(links),
    )


# --- Object versions --------------------------------------------------------


def _version_producer(
    producer: str,
    *,
    label: str,
    owner_workspace: str,
    owner_object_type: str,
    table: str,
    object_column: str,
    sql: str,
    correlation_kinds: tuple[str, ...] = (),
) -> ProducerContract:
    """A governed version producer, built from the one shape they all share.

    Every one of them keys on ``(object, version_number)`` under a UNIQUE
    constraint the owner declares, which is why ``supersedes`` can pin
    ``version_number - 1`` EXACTLY. That is the owner's own ordering, not a
    guess: where the row is absent there is no link, and Diff says so.
    """

    def _event(row: Mapping[str, Any]) -> ReferenceEvent:
        version_id = str(row["id"])
        object_id = str(row[object_column])
        links: list[LinkSpec] = []
        predecessor = row.get("predecessor_version_id")
        if predecessor:
            links.append(
                LinkSpec(
                    relation="supersedes",
                    to_source_identity_key=_identity(producer, str(predecessor)),
                )
            )
        correlations: list[tuple[str, str]] = []
        confirmation = row.get("confirmation_id")
        if confirmation:
            correlations.append(("confirmation", str(confirmation)))
            links.append(
                LinkSpec(
                    relation="approved_by",
                    to_owner_workspace=owner_workspace,
                    to_owner_object_type=f"{owner_object_type}-approval",
                    to_owner_object_id=str(confirmation),
                )
            )
        operation = row.get("operation_id")
        if operation:
            correlations.append(("operation", str(operation)))
        return ReferenceEvent(
            producer=producer,
            record_kind="object_version",
            owner_workspace=owner_workspace,
            owner_object_type=owner_object_type,
            owner_object_id=object_id,
            owner_version_id=version_id,
            occurred_at=row.get("created_at"),
            observed_at=row.get("published_at") or row.get("created_at"),
            integrity_hash=row.get("content_hash"),
            source_identity_key=_identity(producer, version_id),
            correlations=tuple(correlations),
            links=tuple(links),
        )

    return ProducerContract(
        producer=producer,
        record_kind="object_version",
        owner_workspace=owner_workspace,
        owner_object_type=owner_object_type,
        label=f"{label} ({table})",
        correlation_kinds=correlation_kinds,
        backfill_sql=sql,
        row_to_event=_event,
    )


def _sequenced_version_sql(
    table: str, object_column: str, *, extra_columns: str = "", extra_join: str = ""
) -> str:
    """Select one version plus the EXACT predecessor its owner's own sequence names."""
    return f"""
    SELECT v.id, v.{object_column}, v.version_number, v.status, v.content_hash,
           v.created_at, {extra_columns} prior.id AS predecessor_version_id
      FROM app.{table} v
      LEFT JOIN app.{table} prior
             ON prior.project_id = v.project_id
            AND prior.{object_column} = v.{object_column}
            AND prior.version_number = v.version_number - 1
      {extra_join}
     WHERE v.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR v.id > %(cursor)s::text)
     ORDER BY v.id
     LIMIT %(limit)s
    """


_DATA_MAPPING_VERSION_SQL = """
    SELECT v.id, v.datastream_id, v.version_number, v.content_hash, v.created_at,
           NULL::timestamptz AS published_at,
           prior.id AS predecessor_version_id,
           c.id AS confirmation_id, c.operation_id
      FROM app.datastream_mapping_versions v
      LEFT JOIN app.datastream_mapping_versions prior
             ON prior.project_id = v.project_id
            AND prior.datastream_id = v.datastream_id
            AND prior.version_number = v.version_number - 1
      LEFT JOIN LATERAL (
            SELECT pc.id, pc.operation_id
              FROM app.publication_confirmations pc
             WHERE pc.project_id = v.project_id
               AND pc.mapping_version_id = v.id
             ORDER BY pc.created_at DESC, pc.id
             LIMIT 1
      ) c ON TRUE
     WHERE v.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR v.id > %(cursor)s::text)
     ORDER BY v.id
     LIMIT %(limit)s
"""

_RULE_SET_VERSION_SQL = """
    SELECT v.id, v.rule_set_id, v.version_number, v.status, v.content_hash, v.created_at,
           NULL::timestamptz AS published_at,
           prior.id AS predecessor_version_id,
           a.confirmation_id, a.operation_id
      FROM app.governance_rule_set_versions v
      LEFT JOIN app.governance_rule_set_versions prior
             ON prior.project_id = v.project_id
            AND prior.rule_set_id = v.rule_set_id
            AND prior.version_number = v.version_number - 1
      LEFT JOIN LATERAL (
            SELECT ra.confirmation_id, ra.operation_id
              FROM app.rule_set_approvals ra
             WHERE ra.project_id = v.project_id
               AND ra.rule_set_version_id = v.id
             ORDER BY ra.decided_at DESC, ra.id
             LIMIT 1
      ) a ON TRUE
     WHERE v.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR v.id > %(cursor)s::text)
     ORDER BY v.id
     LIMIT %(limit)s
"""

#: Story 60.5. Written out rather than built by `_sequenced_version_sql` for one
#: measured reason: a client value mapping table may be ORG-scoped, so its version
#: rows carry `project_id IS NULL` (migration 235 gives the same shape to the
#: table itself). The generic helper filters on `v.project_id = %(project_id)s`
#: and would make the history of every ORG table invisible from every Project --
#: which is not a bound, it is a disappearance. The join resolves the querying
#: Project's organization instead, so an org-wide table's history is readable from
#: the Projects it actually serves.
_VALUE_MAPPING_TABLE_VERSION_SQL = """
    SELECT v.id, v.table_id, v.version_number, v.status, v.content_hash, v.created_at,
           NULL::timestamptz AS published_at,
           v.predecessor_version_id
      FROM app.value_mapping_table_versions v
      JOIN app.projects p ON p.id = %(project_id)s
     WHERE (v.project_id = %(project_id)s
            OR (v.project_id IS NULL AND v.org_id = p.org_id))
       AND (%(cursor)s::text IS NULL OR v.id > %(cursor)s::text)
     ORDER BY v.id
     LIMIT %(limit)s
"""

_PROJECT_CONFIGURATION_VERSION_SQL = """
    SELECT v.id, v.project_id AS configuration_object_id, v.version_number,
           v.content_hash, v.created_at, NULL::timestamptz AS published_at,
           v.previous_version_id AS predecessor_version_id
      FROM app.project_configuration_versions v
     WHERE v.project_id = %(project_id)s
       AND (%(cursor)s::text IS NULL OR v.id > %(cursor)s::text)
     ORDER BY v.id
     LIMIT %(limit)s
"""


# --- Context Hub: AI Paths --------------------------------------------------
#
# The owner landed in Story 49.6: `app.ai_paths` and `app.ai_path_steps`
# (migration 150), written only by `server/core/ai_paths.py`. It records what an
# execution DID -- observed calls, identifiers, versions, order and outcomes --
# and nothing about model reasoning.
#
# `app.context_path_resolutions` is still NOT this owner and is still not wired
# up as one. It answers which path a question SHOULD take; an AI Path answers
# which path an execution DID take. Those are different claims and the exclusion
# below is deliberate.
#
# ONLY FINALIZED PATHS ARE INDEXED. A `recording` path is still being appended
# to, so its content hash is not yet frozen; indexing it would register an
# identity whose payload changes underneath the record -- the one thing the
# conflict rule exists to refuse.
#
# THE CONTEXT HUB OWNER ROUTE IS NOW REGISTERED, and the sentence this block
# carried until Story 49.6 lot 4 is gone with it. It read: "49.6 has registered
# the `ai-path` route type but mounted no screen for it" -- true when it was
# written, and quoted as a fact by three other files. The screen exists:
# `objectSurfaces.tsx` serves `AiPathPage` for `{ workspace: "context-hub",
# objectType: "ai-path" }`, `navigation/contextHub.ts` declares the type under
# Knowledge Graph, and the workbench now shows the ordered steps with their
# exact owner links. So `owner_route("ai-path", ...)` resolves and an AI Path
# node in the Evidence graph offers the link it always had the identity for --
# gate item 9 of Story 49.5 asks for stable IDs AND exact owner routes, and the
# second half was the only one missing.
#
# `ai-path-step` stays UNREGISTERED, and that is not an oversight: a step has no
# screen of its own. It is read inside its path, and the path is the route. A
# link to an address nobody serves is the "route without a door" this table
# exists to refuse.

_CONTEXT_AI_PATHS_SQL = """
    SELECT p.id, p.project_id, p.lifecycle, p.outcome, p.actor,
           p.execution_correlation, p.started_at, p.ended_at,
           p.model_ref, p.tool_catalog_version, p.content_hash
      FROM app.ai_paths p
     WHERE p.project_id = %(project_id)s
       AND p.lifecycle = 'finalized'
       AND (%(cursor)s::text IS NULL OR p.id > %(cursor)s::text)
     ORDER BY p.id
     LIMIT %(limit)s
"""


def _context_ai_path_event(row: Mapping[str, Any]) -> ReferenceEvent:
    path_id = str(row["id"])
    correlations: list[tuple[str, str]] = [("ai_path", path_id)]
    correlation = str(row.get("execution_correlation") or "")
    if is_w3c_trace_id(correlation):
        correlations.append(("w3c_trace", correlation))
    return ReferenceEvent(
        producer="context_ai_path",
        record_kind="evidence_trace",
        owner_workspace="context-hub",
        owner_object_type="ai-path",
        owner_object_id=path_id,
        is_anchor=True,
        occurred_at=row.get("started_at"),
        observed_at=row.get("ended_at") or row.get("started_at"),
        integrity_hash=row.get("content_hash"),
        source_identity_key=_identity("context_ai_path", path_id),
        correlations=tuple(correlations),
    )


_CONTEXT_AI_PATH_STEPS_SQL = """
    SELECT s.id, s.path_id, s.project_id, s.ordinal, s.observed_at, s.step_kind,
           s.owner_workspace, s.owner_object_type, s.owner_object_id,
           s.owner_version_id, s.tool_name, s.outcome
      FROM app.ai_path_steps s
      JOIN app.ai_paths p
        ON p.id = s.path_id AND p.project_id = s.project_id
     WHERE s.project_id = %(project_id)s
       AND p.lifecycle = 'finalized'
       AND (%(cursor)s::text IS NULL OR s.id > %(cursor)s::text)
     ORDER BY s.id
     LIMIT %(limit)s
"""


def _context_ai_path_step_event(row: Mapping[str, Any]) -> ReferenceEvent:
    """One observed step, anchored to its path in the order the owner recorded.

    `ordinal` is a PROVEN order: `app.ai_path_steps` declares it NOT NULL with a
    unique index per path, so it is the owner's own sequence rather than a
    timestamp sort. That is the one condition under which an ordinal may be
    stored on an edge at all.
    """
    step_id = str(row["id"])
    path_id = str(row["path_id"])
    links = [
        LinkSpec(
            relation="anchored_by",
            to_source_identity_key=_identity("context_ai_path", path_id),
            ordinal=int(row["ordinal"]),
        )
    ]
    # A step that names an exact owner object gets a typed edge to it. The step
    # table carries workspace/type/id/version columns precisely so the reference
    # is exact rather than reconstructed from a tool name.
    workspace = str(row.get("owner_workspace") or "")
    object_type = str(row.get("owner_object_type") or "")
    object_id = str(row.get("owner_object_id") or "")
    if workspace in OWNER_WORKSPACES and object_type and object_id:
        links.append(
            LinkSpec(
                relation="references",
                to_owner_workspace=workspace,
                to_owner_object_type=object_type,
                to_owner_object_id=object_id,
                to_owner_version_id=row.get("owner_version_id"),
            )
        )
    return ReferenceEvent(
        producer="context_ai_path_step",
        record_kind="evidence_trace",
        owner_workspace="context-hub",
        owner_object_type="ai-path-step",
        owner_object_id=step_id,
        occurred_at=row.get("observed_at"),
        source_identity_key=_identity("context_ai_path_step", step_id),
        correlations=(("ai_path", path_id),),
        links=tuple(links),
    )


# --- Audit ------------------------------------------------------------------
#
# Project scope resolves from the authorized resource graph, never from a client
# parameter and never from a label. Two disjoint sources, in this order:
#
#   * a normalized `resource_path` entry naming a Project or a Datastream, joined
#     back to the Project that owns it;
#   * a legacy `metadata->>'project_id'` row, included ONLY when that value is an
#     explicit Project of the same organization. An unscoped legacy row is
#     excluded, because "this probably belongs to the Project you are looking at"
#     is exactly the inference an audit lens must never make.
#
# `provider_account`, `connection_ref` and raw `metadata` are not selected. Not
# filtered downstream — not selected, so no later refactor can leak them.

_AUDIT_SQL = """
    WITH scoped AS (
        SELECT a.id, a.identity, a.action, a.outcome, a.trace_id, a.operation_id,
               a.policy_version, a.catalog_version, a.tool_version,
               a.before_hash, a.after_hash, a.resource_path, a.created_at,
               d.project_id AS resolved_project_id,
               'resource_graph' AS scope_source
          FROM app.audit_log a
          JOIN LATERAL jsonb_array_elements_text(a.resource_path) AS entry(value) ON TRUE
          JOIN app.datastreams d
            ON d.id = split_part(entry.value, ':', 2)
           AND entry.value LIKE 'datastream:%%'
         WHERE a.resource_path IS NOT NULL
           AND d.project_id = %(project_id)s
        UNION
        SELECT a.id, a.identity, a.action, a.outcome, a.trace_id, a.operation_id,
               a.policy_version, a.catalog_version, a.tool_version,
               a.before_hash, a.after_hash, a.resource_path, a.created_at,
               p.id AS resolved_project_id,
               'resource_graph' AS scope_source
          FROM app.audit_log a
          JOIN LATERAL jsonb_array_elements_text(a.resource_path) AS entry(value) ON TRUE
          JOIN app.projects p
            ON p.id = split_part(entry.value, ':', 2)
           AND entry.value LIKE 'project:%%'
         WHERE a.resource_path IS NOT NULL
           AND p.id = %(project_id)s
        UNION
        SELECT a.id, a.identity, a.action, a.outcome, a.trace_id, a.operation_id,
               a.policy_version, a.catalog_version, a.tool_version,
               a.before_hash, a.after_hash, a.resource_path, a.created_at,
               p.id AS resolved_project_id,
               'legacy_metadata' AS scope_source
          FROM app.audit_log a
          JOIN app.projects p ON p.id = a.metadata->>'project_id'
         WHERE p.id = %(project_id)s
    )
    SELECT * FROM scoped
     WHERE (%(cursor)s::text IS NULL OR id > %(cursor)s::text)
     ORDER BY id
     LIMIT %(limit)s
"""


def _audit_event(row: Mapping[str, Any]) -> ReferenceEvent:
    audit_id = str(row["id"])
    correlations: list[tuple[str, str]] = [("audit", audit_id)]
    trace = str(row.get("trace_id") or "")
    if is_w3c_trace_id(trace):
        correlations.append(("w3c_trace", trace))
    operation = row.get("operation_id")
    if operation:
        correlations.append(("operation", str(operation)))
    return ReferenceEvent(
        producer="platform_audit",
        record_kind="audit_event",
        owner_workspace="governance",
        owner_object_type="audit-event",
        owner_object_id=audit_id,
        occurred_at=row.get("created_at"),
        integrity_hash=row.get("after_hash"),
        redaction_class="audit_normalized",
        source_identity_key=_identity("platform_audit", audit_id),
        correlations=tuple(correlations),
    )


# --- The registry itself ----------------------------------------------------

PRODUCERS: tuple[ProducerContract, ...] = (
    # Order matters for the backfill: an anchor is registered before the nodes
    # that link to it, so an edge resolves internally instead of degrading to an
    # external owner reference.
    ProducerContract(
        producer="data_execution",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-execution",
        label="Datastream execution (app.datastream_executions)",
        creates_anchor=True,
        correlation_kinds=("execution",),
        backfill_sql=_DATA_EXECUTIONS_SQL,
        row_to_event=_data_execution_event,
    ),
    ProducerContract(
        producer="data_execution_stage",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-execution-stage",
        label="Execution stage evidence (app.datastream_execution_stage_evidence)",
        correlation_kinds=("execution",),
        backfill_sql=_DATA_STAGE_SQL,
        row_to_event=_data_stage_event,
    ),
    ProducerContract(
        producer="data_execution_phase",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-execution-phase",
        label="Execution phase evidence (app.datastream_execution_phase_evidence)",
        correlation_kinds=("execution", "w3c_trace"),
        backfill_sql=_DATA_PHASE_SQL,
        row_to_event=_data_phase_event,
    ),
    ProducerContract(
        producer="data_publication",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-publication",
        label="Publication pointer swap (app.datastream_publication_log)",
        creates_anchor=True,
        correlation_kinds=("publication", "execution"),
        backfill_sql=_DATA_PUBLICATION_SQL,
        row_to_event=_data_publication_event,
    ),
    ProducerContract(
        producer="data_mapping_publication",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="datastream-mapping-publication",
        label="Mapping publication (app.datastream_mapping_publication_log)",
        creates_anchor=True,
        backfill_sql=_DATA_MAPPING_PUBLICATION_SQL,
        row_to_event=_data_mapping_publication_event,
    ),
    _version_producer(
        "data_mapping_version",
        label="Datastream mapping version",
        owner_workspace="data",
        owner_object_type="datastream-mapping-version",
        table="datastream_mapping_versions",
        object_column="datastream_id",
        sql=_DATA_MAPPING_VERSION_SQL,
        correlation_kinds=("confirmation", "operation"),
    ),
    _version_producer(
        "governance_master_data_version",
        label="Master Data object version",
        owner_workspace="governance",
        owner_object_type="master-data-version",
        table="master_data_object_versions",
        object_column="registry_id",
        sql=_sequenced_version_sql(
            "master_data_object_versions",
            "registry_id",
            extra_columns="v.published_at,",
        ),
    ),
    _version_producer(
        "governance_semantic_concept_version",
        label="Semantic Concept version",
        owner_workspace="governance",
        owner_object_type="semantic-concept-version",
        table="semantic_concept_versions",
        object_column="concept_id",
        sql=_sequenced_version_sql(
            "semantic_concept_versions",
            "concept_id",
            extra_columns="NULL::timestamptz AS published_at,",
        ),
    ),
    _version_producer(
        "governance_semantic_view_version",
        label="Semantic View version",
        owner_workspace="governance",
        owner_object_type="semantic-view-version",
        table="semantic_view_versions",
        object_column="view_id",
        sql=_sequenced_version_sql(
            "semantic_view_versions", "view_id", extra_columns="NULL::timestamptz AS published_at,"
        ),
    ),
    _version_producer(
        "governance_rule_set_version",
        label="Rule Set version",
        owner_workspace="governance",
        owner_object_type="rule-set-version",
        table="governance_rule_set_versions",
        object_column="rule_set_id",
        sql=_RULE_SET_VERSION_SQL,
        correlation_kinds=("confirmation", "operation"),
    ),
    _version_producer(
        "governance_dq_monitor_version",
        label="DQ Monitor version",
        owner_workspace="governance",
        owner_object_type="dq-monitor-version",
        table="dq_monitor_versions",
        object_column="monitor_id",
        sql=_sequenced_version_sql(
            "dq_monitor_versions", "monitor_id", extra_columns="NULL::timestamptz AS published_at,"
        ),
    ),
    # Story 60.5. The eighth and ninth version producers, and the cost of each is
    # this registration: `_version_producer` already knows the only shape they all
    # share -- `(object, version_number)` UNIQUE, a `content_hash`, and a
    # `predecessor_version_id` for the `supersedes` edge. Neither of them needs a
    # screen of its own: the history of a transformation rule lands in
    # `evidence/versions-approvals` beside the seven governed objects that already
    # have one.
    _version_producer(
        "governance_value_mapping_table_version",
        label="Value mapping table version",
        owner_workspace="governance",
        owner_object_type="value-mapping-table-version",
        table="value_mapping_table_versions",
        object_column="table_id",
        sql=_VALUE_MAPPING_TABLE_VERSION_SQL,
    ),
    _version_producer(
        "governance_cleanup_rule_version",
        label="Cleanup rule version",
        owner_workspace="governance",
        owner_object_type="cleanup-rule-version",
        table="cleanup_rule_versions",
        object_column="rule_id",
        sql=_sequenced_version_sql(
            "cleanup_rule_versions", "rule_id", extra_columns="NULL::timestamptz AS published_at,"
        ),
    ),
    _version_producer(
        "governance_project_configuration_version",
        label="Project configuration version",
        owner_workspace="governance",
        owner_object_type="project-configuration-version",
        table="project_configuration_versions",
        object_column="configuration_object_id",
        sql=_PROJECT_CONFIGURATION_VERSION_SQL,
    ),
    ProducerContract(
        producer="platform_audit",
        record_kind="audit_event",
        owner_workspace="governance",
        owner_object_type="audit-event",
        label="Normalized audit spine (app.audit_log, migration 060)",
        correlation_kinds=("audit", "w3c_trace", "operation"),
        redaction_class="audit_normalized",
        backfill_sql=_AUDIT_SQL,
        row_to_event=_audit_event,
    ),
    # --- Future owners. Declared, named, and honestly unavailable. -----------
    ProducerContract(
        producer="analyze_render",
        record_kind="evidence_trace",
        owner_workspace="analyze",
        owner_object_type="render",
        label="Analyze Render",
        disposition="future_owner",
        reason_code="render_owner_not_delivered",
        reason=(
            "`app.render_snapshots` is bounded-retention incumbent storage with no stable "
            "version identity and no governed route. It is not the ratified Render owner, "
            "so no Render evidence is indexed and none is invented. Epic 50 owns it."
        ),
    ),
    ProducerContract(
        producer="context_ai_path",
        record_kind="evidence_trace",
        owner_workspace="context-hub",
        owner_object_type="ai-path",
        label="Context Hub AI Path (app.ai_paths, finalized only)",
        creates_anchor=True,
        correlation_kinds=("ai_path", "w3c_trace"),
        backfill_sql=_CONTEXT_AI_PATHS_SQL,
        row_to_event=_context_ai_path_event,
    ),
    ProducerContract(
        producer="context_ai_path_step",
        record_kind="evidence_trace",
        owner_workspace="context-hub",
        owner_object_type="ai-path-step",
        label="Context Hub AI Path step (app.ai_path_steps)",
        correlation_kinds=("ai_path",),
        backfill_sql=_CONTEXT_AI_PATH_STEPS_SQL,
        row_to_event=_context_ai_path_step_event,
    ),
    ProducerContract(
        producer="test_evaluation_run",
        record_kind="evidence_trace",
        owner_workspace="test",
        owner_object_type="evaluation-run",
        label="Test Evaluation Run",
        disposition="future_owner",
        reason_code="evaluation_owner_not_delivered",
        reason=(
            "`app.eval_runs` is a shallow legacy score row with a generated UUID and no "
            "case-level result. It is not the complete Evaluation Run contract. Epic 51 "
            "owns it."
        ),
    ),
    # --- Explicitly excluded. Named so the absence is deliberate. ------------
    ProducerContract(
        producer="operation_outbox",
        record_kind="evidence_trace",
        owner_workspace="governance",
        owner_object_type="outbox-event",
        label="Operation outbox (app.operation_outbox)",
        disposition="excluded",
        reason_code="transport_not_evidence",
        reason=(
            "The outbox is delivery transport: its rows are retried, locked and marked "
            "delivered. Indexing them would make Evidence a queue monitor."
        ),
    ),
    ProducerContract(
        producer="setup_task_events",
        record_kind="evidence_trace",
        owner_workspace="data",
        owner_object_type="setup-task-event",
        label="Setup task events (app.setup_task_events)",
        disposition="excluded",
        reason_code="progress_not_evidence",
        reason=(
            "Setup task events record how far a person got through an assistant. They "
            "prove no data lineage and pin no version."
        ),
    ),
    # Story 50.7 repointed this contract at the new owner. The DISPOSITION and the
    # REASON CODE are unchanged, because the reasoning was always correct: a share
    # is an access grant, not a lineage claim. Only the table it names moved, from
    # the retired `app.render_snapshot_shares` to `app.render_shares`.
    ProducerContract(
        producer="render_shares",
        record_kind="evidence_trace",
        owner_workspace="analyze",
        owner_object_type="render-share",
        label="Render shares (app.render_shares)",
        disposition="excluded",
        reason_code="sharing_not_evidence",
        reason=(
            "A share is a revocable access grant over a Render, not a claim about how "
            "data came to be. Access history belongs to Audit Activity, which already "
            "records it through the audit spine -- and, since Story 50.7, to the "
            "append-only app.render_share_access_events log."
        ),
    ),
)

PRODUCERS_BY_KEY: dict[str, ProducerContract] = {p.producer: p for p in PRODUCERS}
INDEXED_PRODUCERS: tuple[ProducerContract, ...] = tuple(p for p in PRODUCERS if p.is_indexed)


# ---------------------------------------------------------------------------
# Owner routes.
#
# A raw browser URL is never stored. The index holds a semantic owner reference
# and the client builds the href from its own registry, so renaming a route
# cannot freeze a dead address into an immutable row. An owner type absent from
# this table produces NO link at all — an unroutable reference is reported as
# unavailable rather than emitted as a guess.
# ---------------------------------------------------------------------------

_OWNER_ROUTES: dict[str, tuple[str, str, str, str | None]] = {
    # owner_object_type: (workspace, section, route object type, tab)
    # The tab is None where the object contract declares NO tab -- `ai-path`
    # below. Naming one anyway would build an address the console refuses.
    "datastream": ("data", "datastreams", "datastream", "overview"),
    "datastream-execution": ("data", "datastreams", "datastream", "runs"),
    "datastream-execution-stage": ("data", "datastreams", "datastream", "runs"),
    "datastream-execution-phase": ("data", "datastreams", "datastream", "runs"),
    "datastream-publication": ("data", "datastreams", "datastream", "outputs"),
    "datastream-mapping-publication": ("data", "datastreams", "datastream", "mapping"),
    "datastream-mapping-version": ("data", "datastreams", "datastream", "mapping"),
    "datastream-plan-version": ("data", "datastreams", "datastream", "processing"),
    "master-data-version": ("governance", "master-data", "registry", "versions"),
    "semantic-concept-version": ("governance", "semantic-model", "semantic-concept", "versions"),
    "semantic-view-version": ("governance", "semantic-model", "semantic-view", "versions"),
    "rule-set-version": ("governance", "controls-quality", "rule-set", "versions"),
    # Story 60.5. Both routes exist because that story opened the `versions` tab on
    # the two object types (`governance_read_model.py`, Semantic Model): a link is
    # emitted here only because there is a screen at the other end of it.
    "value-mapping-table-version": (
        "governance",
        "semantic-model",
        "value-mapping-table",
        "versions",
    ),
    "cleanup-rule-version": ("governance", "semantic-model", "cleanup-rule", "versions"),
    "dq-monitor-version": ("governance", "controls-quality", "dq-monitor", "overview"),
    "project-configuration-version": ("governance", "evidence", "object-version", "overview"),
    "audit-event": ("governance", "evidence", "audit-event", "overview"),
    # Story 49.6 AC9. The `usage` tab is not a choice made here: `page-structure.md`
    # (l.605) maps it to the exact sentence that authorises this link -- "Context
    # Hub may link published Event identities into business paths and
    # timeline/graph overlays". The object is the CONFIGURATION, Data-owned; an
    # observation of it has no screen of its own and must never be given one.
    "event-configuration": ("data", "events", "event-configuration", "usage"),
    # Story 49.6 lot 4. See the block above `_CONTEXT_AI_PATHS_SQL`: the screen
    # is mounted, so the reference stops being an identifier and becomes a link.
    # NO TAB, because `navigation/contextHub.ts` declares `{ type: "ai-path" }`
    # with none -- and no version tail either, since a path has one content hash
    # and no versions.
    "ai-path": ("context-hub", "knowledge-graph", "ai-path", None),
    # 2026-09-05 -- the objects a tool call REACHES, read from its arguments
    # (`ai_path_recorder.owner_from_choices`): what an Analyze or Governance step
    # acted on, so « Reached » names the object and the Knowledge Graph lights it.
    "result": ("analyze", "explore", "result", "view"),
    "query-spec": ("analyze", "explore", "query-spec", "query"),
    "visualization": ("analyze", "explore", "visualization", "build"),
    "semantic-view": ("governance", "semantic-model", "semantic-view", "definition"),
    "semantic-concept": ("governance", "semantic-model", "semantic-concept", "definition"),
    "canonical-field": ("governance", "semantic-model", "canonical-field", "definition"),
    # AI-376 (Opus F3): the Context Hub objects a knowledge read reaches. The console
    # declares `context-topic` and `context-procedure` (`navigation/contextHub.ts`)
    # with content/usage/versions; every Topic and Procedure step printed « no screen
    # opens this object from here » while the screen existed.
    "topic": ("context-hub", "knowledge-library", "context-topic", "content"),
    "procedure": ("context-hub", "skills-registry", "context-procedure", "content"),
}


def owner_route(
    owner_object_type: str, owner_object_id: str, owner_version_id: str | None = None
) -> dict[str, Any] | None:
    """A validated semantic owner reference, or None when it cannot be proven."""
    route = _OWNER_ROUTES.get(owner_object_type)
    if route is None or not owner_object_id:
        return None
    workspace, section, route_type, tab = route
    from core.project_overview import owner_reference  # noqa: PLC0415 -- shared contract

    return owner_reference(
        workspace,
        section,
        object_type=route_type,
        object_id=owner_object_id,
        tab=tab,
        version_id=owner_version_id,
    )


# ---------------------------------------------------------------------------
# The projector.
# ---------------------------------------------------------------------------

_INSERT_RECORD = """
    INSERT INTO app.evidence_records (
        id, org_id, project_id, record_kind, producer,
        owner_workspace, owner_object_type, owner_object_id, owner_version_id,
        is_anchor, occurred_at, observed_at, source_identity_key,
        source_payload_hash, integrity_hash, redaction_class
    ) VALUES (
        %(id)s, %(org_id)s, %(project_id)s, %(record_kind)s, %(producer)s,
        %(owner_workspace)s, %(owner_object_type)s, %(owner_object_id)s, %(owner_version_id)s,
        %(is_anchor)s, %(occurred_at)s, %(observed_at)s, %(source_identity_key)s,
        %(source_payload_hash)s, %(integrity_hash)s, %(redaction_class)s
    )
    ON CONFLICT (project_id, source_identity_key) DO NOTHING
    RETURNING id
"""

_SELECT_BY_SOURCE = """
    SELECT id, source_payload_hash
      FROM app.evidence_records
     WHERE project_id = %(project_id)s AND source_identity_key = %(key)s
"""

_INSERT_CORRELATION = """
    INSERT INTO app.evidence_correlations
        (id, org_id, project_id, record_id, correlation_kind, correlation_id)
    VALUES (%(id)s, %(org_id)s, %(project_id)s, %(record_id)s, %(kind)s, %(value)s)
    ON CONFLICT (record_id, correlation_kind, correlation_id) DO NOTHING
"""

_INSERT_LINK = """
    INSERT INTO app.evidence_links (
        id, org_id, project_id, from_record_id, relation,
        to_record_id, to_owner_workspace, to_owner_object_type,
        to_owner_object_id, to_owner_version_id, ordinal, integrity_hash
    ) VALUES (
        %(id)s, %(org_id)s, %(project_id)s, %(from_record_id)s, %(relation)s,
        %(to_record_id)s, %(to_owner_workspace)s, %(to_owner_object_type)s,
        %(to_owner_object_id)s, %(to_owner_version_id)s, %(ordinal)s, %(integrity_hash)s
    )
    ON CONFLICT DO NOTHING
"""


def register_reference(
    conn: Any, *, org_id: str, project_id: str, event: ReferenceEvent
) -> tuple[str, bool]:
    """Index one owner reference. Returns ``(record_id, created)``.

    Idempotent by ``(project_id, source_identity_key)``. A replay returns the
    existing id and writes nothing. A DIFFERENT payload under the same identity
    raises :class:`EvidenceConflict` — it never updates the immutable record in
    place, and never appends a second record that would double the owner.
    """
    if not org_id or not project_id:
        raise EvidenceIndexError("an Evidence Record needs an exact organization and Project")
    payload_hash = event.payload_hash

    existing = _fetch(
        conn, _SELECT_BY_SOURCE, {"project_id": project_id, "key": event.source_identity_key}
    )
    if existing:
        record_id = str(existing[0]["id"])
        if str(existing[0]["source_payload_hash"]) != payload_hash:
            raise EvidenceConflict(
                f"source identity already indexed with a different payload: {event.producer}"
            )
        return record_id, False

    record_id = _mint("evr")
    inserted = _fetch(
        conn,
        _INSERT_RECORD,
        {
            "id": record_id,
            "org_id": org_id,
            "project_id": project_id,
            "record_kind": event.record_kind,
            "producer": event.producer,
            "owner_workspace": event.owner_workspace,
            "owner_object_type": event.owner_object_type,
            "owner_object_id": event.owner_object_id,
            "owner_version_id": event.owner_version_id,
            "is_anchor": event.is_anchor,
            "occurred_at": event.occurred_at,
            "observed_at": event.observed_at,
            "source_identity_key": event.source_identity_key,
            "source_payload_hash": payload_hash,
            "integrity_hash": _sha_or_none(event.integrity_hash),
            "redaction_class": event.redaction_class,
        },
    )
    if not inserted:
        # A concurrent projector won the race. Re-read and apply the same
        # conflict rule, so two workers can never produce two records.
        return register_reference(conn, org_id=org_id, project_id=project_id, event=event)

    for kind, value in event.correlations:
        _fetch(
            conn,
            _INSERT_CORRELATION,
            {
                "id": _mint("evc"),
                "org_id": org_id,
                "project_id": project_id,
                "record_id": record_id,
                "kind": kind,
                "value": value,
            },
        )

    for link in event.links:
        target_id: str | None = None
        if link.to_source_identity_key is not None:
            found = _fetch(
                conn,
                _SELECT_BY_SOURCE,
                {"project_id": project_id, "key": link.to_source_identity_key},
            )
            if not found:
                # The destination is not (yet) indexed. The edge is DROPPED
                # rather than downgraded to a fabricated owner reference: a
                # missing edge stops the proven chain, and the workbench reports
                # the gap. Guessing a destination is how a trace grows a step
                # nobody observed.
                continue
            target_id = str(found[0]["id"])
        _fetch(
            conn,
            _INSERT_LINK,
            {
                "id": _mint("evl"),
                "org_id": org_id,
                "project_id": project_id,
                "from_record_id": record_id,
                "relation": link.relation,
                "to_record_id": target_id,
                "to_owner_workspace": link.to_owner_workspace,
                "to_owner_object_type": link.to_owner_object_type,
                "to_owner_object_id": link.to_owner_object_id,
                "to_owner_version_id": link.to_owner_version_id,
                "ordinal": link.ordinal,
                "integrity_hash": _sha_or_none(link.integrity_hash),
            },
        )
    return record_id, True


def record_availability(
    conn: Any,
    *,
    org_id: str,
    project_id: str,
    record_id: str,
    availability: str,
    reason_code: str,
    actor: str,
) -> str:
    """Append an availability fact. It never edits the record it qualifies."""
    event_id = _mint("eva")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evidence_availability_events
                (id, org_id, project_id, record_id, availability, reason_code, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (event_id, org_id, project_id, record_id, availability, reason_code, actor),
        )
    return event_id


def quarantine(
    conn: Any, *, project_id: str, producer: str, source_identity_key: str, error_class: str
) -> None:
    """Park a poison projection under a SAFE error class, and nothing else.

    There is no payload parameter, because the table has no payload column. A
    quarantine that stores the event body is where an unredacted provider
    response ends up living under an operational name.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evidence_projection_quarantine
                (id, project_id, producer, source_identity_key, error_class)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (project_id, producer, source_identity_key) DO UPDATE
               SET attempts = app.evidence_projection_quarantine.attempts + 1,
                   last_seen_at = NOW(),
                   error_class = EXCLUDED.error_class
            """,
            (_mint("evq"), project_id, producer, source_identity_key[:400], error_class),
        )


# ---------------------------------------------------------------------------
# Backfill — restartable, bounded, watermarked.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackfillReport:
    producer: str
    indexed: int
    scanned: int
    state: str
    failure_class: str | None = None
    cursor: str | None = None


def _read_watermark(conn: Any, project_id: str, producer: str) -> dict[str, Any] | None:
    rows = _fetch(
        conn,
        """
        SELECT last_source_cursor, last_indexed_at, indexed_count, state, failure_class, attempts
          FROM app.evidence_index_watermarks
         WHERE project_id = %(project_id)s AND producer = %(producer)s
        """,
        {"project_id": project_id, "producer": producer},
    )
    return rows[0] if rows else None


def _write_watermark(
    conn: Any,
    project_id: str,
    producer: str,
    *,
    cursor: str | None,
    indexed: int,
    state: str,
    failure_class: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evidence_index_watermarks
                (project_id, producer, last_source_cursor, last_indexed_at,
                 indexed_count, state, failure_class, attempts, updated_at)
            VALUES (%s, %s, %s, NOW(), %s, %s, %s, 1, NOW())
            ON CONFLICT (project_id, producer) DO UPDATE
               SET last_source_cursor = EXCLUDED.last_source_cursor,
                   last_indexed_at = NOW(),
                   indexed_count = app.evidence_index_watermarks.indexed_count
                                 + EXCLUDED.indexed_count,
                   state = EXCLUDED.state,
                   failure_class = EXCLUDED.failure_class,
                   attempts = app.evidence_index_watermarks.attempts + 1,
                   updated_at = NOW()
            """,
            (project_id, producer, cursor, indexed, state, failure_class),
        )


def backfill_producer(
    conn: Any,
    *,
    org_id: str,
    project_id: str,
    producer: str,
    limit: int = MAX_BACKFILL_BATCH,
) -> BackfillReport:
    """One bounded pass over one producer, resumable from its own watermark."""
    contract = PRODUCERS_BY_KEY.get(producer)
    if contract is None or not contract.is_indexed:
        raise EvidenceIndexError(f"producer is not indexable: {producer!r}")
    assert contract.backfill_sql and contract.row_to_event  # registry invariant

    watermark = _read_watermark(conn, project_id, producer)
    cursor = watermark["last_source_cursor"] if watermark else None
    bounded = max(1, min(int(limit), MAX_BACKFILL_BATCH))

    try:
        rows = _fetch(
            conn,
            contract.backfill_sql,
            {"project_id": project_id, "cursor": cursor, "limit": bounded},
        )
    except Exception as exc:  # noqa: BLE001 -- an unreadable owner is not zero rows
        failure = type(exc).__name__.lower()[:60] or "owner_unreadable"
        _write_watermark(
            conn,
            project_id,
            producer,
            cursor=cursor,
            indexed=0,
            state="failed",
            failure_class=failure,
        )
        return BackfillReport(producer, 0, 0, "failed", failure, cursor)

    indexed = 0
    last_cursor = cursor
    for row in rows:
        source_key = ""
        try:
            event = contract.row_to_event(row)
            source_key = event.source_identity_key
            _, created = register_reference(conn, org_id=org_id, project_id=project_id, event=event)
            indexed += int(created)
        except EvidenceConflict:
            quarantine(
                conn,
                project_id=project_id,
                producer=producer,
                source_identity_key=source_key or _identity(producer, row.get("id")),
                error_class="source_identity_conflict",
            )
        except Exception as exc:  # noqa: BLE001 -- one poison row is not a failed pass
            quarantine(
                conn,
                project_id=project_id,
                producer=producer,
                source_identity_key=source_key or _identity(producer, row.get("id")),
                error_class=(type(exc).__name__.lower()[:60] or "projection_failed"),
            )
        last_cursor = str(row.get("id") or last_cursor)

    state = "backfilling" if len(rows) == bounded else "idle"
    _write_watermark(
        conn,
        project_id,
        producer,
        cursor=last_cursor,
        indexed=indexed,
        state=state,
        failure_class=None,
    )
    return BackfillReport(producer, indexed, len(rows), state, None, last_cursor)


def backfill_project(
    conn: Any, *, org_id: str, project_id: str, limit: int = MAX_BACKFILL_BATCH
) -> list[BackfillReport]:
    """Every indexable producer, in registry order (anchors before their nodes)."""
    return [
        backfill_producer(
            conn, org_id=org_id, project_id=project_id, producer=contract.producer, limit=limit
        )
        for contract in INDEXED_PRODUCERS
    ]


# ---------------------------------------------------------------------------
# The durable projector.
#
# The owner writes its mutation and its outbox event in ONE transaction
# (migration 060). That event is the durable signal this projector consumes.
#
# What it does NOT do is trust the event's payload. An outbox payload is written
# by the owner for the owner's own consumers; reading it here would make the
# index a mirror of whatever shape that payload happens to have, which is the
# generic event store the contract forbids. The event is used only to learn
# WHICH producer moved -- and the producer is then re-read through its own
# registered query, so what gets indexed is always the owner's current truth.
#
# A projector failure never touches the owner artifact and never marks
# unavailable evidence healthy: the failure lands in the producer's watermark as
# a safe class, and the owner's mutation stays committed.
# ---------------------------------------------------------------------------

#: Outbox `event_type` -> the producer it moves. An unmapped event type is
#: IGNORED rather than guessed: an event nobody registered is not evidence.
OUTBOX_EVENT_PRODUCERS: dict[str, str] = {
    "datastream.execution.created": "data_execution",
    "datastream.execution.stage_recorded": "data_execution_stage",
    "datastream.execution.phase_recorded": "data_execution_phase",
    "datastream.publication.committed": "data_publication",
    "datastream.mapping.published": "data_mapping_publication",
    "datastream.mapping.version_created": "data_mapping_version",
    "context.ai_path.finalized": "context_ai_path",
    "governance.rule_set.published": "governance_rule_set_version",
    "governance.master_data.published": "governance_master_data_version",
    "governance.semantic_concept.published": "governance_semantic_concept_version",
    "governance.semantic_view.published": "governance_semantic_view_version",
    "governance.dq_monitor.published": "governance_dq_monitor_version",
    "project.configuration.activated": "governance_project_configuration_version",
}

_PENDING_OUTBOX = """
    SELECT o.id, o.event_type, o.created_at, op.effective_org_id
      FROM app.operation_outbox o
      JOIN app.operations op ON op.id = o.operation_id
     WHERE op.effective_org_id = %(org_id)s
       AND o.event_type = ANY(%(event_types)s)
       AND o.created_at > COALESCE(%(since)s::timestamptz, TIMESTAMPTZ '-infinity')
     ORDER BY o.created_at, o.id
     LIMIT %(limit)s
"""

#: The projector's own position, kept under a reserved producer key so it shares
#: the watermark table without pretending to be an adapter.
_PROJECTION_KEY = "outbox_projection"


@dataclass(frozen=True, slots=True)
class ProjectionReport:
    consumed: int
    indexed: int
    producers: tuple[str, ...]
    lag_seconds: float | None
    failures: tuple[str, ...] = ()


def _projection_watermark(conn: Any, project_id: str) -> Any:
    rows = _fetch(
        conn,
        """
        SELECT last_source_cursor
          FROM app.evidence_index_watermarks
         WHERE project_id = %(project_id)s AND producer = %(producer)s
        """,
        {"project_id": project_id, "producer": _PROJECTION_KEY},
    )
    return rows[0]["last_source_cursor"] if rows else None


def project_from_outbox(
    conn: Any, *, org_id: str, project_id: str, limit: int = 100
) -> ProjectionReport:
    """Consume durable reference events once, idempotently, and record the lag.

    Returns what it consumed and how far behind the newest event it was. The lag
    is reported rather than acted on: a projector that decided for itself that it
    was "too far behind" would start dropping events to catch up.
    """
    events = _fetch(
        conn,
        _PENDING_OUTBOX,
        {
            "org_id": org_id,
            "event_types": list(OUTBOX_EVENT_PRODUCERS),
            "since": _projection_watermark(conn, project_id),
            "limit": max(1, min(int(limit), MAX_BACKFILL_BATCH)),
        },
    )
    if not events:
        return ProjectionReport(0, 0, (), None)

    moved: list[str] = []
    for event in events:
        producer = OUTBOX_EVENT_PRODUCERS.get(str(event["event_type"]))
        if producer and producer not in moved:
            moved.append(producer)

    indexed = 0
    failures: list[str] = []
    # Registry order, not event order: an anchor is registered before the nodes
    # that link to it, so an edge resolves internally instead of being dropped.
    for contract in INDEXED_PRODUCERS:
        if contract.producer not in moved:
            continue
        report = backfill_producer(
            conn, org_id=org_id, project_id=project_id, producer=contract.producer, limit=limit
        )
        indexed += report.indexed
        if report.state == "failed" and report.failure_class:
            failures.append(f"{contract.producer}:{report.failure_class}")

    newest = max(event["created_at"] for event in events)
    _write_watermark(
        conn,
        project_id,
        _PROJECTION_KEY,
        cursor=_iso(newest),
        indexed=0,
        state="idle",
        failure_class=None,
    )
    lag = None
    if isinstance(newest, datetime):
        moment = newest if newest.tzinfo else newest.replace(tzinfo=UTC)
        lag = max(0.0, (datetime.now(UTC) - moment).total_seconds())

    return ProjectionReport(len(events), indexed, tuple(moved), lag, tuple(failures))


# ---------------------------------------------------------------------------
# Cursors and filters.
#
# The SERVER applies both. A cursor is opaque, scoped to the exact Project, lens
# and filter set, and stable under newer inserts because the keyset descends on
# `(occurred_at, id)` — a row inserted later sorts before the cursor position
# and cannot shift the page under it.
# ---------------------------------------------------------------------------

ALLOWED_FILTERS = (
    "from",
    "to",
    "owner_workspace",
    "record_kind",
    "correlation_kind",
    "correlation_id",
    "outcome",
)


def _filter_fingerprint(lens: str, filters: Mapping[str, str]) -> str:
    return _hash({"lens": lens, "filters": {k: filters[k] for k in sorted(filters)}})


def encode_cursor(
    project_id: str, lens: str, filters: Mapping[str, str], row: Mapping[str, Any]
) -> str:
    payload = {
        "p": project_id,
        "l": lens,
        "f": _filter_fingerprint(lens, filters),
        "o": _iso(row["occurred_at"]),
        "i": str(row["id"]),
    }
    return (
        base64.urlsafe_b64encode(canonical_json(payload).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def decode_cursor(
    cursor: str, project_id: str, lens: str, filters: Mapping[str, str]
) -> tuple[str, str]:
    """Return the keyset position, or refuse. There is no fallback to page one."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise EvidenceCursorInvalid("malformed cursor") from exc
    if not isinstance(payload, dict):
        raise EvidenceCursorInvalid("malformed cursor")
    if payload.get("p") != project_id or payload.get("l") != lens:
        # A cursor minted in another Project or another lens is refused, not
        # reinterpreted. Reinterpreting it is how page two of one Project's
        # Evidence becomes page two of another's.
        raise EvidenceCursorInvalid("cursor is scoped elsewhere")
    if payload.get("f") != _filter_fingerprint(lens, filters):
        raise EvidenceCursorInvalid("cursor does not match this filter set")
    occurred, record_id = payload.get("o"), payload.get("i")
    if not isinstance(occurred, str) or not isinstance(record_id, str):
        raise EvidenceCursorInvalid("malformed cursor")
    return occurred, record_id


def normalize_filters(lens: str, raw: Mapping[str, Any]) -> dict[str, str]:
    """Allowlist, validate, and refuse everything else. No silent dropping."""
    expected_kind = LENS_RECORD_KIND[lens]
    filters: dict[str, str] = {}
    for key, value in raw.items():
        if value in (None, ""):
            continue
        if key not in ALLOWED_FILTERS:
            raise EvidenceFilterInvalid(f"unknown filter: {key!r}")
        text = str(value).strip()
        if len(text) > 200:
            raise EvidenceFilterInvalid(f"filter value too long: {key!r}")
        if key == "record_kind" and text != expected_kind:
            # A lens cannot be asked to show another lens' records. Answering
            # would be the cross-lens reclassification the contract forbids.
            raise EvidenceFilterInvalid("record_kind does not belong to this lens")
        if key == "owner_workspace" and text not in OWNER_WORKSPACES:
            raise EvidenceFilterInvalid("unknown owner workspace")
        if key == "correlation_kind" and text not in CORRELATION_KINDS:
            raise EvidenceFilterInvalid("unknown correlation kind")
        if key in {"from", "to"}:
            try:
                datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise EvidenceFilterInvalid(f"{key} is not an ISO-8601 instant") from exc
        filters[key] = text
    return filters


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Page:
    rows: tuple[dict[str, Any], ...]
    total: int | None
    next_cursor: str | None
    coverage_state: str
    adapters: tuple[dict[str, Any], ...] = ()
    horizon: str | None = None
    reasons: tuple[dict[str, Any], ...] = ()


def _lens_where(lens: str, filters: Mapping[str, str], params: dict[str, Any]) -> str:
    clauses = ["r.project_id = %(project_id)s", "r.record_kind = %(record_kind)s"]
    params["record_kind"] = LENS_RECORD_KIND[lens]
    if lens == "lineage-provenance":
        # A trace IS its anchor. Nodes are reachable from one; listing them
        # beside their own anchor would show the same chain several times.
        clauses.append("r.is_anchor")
    if "owner_workspace" in filters:
        clauses.append("r.owner_workspace = %(owner_workspace)s")
        params["owner_workspace"] = filters["owner_workspace"]
    if "from" in filters:
        clauses.append("r.occurred_at >= %(from)s")
        params["from"] = filters["from"]
    if "to" in filters:
        clauses.append("r.occurred_at <= %(to)s")
        params["to"] = filters["to"]
    if "correlation_kind" in filters or "correlation_id" in filters:
        correlation = ["c.record_id = r.id"]
        if "correlation_kind" in filters:
            correlation.append("c.correlation_kind = %(correlation_kind)s")
            params["correlation_kind"] = filters["correlation_kind"]
        if "correlation_id" in filters:
            correlation.append("c.correlation_id = %(correlation_id)s")
            params["correlation_id"] = filters["correlation_id"]
        clauses.append(
            "EXISTS (SELECT 1 FROM app.evidence_correlations c WHERE "
            + " AND ".join(correlation)
            + ")"
        )
    return " AND ".join(clauses)


_RECORD_COLUMNS = """
    r.id, r.record_kind, r.producer, r.owner_workspace, r.owner_object_type,
    r.owner_object_id, r.owner_version_id, r.is_anchor, r.occurred_at,
    r.observed_at, r.indexed_at, r.integrity_hash, r.redaction_class
"""


def list_records(
    conn: Any,
    *,
    project_id: str,
    lens: str,
    filters: Mapping[str, str],
    cursor: str | None,
    limit: int,
) -> Page:
    """One bounded, server-ordered page of one lens."""
    if lens not in LENS_RECORD_KIND:
        raise EvidenceIndexError(f"unknown Evidence lens: {lens!r}")
    bounded = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
    params: dict[str, Any] = {"project_id": project_id}
    where = _lens_where(lens, filters, params)

    if cursor:
        occurred, record_id = decode_cursor(cursor, project_id, lens, filters)
        params["cursor_occurred"] = occurred
        params["cursor_id"] = record_id
        where += " AND (r.occurred_at, r.id) < (%(cursor_occurred)s::timestamptz, %(cursor_id)s)"

    params["limit"] = bounded + 1
    rows = _fetch(
        conn,
        f"""
        SELECT {_RECORD_COLUMNS}
          FROM app.evidence_records r
         WHERE {where}
         ORDER BY r.occurred_at DESC, r.id DESC
         LIMIT %(limit)s
        """,
        params,
    )
    has_more = len(rows) > bounded
    page = rows[:bounded]

    # The total counts ONLY what this caller can see: the same predicate, the
    # same RLS, the same transaction. A count taken with a wider predicate would
    # be an undercount presented as complete — or worse, a disclosure.
    count_params: dict[str, Any] = {"project_id": project_id}
    count_where = _lens_where(lens, filters, count_params)
    total: int | None
    try:
        counted = _fetch(
            conn,
            f"SELECT COUNT(*) AS total FROM app.evidence_records r WHERE {count_where}",
            count_params,
        )
        total = int(counted[0]["total"]) if counted else None
    except Exception:  # noqa: BLE001 -- a count we could not take is not a zero
        total = None

    coverage, adapters, reasons = _coverage(conn, project_id, lens)
    if total is None and coverage == "complete":
        coverage = "partial"
    horizon = _iso(min((row["occurred_at"] for row in page), default=None))
    next_cursor = encode_cursor(project_id, lens, filters, page[-1]) if has_more and page else None
    return Page(
        rows=tuple(page),
        total=total,
        next_cursor=next_cursor,
        coverage_state=coverage,
        adapters=adapters,
        horizon=horizon,
        reasons=reasons,
    )


def _coverage(
    conn: Any, project_id: str, lens: str
) -> tuple[str, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """What this lens can and cannot prove, per adapter.

    `complete`, `partial`, `unavailable` and `backfilling` are four different
    answers. Collapsing them into "0 results" is the failure this whole story
    exists to remove.
    """
    kind = LENS_RECORD_KIND[lens]
    watermarks = {
        str(row["producer"]): row
        for row in _fetch(
            conn,
            """
            SELECT producer, state, failure_class, last_indexed_at, indexed_count
              FROM app.evidence_index_watermarks
             WHERE project_id = %(project_id)s
            """,
            {"project_id": project_id},
        )
    }

    adapters: list[dict[str, Any]] = []
    reasons: list[dict[str, Any]] = []
    backfilling = False
    failed = False
    never_ran = False

    for contract in PRODUCERS:
        if contract.record_kind != kind:
            continue
        if contract.disposition != "indexed":
            adapters.append(
                {
                    "producer": contract.producer,
                    "label": contract.label,
                    "state": "unavailable"
                    if contract.disposition == "future_owner"
                    else "excluded",
                    "owner_workspace": contract.owner_workspace,
                    "reason_code": contract.reason_code,
                }
            )
            if contract.disposition == "future_owner":
                reasons.append(
                    {
                        "code": contract.reason_code or "owner_not_delivered",
                        "message": contract.reason or "",
                        "owner_reference": owner_route(contract.owner_object_type, "") or None,
                    }
                )
            continue
        watermark = watermarks.get(contract.producer)
        if watermark is None:
            never_ran = True
            state = "never_indexed"
        else:
            state = str(watermark["state"])
            backfilling = backfilling or state == "backfilling"
            failed = failed or state == "failed"
        adapters.append(
            {
                "producer": contract.producer,
                "label": contract.label,
                "state": state,
                "owner_workspace": contract.owner_workspace,
                "last_indexed_at": _iso(watermark["last_indexed_at"]) if watermark else None,
                "indexed_count": int(watermark["indexed_count"]) if watermark else 0,
                "failure_class": watermark["failure_class"] if watermark else None,
            }
        )

    if failed:
        coverage = "unavailable"
        reasons.append(
            {
                "code": "adapter_failed",
                "message": (
                    "At least one Evidence adapter could not read its owner. What is listed is "
                    "what the working adapters proved; it is not a complete inventory, and the "
                    "gap is not an absence."
                ),
            }
        )
    elif backfilling or never_ran:
        coverage = "backfilling"
        reasons.append(
            {
                "code": "index_backfilling",
                "message": (
                    "The Evidence index has not finished registering every eligible owner "
                    "artifact for this Project. Records already indexed are exact; the list is "
                    "not yet complete."
                ),
            }
        )
    else:
        coverage = "complete"
    return coverage, tuple(adapters), tuple(reasons)


def load_record(conn: Any, *, project_id: str, record_id: str) -> dict[str, Any] | None:
    """One exact record. Never a neighbour, and never bounded by a page size."""
    rows = _fetch(
        conn,
        f"""
        SELECT {_RECORD_COLUMNS}
          FROM app.evidence_records r
         WHERE r.project_id = %(project_id)s AND r.id = %(record_id)s
        """,
        {"project_id": project_id, "record_id": record_id},
    )
    return rows[0] if rows else None


def load_correlations(
    conn: Any, *, project_id: str, record_ids: Sequence[str]
) -> dict[str, list[dict[str, str]]]:
    if not record_ids:
        return {}
    rows = _fetch(
        conn,
        """
        SELECT record_id, correlation_kind, correlation_id
          FROM app.evidence_correlations
         WHERE project_id = %(project_id)s AND record_id = ANY(%(ids)s)
         ORDER BY record_id, correlation_kind, correlation_id
        """,
        {"project_id": project_id, "ids": list(record_ids)},
    )
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(str(row["record_id"]), []).append(
            {"kind": str(row["correlation_kind"]), "id": str(row["correlation_id"])}
        )
    return grouped


def load_availability(
    conn: Any, *, project_id: str, record_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """The newest authorized availability fact per record, deterministically.

    Absence means no override — NOT unavailability. A record with no event is
    available exactly as indexed.
    """
    if not record_ids:
        return {}
    rows = _fetch(
        conn,
        """
        SELECT DISTINCT ON (record_id)
               record_id, availability, reason_code, effective_at
          FROM app.evidence_availability_events
         WHERE project_id = %(project_id)s AND record_id = ANY(%(ids)s)
         ORDER BY record_id, effective_at DESC, id DESC
        """,
        {"project_id": project_id, "ids": list(record_ids)},
    )
    return {
        str(row["record_id"]): {
            "availability": str(row["availability"]),
            "reason_code": str(row["reason_code"]),
            "effective_at": _iso(row["effective_at"]),
        }
        for row in rows
    }


_LINKS_FROM = """
    SELECT l.id, l.from_record_id, l.relation, l.to_record_id,
           l.to_owner_workspace, l.to_owner_object_type, l.to_owner_object_id,
           l.to_owner_version_id, l.ordinal, l.integrity_hash
      FROM app.evidence_links l
     WHERE l.project_id = %(project_id)s AND l.from_record_id = ANY(%(ids)s)
     ORDER BY l.from_record_id, l.relation, COALESCE(l.ordinal, 2147483647), l.id
"""

_LINKS_TO = """
    SELECT l.id, l.from_record_id, l.relation, l.to_record_id,
           l.to_owner_workspace, l.to_owner_object_type, l.to_owner_object_id,
           l.to_owner_version_id, l.ordinal, l.integrity_hash
      FROM app.evidence_links l
     WHERE l.project_id = %(project_id)s AND l.to_record_id = ANY(%(ids)s)
     ORDER BY l.to_record_id, l.relation, COALESCE(l.ordinal, 2147483647), l.id
"""


def load_trace_graph(conn: Any, *, project_id: str, anchor_id: str) -> dict[str, Any]:
    """The bounded graph reachable from ONE anchor, through persisted edges only.

    Traversal follows edges in both directions — a stage points at its anchor,
    a publication points at its execution — but it starts at the selected anchor
    and never crosses into another anchor's subgraph except through an edge that
    was explicitly stored. Correlation equality, timestamp proximity, equal
    labels and a shared Project are NOT edges here.
    """
    anchor = load_record(conn, project_id=project_id, record_id=anchor_id)
    if anchor is None:
        raise EvidenceIndexError("anchor is not visible in this Project")

    nodes: dict[str, dict[str, Any]] = {anchor_id: dict(anchor)}
    edges: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []
    frontier = [anchor_id]
    seen_edges: set[str] = set()
    truncated = False

    while frontier:
        batch = frontier
        frontier = []
        outgoing = _fetch(conn, _LINKS_FROM, {"project_id": project_id, "ids": batch})
        incoming = _fetch(conn, _LINKS_TO, {"project_id": project_id, "ids": batch})
        for row in [*outgoing, *incoming]:
            edge_id = str(row["id"])
            if edge_id in seen_edges:
                continue
            seen_edges.add(edge_id)
            target = row.get("to_record_id")
            if target:
                edges.append(
                    {
                        "id": edge_id,
                        "relation": str(row["relation"]),
                        "from": str(row["from_record_id"]),
                        "to": str(target),
                        "ordinal": row.get("ordinal"),
                        "integrity_hash": row.get("integrity_hash"),
                    }
                )
                for candidate in (str(row["from_record_id"]), str(target)):
                    if candidate in nodes:
                        continue
                    if len(nodes) >= MAX_GRAPH_NODES:
                        truncated = True
                        continue
                    record = load_record(conn, project_id=project_id, record_id=candidate)
                    if record is None:
                        # Invisible to this caller. Removed BEFORE any count, so
                        # a hidden record cannot be inferred from a total.
                        continue
                    nodes[candidate] = dict(record)
                    frontier.append(candidate)
            else:
                external.append(
                    {
                        "id": edge_id,
                        "relation": str(row["relation"]),
                        "from": str(row["from_record_id"]),
                        "owner_workspace": row.get("to_owner_workspace"),
                        "owner_object_type": row.get("to_owner_object_type"),
                        "owner_object_id": row.get("to_owner_object_id"),
                        "owner_version_id": row.get("to_owner_version_id"),
                        "ordinal": row.get("ordinal"),
                    }
                )

    node_ids = list(nodes)
    correlations = load_correlations(conn, project_id=project_id, record_ids=node_ids)
    availability = load_availability(conn, project_id=project_id, record_ids=node_ids)

    unavailable: list[dict[str, Any]] = []
    projected_nodes = []
    for record_id, record in nodes.items():
        state = availability.get(record_id, {}).get("availability", "available")
        route = owner_route(
            str(record["owner_object_type"]),
            str(record["owner_object_id"]),
            record.get("owner_version_id"),
        )
        if route is None:
            unavailable.append(
                {
                    "code": "owner_route_unavailable",
                    "message": (
                        "This node's owner type has no registered route, so no link is offered. "
                        "The reference itself is intact."
                    ),
                }
            )
        projected_nodes.append(
            {
                "record_id": record_id,
                "producer": str(record["producer"]),
                "owner_workspace": str(record["owner_workspace"]),
                "owner_object_type": str(record["owner_object_type"]),
                "owner_object_id": str(record["owner_object_id"]),
                "owner_version_id": record.get("owner_version_id"),
                "is_anchor": bool(record["is_anchor"]),
                "occurred_at": _iso(record["occurred_at"]),
                "observed_at": _iso(record.get("observed_at")),
                "integrity_hash": record.get("integrity_hash"),
                "availability": state,
                "correlations": correlations.get(record_id, []),
                "owner_href": route,
            }
        )

    projected_external = []
    for reference in external:
        route = owner_route(
            str(reference["owner_object_type"] or ""),
            str(reference["owner_object_id"] or ""),
            reference.get("owner_version_id"),
        )
        projected_external.append({**reference, "owner_href": route})
        if route is None:
            unavailable.append(
                {
                    "code": "owner_route_unavailable",
                    "message": (
                        "An owner reference on this trace has no registered route. It is shown "
                        "as an identifier and is not turned into a guessed address."
                    ),
                }
            )

    linked = {edge["to"] for edge in edges}
    sources = {edge["from"] for edge in edges}
    roots = sorted(node for node in nodes if node not in linked)
    terminals = sorted(node for node in nodes if node not in sources)

    if truncated:
        unavailable.append(
            {
                "code": "graph_bounded",
                "message": (
                    f"This trace exceeds the {MAX_GRAPH_NODES}-node bound. What is shown is exact; "
                    "the remainder is bounded, not absent."
                ),
            }
        )
    if len(nodes) == 1 and not projected_external:
        unavailable.append(
            {
                "code": "single_node_chain",
                "message": (
                    "Only this record proves anything here. No upstream or downstream step is "
                    "linked, so no chain is drawn from it."
                ),
            }
        )

    coverage = "complete"
    if truncated:
        coverage = "partial"
    elif any(node["availability"] != "available" for node in projected_nodes):
        coverage = "partial"
    elif len(nodes) == 1:
        coverage = "partial"

    return {
        "anchor": {
            "record_id": anchor_id,
            "producer": str(anchor["producer"]),
            "owner_object_type": str(anchor["owner_object_type"]),
            "owner_object_id": str(anchor["owner_object_id"]),
            "owner_version_id": anchor.get("owner_version_id"),
        },
        "nodes": projected_nodes,
        "edges": edges,
        "owner_references": projected_external,
        "roots": roots,
        "terminals": terminals,
        "coverage": coverage,
        "unavailable_reasons": unavailable,
    }


def load_version_detail(conn: Any, *, project_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Object Version detail, composed only from links the projector proved.

    `Diff` compares the selected version with the ONE predecessor pinned by a
    typed ``supersedes`` link. With no predecessor, or when either exact version
    is unavailable, it is typed unavailable — it never resolves `previous`,
    `current` or `latest`, and it never serializes arbitrary owner JSON.
    """
    record_id = str(record["id"])
    links = _fetch(conn, _LINKS_FROM, {"project_id": project_id, "ids": [record_id]})
    inbound = _fetch(conn, _LINKS_TO, {"project_id": project_id, "ids": [record_id]})

    predecessor: dict[str, Any] | None = None
    approvals: list[dict[str, Any]] = []
    for link in links:
        if link["relation"] == "supersedes" and link.get("to_record_id"):
            pinned = load_record(conn, project_id=project_id, record_id=str(link["to_record_id"]))
            if pinned is not None:
                predecessor = dict(pinned)
        elif link["relation"] == "approved_by":
            approvals.append(
                {
                    "approval_id": link.get("to_owner_object_id"),
                    "owner_workspace": link.get("to_owner_workspace"),
                    "owner_object_type": link.get("to_owner_object_type"),
                }
            )

    used_by = [
        {
            "record_id": str(row["from_record_id"]),
            "relation": str(row["relation"]),
        }
        for row in inbound
        if row["relation"] in {"derives_from", "consumed_by", "supersedes"}
    ]

    if predecessor is None:
        diff = {
            "state": "unavailable",
            "reason_code": "no_pinned_predecessor",
            "message": (
                "No predecessor is pinned to this version by its owner, so there is nothing "
                "exact to compare it with. A plausible earlier version has not been searched for."
            ),
        }
    elif not record.get("integrity_hash") or not predecessor.get("integrity_hash"):
        diff = {
            "state": "unavailable",
            "reason_code": "integrity_reference_missing",
            "message": (
                "One of the two exact versions carries no integrity reference, so no safe "
                "comparison can be stated. The version content itself is not copied here."
            ),
        }
    else:
        # The index compares REFERENCES, never content: identical integrity
        # hashes prove equality, different ones prove change, and the owner
        # workbench is where the field-level difference lives.
        changed = str(record["integrity_hash"]) != str(predecessor["integrity_hash"])
        diff = {
            "state": "available",
            "selected_version_id": record.get("owner_version_id"),
            "predecessor_version_id": predecessor.get("owner_version_id"),
            "predecessor_record_id": str(predecessor["id"]),
            "selected_integrity_hash": record.get("integrity_hash"),
            "predecessor_integrity_hash": predecessor.get("integrity_hash"),
            "changed": changed,
            "owner_href": owner_route(
                str(record["owner_object_type"]),
                str(record["owner_object_id"]),
                record.get("owner_version_id"),
            ),
        }

    return {
        "diff": diff,
        "approvals": {
            "state": "available" if approvals else "empty",
            "items": approvals,
        },
        "used_by": {
            "state": "available" if used_by else "empty",
            "items": used_by,
        },
    }


_AUDIT_DETAIL_SQL = """
    SELECT a.id, a.identity, a.action, a.outcome, a.trace_id, a.operation_id,
           a.policy_version, a.catalog_version, a.tool_version,
           a.before_hash, a.after_hash, a.resource_path, a.created_at
      FROM app.audit_log a
     WHERE a.id = %(audit_id)s
"""


def load_audit_detail(conn: Any, *, record: Mapping[str, Any]) -> dict[str, Any]:
    """The audit owner's own safe columns.

    `provider_account`, `connection_ref`, raw `metadata`, tokens, confirmation
    material and idempotency hashes are NOT selected. Not filtered afterwards —
    not selected, so no later edit can reintroduce them by widening a projection.
    """
    rows = _fetch(conn, _AUDIT_DETAIL_SQL, {"audit_id": str(record["owner_object_id"])})
    if not rows:
        return {
            "state": "unavailable",
            "reason_code": "audit_owner_unavailable",
            "message": (
                "The audited record is no longer readable. Its reference is intact and "
                "nothing has been reconstructed in its place."
            ),
        }
    row = rows[0]
    resource_path = row.get("resource_path")
    return {
        "state": "available",
        "audit_id": str(row["id"]),
        "actor": row.get("identity"),
        "action": row.get("action"),
        "outcome": row.get("outcome"),
        "occurred_at": _iso(row.get("created_at")),
        "resource_path": list(resource_path) if isinstance(resource_path, list) else [],
        "correlation": {
            "trace_id": row.get("trace_id") if is_w3c_trace_id(row.get("trace_id")) else None,
            "operation_id": row.get("operation_id"),
        },
        "versions": {
            "policy_version": row.get("policy_version"),
            "catalog_version": row.get("catalog_version"),
            "tool_version": row.get("tool_version"),
        },
        "hashes": {
            "before_hash": row.get("before_hash"),
            "after_hash": row.get("after_hash"),
        },
    }


def producer_inventory() -> list[dict[str, Any]]:
    """The registry, as data. What the coverage report and the tests both read."""
    return [
        {
            "producer": contract.producer,
            "record_kind": contract.record_kind,
            "owner_workspace": contract.owner_workspace,
            "owner_object_type": contract.owner_object_type,
            "label": contract.label,
            "disposition": contract.disposition,
            "creates_anchor": contract.creates_anchor,
            "reason_code": contract.reason_code,
            "reason": contract.reason,
        }
        for contract in PRODUCERS
    ]


__all__ = [
    "BackfillReport",
    "EvidenceConflict",
    "EvidenceCursorInvalid",
    "EvidenceFilterInvalid",
    "EvidenceIndexError",
    "LENS_RECORD_KIND",
    "LinkSpec",
    "MAX_PAGE",
    "OUTBOX_EVENT_PRODUCERS",
    "Page",
    "PRODUCERS",
    "ProducerContract",
    "ProjectionReport",
    "ReferenceEvent",
    "backfill_producer",
    "backfill_project",
    "decode_cursor",
    "encode_cursor",
    "is_w3c_trace_id",
    "list_records",
    "load_audit_detail",
    "load_availability",
    "load_correlations",
    "load_record",
    "load_trace_graph",
    "load_version_detail",
    "normalize_filters",
    "owner_route",
    "producer_inventory",
    "project_from_outbox",
    "quarantine",
    "record_availability",
    "register_reference",
]
