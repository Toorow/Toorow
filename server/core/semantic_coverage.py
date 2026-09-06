"""Mapping Coverage: a projection of Data-owned mappings (Story 49.3, AC6-AC7).

Governance does not own physical mapping and never writes it. This module reads
what Data has already PUBLISHED and composes one answer to "where is this meaning
actually bound, and how sure are we?".

The distinctions it exists to keep:

* **Active means published.** Coverage counts a binding only when it comes from
  the immutable ``app.datastream_mapping_versions`` row that
  ``app.datastreams.current_mapping_version_id`` points at. The mutable
  ``app.datastream_mappings`` working table, connector defaults and open
  proposals are qualified INPUT and are reported under their own states — they
  never move the numerator.
* **Unavailable is not zero.** When the Data adapter cannot be read, every count
  is ``None`` and the state is ``unavailable``. Returning ``0/12`` for "we could
  not look" is the single most expensive lie this surface could tell.
* **The denominator is enumerated, not assumed.** It is the exact set of
  eligible current Datastream bindings for the selected scope, computed from the
  same snapshot as the numerator, so a page and its totals cannot disagree.
* **No raw values leave.** A source field's identity and path are reported; the
  sampled values behind them are not read and cannot be returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence

#: Coverage states, most-bound first. `active` and `confirmed` are the only two
#: that count toward the numerator; every other state is reported and excluded.
COVERAGE_STATES = (
    "active",
    "confirmed",
    "candidate",
    "suggested",
    "excluded",
    "stale",
    "blocking",
    "unavailable",
)

_COUNTED_STATES = frozenset({"active", "confirmed"})

#: Hard ceiling on one page of binding rows.
MAX_COVERAGE_ROWS = 200


class CoverageUnavailable(RuntimeError):
    """The Data adapter could not be read. Callers must report `unavailable`."""


@dataclass(frozen=True, slots=True)
class BindingRow:
    concept_id: str | None
    concept_version_id: str | None
    concept_name: str | None
    datastream_id: str
    datastream_name: str | None
    mapping_version_id: str
    mapping_version_number: int | None
    source_field_id: str
    source_field_path: str | None
    state: str
    confidence: float | None
    publication_ref: dict[str, Any] | None
    fingerprints: dict[str, Any]
    freshness: dict[str, Any]
    blocking_refs: list[dict[str, Any]]
    provenance: dict[str, Any]
    owner_href: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "concept_version_id": self.concept_version_id,
            "concept_name": self.concept_name,
            "datastream_id": self.datastream_id,
            "datastream_name": self.datastream_name,
            "mapping_version_id": self.mapping_version_id,
            "mapping_version_number": self.mapping_version_number,
            "source_field_id": self.source_field_id,
            "source_field_path": self.source_field_path,
            "state": self.state,
            "confidence": self.confidence,
            "publication_ref": self.publication_ref,
            "fingerprints": self.fingerprints,
            "freshness": self.freshness,
            "blocking_refs": self.blocking_refs,
            "provenance": self.provenance,
            "owner_href": self.owner_href,
        }


@dataclass(slots=True)
class CoverageReport:
    state: str
    rows: list[BindingRow] = field(default_factory=list)
    # EVERY count defaults to None, and none of them to 0. A zero here is a
    # MEASUREMENT -- "we looked, and found none" -- so a report that was built
    # without setting a count would otherwise publish a measurement nobody took.
    # `unavailable_report` did exactly that: it set bound and eligible to None and
    # let `unmapped_datastreams` keep its 0 default, so an unreadable owner
    # answered "0 Datastreams are unmapped" in the same payload that said coverage
    # was unknown. Both construction sites below pass every count explicitly; the
    # default exists only so the next one cannot repeat it.
    bound: int | None = None
    eligible: int | None = None
    #: Datastreams with no published mapping version. Their field population is
    #: unknown, so they are counted here and NEVER inside `eligible`
    #: (`overview.md:124`: every count names its denominator and window).
    unmapped_datastreams: int | None = None
    total_rows: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    unavailable_reason: dict[str, Any] | None = None

    @property
    def truncated(self) -> bool:
        return self.total_rows > len(self.rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "rows": [row.as_dict() for row in self.rows],
            "coverage": {
                "bound": self.bound,
                "eligible": self.eligible,
                "unmapped_datastreams": self.unmapped_datastreams,
                "by_state": dict(self.by_state),
                "returned": len(self.rows),
                "total": self.total_rows,
                "bound_limit": MAX_COVERAGE_ROWS,
                "truncated": self.truncated,
            },
            "unavailable_reason": self.unavailable_reason,
        }


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


# The ONLY mapping source that counts: the immutable version the Datastream
# currently points at. Joining through `current_mapping_version_id` rather than
# taking the newest version is deliberate -- a newer version may exist and not be
# published, and publishing is Data's decision, not this projection's.
_ACTIVE_MAPPING_VERSIONS = """
    SELECT d.id            AS datastream_id,
           d.name          AS datastream_name,
           d.current_published_execution_id,
           v.id            AS mapping_version_id,
           v.version_number,
           v.mapping_payload,
           v.source_schema_hash,
           v.plan_version_id,
           v.capability_fingerprint,
           v.content_hash,
           v.executable,
           v.blocking_count,
           v.created_at    AS mapping_created_at,
           v.created_by    AS mapping_created_by
    FROM app.datastreams d
    JOIN app.datastream_mapping_versions v
      ON v.id = d.current_mapping_version_id
     AND v.datastream_id = d.id
     AND v.project_id = d.project_id
    WHERE d.project_id = %(project_id)s
    ORDER BY d.name, d.id
"""

# Datastreams with NO published mapping version. They are the honest part of the
# denominator: eligible to carry meaning, carrying none yet. Dropping them would
# make coverage read 4/4 while eight Datastreams sit unmapped.
#
# IT SELECTED `d.status`, A COLUMN `app.datastreams` HAS NEVER HAD. Written on
# 2026-07-30 with story 49.3 (`0e154441`) and unmeasured until 2026-08-24: the
# three reads below are wrapped in one `try` that turns any adapter failure into
# `CoverageUnavailable`, so an `UndefinedColumn` raised on EVERY call read as
# "the Data owner could not be reached". Two surfaces answered `unavailable` for
# every Project of every deployment because of it -- the `mapping-coverage` lens,
# and `_concept_coverage`, which swallows the exception and hands every Concept a
# coverage of `unavailable`. Nothing ever consumed the value: the unmapped rows
# are projected with `datastream_name` alone. The near-neighbour column is
# `lifecycle_state`, and it is deliberately NOT substituted -- adding a state to
# the projection is a design decision, removing a phantom is the repair.
_UNMAPPED_DATASTREAMS = """
    SELECT d.id AS datastream_id, d.name AS datastream_name
    FROM app.datastreams d
    WHERE d.project_id = %(project_id)s
      AND d.current_mapping_version_id IS NULL
    ORDER BY d.name, d.id
"""

# Proposals are qualified INPUT, never coverage. Read separately so a candidate
# can be shown next to the active binding it would replace without ever being
# counted as one.
_OPEN_PROPOSALS = """
    SELECT mp.id, mp.datastream_id, mp.mapping_version_id, mp.state, mp.mode,
           mp.checks, mp.created_at,
           d.name AS datastream_name
    FROM app.mapping_proposals mp
    LEFT JOIN app.datastreams d
      ON d.id = mp.datastream_id AND d.project_id = mp.project_id
    WHERE mp.project_id = %(project_id)s
      AND mp.state NOT IN ('applied', 'rejected', 'expired')
    ORDER BY mp.created_at DESC, mp.id
"""


def _fetch(conn: Any, query: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _binding_state(binding: Mapping[str, Any], *, executable: bool, blocking: int) -> str:
    """Translate the Data-owned binding status into a coverage state.

    Data's vocabulary and Governance's are not the same, and collapsing them
    would erase the difference between "a person confirmed this" and "a profiler
    guessed it".
    """
    status = str(binding.get("status") or "suggested").lower()
    if binding.get("excluded") or status == "excluded":
        return "excluded"
    if blocking > 0 and status in {"blocked", "conflict"}:
        return "blocking"
    if status in {"confirmed", "resolved"}:
        return "active" if executable else "stale"
    if status == "candidate":
        return "candidate"
    if status in {"blocked", "conflict"}:
        return "blocking"
    return "suggested"


def _owner_href(datastream_id: str, version_id: str | None) -> dict[str, Any]:
    """The canonical Data address that OWNS this binding. Governance links; the
    editor lives there and only there."""
    from core.project_overview import owner_reference  # noqa: PLC0415 -- shared contract

    return owner_reference(
        "data",
        "datastreams",
        object_type="datastream",
        object_id=datastream_id,
        tab="mapping",
        version_id=version_id,
    )


def compose_mapping_coverage(
    conn: Any,
    project_id: str,
    *,
    concept_ids: Sequence[str] | None = None,
    concept_names: Mapping[str, str] | None = None,
    datastream_id: str | None = None,
    states: Iterable[str] | None = None,
    limit: int = MAX_COVERAGE_ROWS,
    offset: int = 0,
) -> CoverageReport:
    """Project the active Data mappings of one Project into coverage rows.

    ``concept_names`` maps a Concept id to its machine name so a payload that
    bound a *name* (every mapping written before migration 142) still resolves to
    the Concept that now owns that name. A binding whose target resolves to
    neither an id nor a known name keeps ``concept_id=None``: it is reported as
    an unattributed binding rather than attached to a plausible neighbour.
    """
    wanted_concepts = set(concept_ids or ())
    name_to_concept = {name: cid for cid, name in (concept_names or {}).items()}
    wanted_states = set(states or ()) or None

    try:
        active = _fetch(conn, _ACTIVE_MAPPING_VERSIONS, {"project_id": project_id})
        unmapped = _fetch(conn, _UNMAPPED_DATASTREAMS, {"project_id": project_id})
        proposals = _fetch(conn, _OPEN_PROPOSALS, {"project_id": project_id})
    except Exception as exc:  # noqa: BLE001 -- the adapter, not the data, failed
        raise CoverageUnavailable(str(exc)) from exc

    rows: list[BindingRow] = []
    by_state: dict[str, int] = {state: 0 for state in COVERAGE_STATES}
    bound_pairs: set[tuple[str, str]] = set()
    eligible_pairs: set[tuple[str, str]] = set()

    for record in active:
        payload = record.get("mapping_payload") or {}
        fields = payload.get("fields") if isinstance(payload, Mapping) else None
        executable = bool(record.get("executable"))
        blocking = int(record.get("blocking_count") or 0)
        fingerprints = {
            "source_schema_hash": record.get("source_schema_hash"),
            "plan_version_id": record.get("plan_version_id"),
            "capability_fingerprint": record.get("capability_fingerprint"),
            "content_hash": record.get("content_hash"),
        }
        freshness = {
            "mapping_published_at": _iso(record.get("mapping_created_at")),
            "published_execution_id": record.get("current_published_execution_id"),
            # A mapping with no published execution behind it is bound but not
            # yet proven by data. Saying so is different from saying it is stale.
            "has_published_execution": bool(record.get("current_published_execution_id")),
        }
        datastream_id_value = str(record["datastream_id"])
        if datastream_id and datastream_id_value != datastream_id:
            continue

        for entry in fields or ():
            if not isinstance(entry, Mapping):
                continue
            binding = entry.get("binding") if isinstance(entry.get("binding"), Mapping) else {}
            profile = entry.get("profile") if isinstance(entry.get("profile"), Mapping) else {}
            target_id = binding.get("mdm_target")
            target_name = binding.get("canonical_target")
            concept_id = (
                str(target_id)
                if target_id
                else name_to_concept.get(str(target_name)) if target_name else None
            )
            if wanted_concepts and concept_id not in wanted_concepts:
                continue
            state = _binding_state(binding, executable=executable, blocking=blocking)
            # ONE population on both sides of the fraction: the eligible
            # (datastream, field) pair. The numerator used to key on the CONCEPT
            # instead, so two fields bound to the same concept gave bound=1,
            # eligible=2 and the fraction could not reach 100% however complete the
            # mapping was. A ratio whose ceiling is unreachable is not a measure.
            field_pair = (datastream_id_value, str(entry.get("field_id")))
            eligible_pairs.add(field_pair)
            if state in _COUNTED_STATES and concept_id:
                bound_pairs.add(field_pair)
            by_state[state] = by_state.get(state, 0) + 1
            if wanted_states and state not in wanted_states:
                continue
            rows.append(
                BindingRow(
                    concept_id=concept_id,
                    concept_version_id=binding.get("concept_version_id"),
                    concept_name=str(target_name) if target_name else None,
                    datastream_id=datastream_id_value,
                    datastream_name=record.get("datastream_name"),
                    mapping_version_id=str(record["mapping_version_id"]),
                    mapping_version_number=record.get("version_number"),
                    source_field_id=str(entry.get("field_id") or ""),
                    # The PATH, never the values behind it. Nothing in this
                    # module reads a sample.
                    source_field_path=entry.get("source_path") or entry.get("field_id"),
                    state=state,
                    confidence=(
                        float(profile["confidence"])
                        if isinstance(profile.get("confidence"), (int, float))
                        else None
                    ),
                    publication_ref=(
                        {
                            "kind": "datastream-execution",
                            "id": record.get("current_published_execution_id"),
                        }
                        if record.get("current_published_execution_id")
                        else None
                    ),
                    fingerprints=fingerprints,
                    freshness=freshness,
                    blocking_refs=[],
                    provenance={
                        "mapping_created_by": record.get("mapping_created_by"),
                        "binding_status": binding.get("status"),
                        "semantic_role": (entry.get("suggestion") or {}).get("semantic_role")
                        if isinstance(entry.get("suggestion"), Mapping)
                        else None,
                    },
                    owner_href=_owner_href(
                        datastream_id_value, str(record["mapping_version_id"])
                    ),
                )
            )

    # Datastreams with no published mapping. Named and counted -- in their OWN
    # number, never inside a fraction counted in fields (see `eligible` below).
    counted_unmapped: set[str] = set()
    for record in unmapped:
        datastream_id_value = str(record["datastream_id"])
        if datastream_id and datastream_id_value != datastream_id:
            continue
        counted_unmapped.add(datastream_id_value)
        by_state["unavailable"] = by_state.get("unavailable", 0) + 1
        if wanted_states and "unavailable" not in wanted_states:
            continue
        # NO concept filter here, and that is the point. A field bound to another
        # Concept is outside the question a concept-scoped read asks, so it is
        # dropped from the rows AND from `by_state`. A Datastream that publishes no
        # mapping at all is outside NO question: its field population is unknown,
        # so whether it carries THIS Concept is unknown too. It used to be dropped
        # from the rows while its count was kept in `by_state` and in
        # `unmapped_datastreams`, which is the shape of the concept workbench: the
        # state filter offered "Unavailable (8)" and selecting it showed nothing,
        # and the eight Datastreams the count referred to could not be named.
        rows.append(
            BindingRow(
                concept_id=None,
                concept_version_id=None,
                concept_name=None,
                datastream_id=datastream_id_value,
                datastream_name=record.get("datastream_name"),
                mapping_version_id="",
                mapping_version_number=None,
                source_field_id="",
                source_field_path=None,
                state="unavailable",
                confidence=None,
                publication_ref=None,
                fingerprints={},
                freshness={"has_published_execution": False},
                blocking_refs=[],
                provenance={
                    # It is NOT in the denominator, and saying so was left over from
                    # the arithmetic CAV-12 removed. `eligible` counts (Datastream,
                    # field) pairs; this Datastream publishes no mapping, so it
                    # contributes no field and is counted beside the fraction.
                    "reason": "This Datastream has no published mapping version, so it "
                    "carries no meaning yet. Its field population is unknown, so it is "
                    "counted beside the fraction and never inside it."
                },
                owner_href=_owner_href(datastream_id_value, None),
            )
        )

    # Open proposals attach to the row they would replace as a blocking/candidate
    # reference. They are never rows of their own and never counted.
    proposals_by_datastream: dict[str, list[dict[str, Any]]] = {}
    for record in proposals:
        proposals_by_datastream.setdefault(str(record["datastream_id"]), []).append(
            {
                "kind": "mapping-proposal",
                "id": str(record["id"]),
                "state": record.get("state"),
                "mode": record.get("mode"),
                "recorded_at": _iso(record.get("created_at")),
                # The decision flow is Story 49.4's. This is a link, not a verdict.
                "decision_owner": "governance/controls-quality",
            }
        )
    for index, row in enumerate(rows):
        pending = proposals_by_datastream.get(row.datastream_id)
        if pending:
            rows[index] = replace(row, blocking_refs=pending)

    total = len(rows)
    window = rows[offset : offset + max(0, min(limit, MAX_COVERAGE_ROWS))]
    return CoverageReport(
        state="available" if rows else "empty",
        rows=window,
        bound=len(bound_pairs),
        # Unmapped Datastreams are NOT folded in. They have no published mapping,
        # so their field population is unknown -- adding 1 per Datastream to a
        # denominator counted in fields is the category error that made this
        # fraction mix three populations. They are reported beside it instead, so
        # the surface still cannot read 4/4 while eight Datastreams sit unmapped
        # (the reason the header gives for including them at all).
        eligible=len(eligible_pairs),
        unmapped_datastreams=len(counted_unmapped),
        total_rows=total,
        by_state={state: count for state, count in by_state.items() if count},
    )


def unavailable_report(reason: str) -> CoverageReport:
    """The shape every caller must produce when the Data adapter cannot be read.
    Counts are None, never 0: a number here would be read as a measurement.

    ALL THREE counts, not two. `unmapped_datastreams` used to fall through to its
    0 default here, so the payload that refused to say "0 bound" still said "0
    Datastreams are unmapped" -- a population that was never read, rendered as a
    population that is empty. It is the same lie the module header refuses, on the
    count that was added last.
    """
    return CoverageReport(
        state="unavailable",
        rows=[],
        bound=None,
        eligible=None,
        unmapped_datastreams=None,
        total_rows=0,
        by_state={},
        unavailable_reason={
            "code": "mapping_owner_unreadable",
            "message": "The Data mapping owner could not be read, so coverage is "
            "unknown for this Project. This is not a coverage of zero.",
            "detail": reason,
        },
    )
