"""Story 71.4 -- the FIRST CALLER of Analytics Alignment, and of the measurement grain.

WHAT THIS MODULE IS. One composition, and no new engine. `analytics_alignment`
owns the cascade and the two stored acts; `analytics_ventilation` owns the split
and its conservation; `metric_dimensions` owns the governed grain. Until this
file, **`run_cascade` and `ventilate` had no caller at all** and no consumer
SLICED by a measurement grain -- `alignable_pairs` was already read, by
`capability_compilers.AnalyticsAlignmentCompiler:1705`, and the grain already had
readers on the Governance surfaces (`metric_dimensions_api`, `admin_api`,
`datastream_workbench` and its API). What did not exist is a caller that runs the
cascade and cuts a metric by a governed grain. This composes the three for the ONE
surface `capabilities/analytics-alignment.md` names for MCP -- the bounded block on
the generic capability read -- and adds no third opinion about anything the three
already decide.

THE GRAIN SANCTIONS THE BREAKDOWN, and that is why this file exists at all.
`governance.md` § *Amendment, 2026-08-27 -- the fourth fact* closes its
`Incomplete if` with the clause this module answers: *"analytics-alignment slices
a metric by a dimension no governed grain relates to it, presenting a breakdown
the MDM never sanctioned."* :func:`sanction_breakdown` is the refusal. A
breakdown is served only when ONE live grain version of that metric names every
requested dimension, and the payload then carries the grain id AND the version id
it was cut by -- because *"a consumer pins a grain without a version"* is the
other clause of the same list.

WHY A CONSUMER PIN IS NOT A ROW. `metric_dimensions.grain_used_by` reads what
PINS a grain version: the `master_data_refs` of a published Semantic View or
Concept version. A read persists nothing, so this caller writes no pin and
`grain_used_by` is unchanged by it. The version travels on the payload
(`sliced_by`), where the reader that asked for the slice can see it. Anybody
looking for an `analytics_alignment` row in a used-by table will not find one,
and that is the design and not a gap.

THE OBSERVED ROWS COME FROM THE OBSERVED-ENTITY REGISTRY, and this module is the
caller story 70.2 said would arrive. `capabilities/analytics-alignment.md` §2
names the source in its own words -- the registry is `core.observed_entities`,
mounted on the generic Master Data owner -- and its `Incomplete if` said "no
caller reaching the registry yet (story 70.3 projects the capability that will
consume it)". This is that consumer.

  * `observed_entities.list_attachments(conn, project_id=...)` returns every LIVE
    binding of the Project. Each row carries the Datastream it was observed on in
    `provenance_reference` (written verbatim at `observed_entities.py:561`, and
    repeated inside `evidence` at :562), the governed `node_id`, and the
    hierarchical path as `raw_value`.
  * the path is decoded with `observed_entities.path_from_mapping`, whose
    `entity_id` property is *the id at this path's own level* -- exactly what the
    cascade's `id_exact` stage compares.
  * the name is the governed node's LABEL, read from
    `master_data.list_nodes(...)` and never re-derived here: a label recomputed
    beside the one Master Data stores is a second place for it to be contradicted.

WHAT THE REGISTRY CANNOT GIVE, SAID IN THE PAYLOAD AND NOT SWALLOWED. An
attachment binds a PATH to a node; it carries no value of a common-key component,
so `AlignmentSide.entity_key` is `None` and the payload says
``entity_key: "not_derivable"`` with the reason. The `key_exact` stage therefore
proposes nothing on a registry-built reading -- which is a stage that had nothing
to compare, not a stage that found no match. The mapping binding that designates
an entity column (`fields[].binding.designates_object_kind`,
`object_kind_registry.entity_designations`) is counted per side so a reader can
see the wire the attachment rode in on. The registry carries no metric either --
`analytics_alignment.py` says of itself "four fields and no metric" -- so a
registry-built reading ventilates nothing and states why.

A caller that HAS fuller rows (the future Analyze surface) passes an
:class:`ObservedReading` and that is used instead, untouched.

NEVER A ZERO WHERE NOTHING WAS OBSERVED. A Datastream of the pair with zero live
attachments produces ``reading: {state: "unavailable", code:
"no_observed_entity_attached"}`` NAMING that Datastream, and the block then
carries NO ``alignment_counts``. Four zeros over a population nobody attached
would be an alignment nobody ran -- the exact fault `alignment_counts` exists to
prevent when it returns `unmatched` at zero *for a run that happened*. A registry
read that FAILED is `observed_entity_registry_unreadable`, kept apart from an
empty one for the reason `metric_dimensions.derived_coverage` keeps them apart:
"I could not look" is not "nobody attached anything".

OFF NAMES NOTHING. On `disabled`, `draft`, `blocked` and `unset` this returns a
block that does not contain one of the three added column names, one ventilated
column name, a pair, or a count. « Éteinte, la capacité n'apparaît nulle part. »
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Mapping, Sequence

from core.analytics_alignment import (
    ANALYTICS_ALIGNMENT_CAPABILITY_KEY,
    DECISION_ARBITRATED,
    METHOD_HUMAN_ARBITRATION,
    NO_ALIGNABLE_PAIR,
    AlignedRow,
    AlignmentCurrencies,
    AlignmentRefused,
    AlignmentSide,
    added_columns,
    alignable_pairs,
    alignment_counts,
    list_alignment_decisions,
    resolve_dependencies,
    run_cascade,
)
from core.analytics_ventilation import (
    VentilationBasis,
    VentilationGroup,
    VentilationOutcome,
    list_ventilation_weights,
    ventilate,
    ventilated_rows,
    ventilation_columns,
    ventilation_counts,
)
from core.plan_matching_states import (
    MATCHING_STATE_AMBIGUOUS,
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_UNMATCHED,
)
from core.project_capability_states import capability_is_active

logger = logging.getLogger(__name__)

#: The payload shape, versioned like every other MCP structuredContent schema.
ALIGNMENT_READ_SCHEMA = "analytics_alignment_read.v1"

#: The row bound, on the `_bounded_matrix` patron of `project_capabilities_mcp`:
#: a list is capped and the TRUE count travels beside it, so a cap is never read
#: as a total.
#:
#: SIX AND NOT TWELVE, and the number is measured rather than chosen.
#: `model_channel.MAX_BOUNDED_EVIDENCE_FIGURES` is twelve, and twelve is the right
#: bound for a list of short figures. Here a pair row is ~172 bytes and a decision
#: row ~186, so twelve of each is 4.3 KB -- past `MODEL_CHANNEL_MAX_BYTES` (4096)
#: on those two lists alone, with the dependencies, the reading and the headline
#: still to come. At six, the block a Project without attachments produces
#: measures 3 682 bytes and is read by the model whole.
#:
#: A block that DID run the cascade measures ~5.6 KB at the same bound, and that is
#: not a bound to tighten further: AC1 of story 71.4 requires every `unmatched` row
#: listed, every `ambiguous` row with its candidates and every `matched` row with
#: its method, and an aligned row is ~250 bytes. Any cap that fit those three lists
#: into 4 KB would show two rows and call it a worklist. So that shape rides the
#: SHARED guard -- `model_channel.partition_envelope` routes the largest subtree to
#: the app channel and leaves a STATED descriptor: nothing discarded, nothing
#: silently trimmed. Measured on the real envelope (review round 2): the WHOLE
#: `foundation` moves, and the descriptor carries no count -- the counts survive
#: in the TEXT channel (the tool's summary line); `structuredContent` keeps only
#: the descriptor.
#:
#: The shared guard is the enforcer -- `enforce_result_model_channel` runs on EVERY
#: returning tool and a second budget check here would be a second authority for one
#: rule. This is the courtesy bound that keeps the cheap shapes inside it.
MAX_LISTED_ROWS = 6

#: Where a registry-built reading came from. Named on the payload, because a
#: reader who cannot tell which source produced the two sides cannot defend the
#: counts: the registry gives identities and no metric, and a fuller reading
#: handed in by a surface gives both.
ROWS_FROM_REGISTRY = "observed_entity_registry"
ROWS_FROM_CALLER = "caller_supplied_reading"

#: A Datastream of the pair carries NO live observed-entity attachment. The
#: absence with its cause -- never an empty side quietly aligned against a full
#: one, which would report every row of the other side as `unmatched` and call
#: that the work.
NO_ATTACHMENTS = "no_observed_entity_attached"

#: The registry could not be READ. Kept apart from the empty case for the reason
#: `metric_dimensions.derived_coverage` keeps its own two apart: "I could not
#: look" is not "nobody attached anything", and only one of them invites a person
#: to go and attach something.
REGISTRY_UNREADABLE = "observed_entity_registry_unreadable"

ATTACH_GESTURE = (
    "Attach the observed entities of that Datastream to governed nodes in the "
    "observed-entity registry (Governance > Master Data). The registry and its "
    "attachment command exist (`core.observed_entities.attach_observed_entity`); no API, "
    "no screen and no MCP tool writes an attachment yet, so this gesture is available "
    "only to a caller of that function today."
)

#: Why `entity_key` is empty on every side this module builds. An attachment binds
#: a PATH to a node and carries no value of a common-key component, so the
#: `key_exact` stage has nothing to compare -- which is different from finding no
#: match, and the payload says which.
ENTITY_KEY_NOT_DERIVABLE = "not_derivable"

ENTITY_KEY_NOT_DERIVABLE_REASON = (
    "An observed-entity attachment binds a hierarchical path to a governed node; it "
    "carries no value of a common-key component. So the declared-key stage of the "
    "cascade had nothing to compare on this reading -- it did not fail to match."
)

# --- the grain refusals ----------------------------------------------------

#: The metric is not the head of any live measurement grain, so nothing governs
#: what it may be cut by. Distinct from the refusal below: there is no grain at
#: all, so the gesture is to DECLARE one.
REFUSAL_NO_GOVERNED_GRAIN = "metric_has_no_governed_grain"

#: The clause of `governance.md` § Amendment 2026-08-27 this module answers.
REFUSAL_DIMENSION_NOT_IN_GRAIN = "breakdown_dimension_not_in_grain"

#: Every dimension IS governed for this metric, but by more than one grain, and no
#: single live version names them all. Refused rather than served from the union:
#: a union of two grains is a grain nobody declared, and slicing by it is exactly
#: the breakdown the MDM never sanctioned. It is a third code and not a variant of
#: the one above, because the gesture differs -- here a version already exists to
#: extend, there one has to be declared.
REFUSAL_BREAKDOWN_SPANS_GRAINS = "breakdown_spans_two_grains"

#: A breakdown that names NO dimension is a request for a TOTAL, and a total is a
#: declared object: the grain of zero dimensions this amendment calls legal.
#: Without one, `all([])` is true of every grain and the payload would pin
#: whichever sorted first -- a contract nobody chose, presented as governed.
REFUSAL_TOTAL_NOT_DECLARED = "breakdown_total_not_declared"

#: The metric names a grain, the grain names the dimensions, and the reading that
#: would carry the figures is not there. Stated, never rendered as zero.
VALUES_UNAVAILABLE = "breakdown_values_unavailable"

#: The two Datastreams of the pair publish observed entities of the SAME platform.
#: Story 70.2 keys an attachment by PATH and a path begins with its platform, so
#: an entity both sides observe is ONE alias row carrying whichever Datastream
#: attached it first -- the other side simply does not have it, and its rows read
#: `unmatched` for a reason that is not about the data.
SAME_PLATFORM_PAIR = "same_platform_pair"


# ---------------------------------------------------------------------------
# What a caller hands in. Nothing here is read from a warehouse by this module.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedReading:
    """The two sides of one pair, as a caller that HAS them presents them.

    Every field is what the caller observed, and this module neither completes nor
    defaults one. ``currencies`` is optional exactly as
    :class:`core.analytics_alignment.AlignmentCurrencies` is optional: a reading
    that states no currency makes no claim about money, and the moment it states
    them an unconverted side is REFUSED rather than warned.

    ``basis`` is likewise required only to ventilate. There is no default volume
    and no equal parts -- `analytics_ventilation` refuses both by name -- so a
    reading with candidates and no basis simply does not split, and says so.
    """

    left: tuple[AlignmentSide, ...] = ()
    right: tuple[AlignmentSide, ...] = ()
    currencies: AlignmentCurrencies | None = None
    #: The metrics each LEFT row published, as measured. Read, never written.
    observed_by_row: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: One coarse key per day, with the candidates that answer it.
    groups: tuple[VentilationGroup, ...] = ()
    basis: VentilationBasis | None = None
    day: str | None = None


# ---------------------------------------------------------------------------
# Bounding. The cap is stated, never silent.
# ---------------------------------------------------------------------------


def bounded(rows: Sequence[Any], cap: int = MAX_LISTED_ROWS) -> tuple[list[Any], int]:
    """``(head, withheld)`` -- always both, on `_bounded_matrix`'s patron.

    A silent cap reads as coverage: twelve unmatched rows shown out of two hundred
    is a worklist somebody would call finished.
    """
    items = list(rows)
    head = items[: max(0, cap)]
    return head, max(0, len(items) - len(head))


def _listing(rows: Sequence[Any], cap: int = MAX_LISTED_ROWS) -> dict[str, Any]:
    head, withheld = bounded(rows, cap)
    return {"items": head, "count": len(rows), "withheld": withheld}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _canonical_hash(payload: Any) -> str:
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


# ---------------------------------------------------------------------------
# The grain sanction -- epic 71's first caller.
# ---------------------------------------------------------------------------


def _live_grain_versions(conn, *, project_id: str, metric_field_id: str) -> list[dict[str, Any]]:
    """Every ACTIVE grain whose CURRENT version is headed by this metric.

    The current version and not every version: an older version is superseded, and
    slicing by a superseded contract would pin a grain to a meaning its owner has
    already replaced. `list_measurement_grains` orders by status then name, so the
    order below is deterministic and a re-read picks the same grain.
    """
    from core.metric_dimensions import list_measurement_grains  # noqa: PLC0415

    live: list[dict[str, Any]] = []
    for grain in list_measurement_grains(conn, project_id=project_id):
        if _text(grain.get("status")) != "active":
            continue
        version = grain.get("current_version") or None
        if not version:
            continue
        head = version.get("head") or {}
        if _text(head.get("canonical_field_id")) != metric_field_id:
            continue
        live.append(grain)
    return live


def _member_ids(version: Mapping[str, Any]) -> list[str]:
    return [
        _text((member or {}).get("canonical_field_id"))
        for member in version.get("members") or []
    ]


def sanction_breakdown(
    conn, *, project_id: str, metric_field_id: str, dimension_field_ids: Sequence[str]
) -> dict[str, Any]:
    """Does a governed grain relate these dimensions to this metric? Refuse by name.

    Three refusals and one sanction, and the three are kept apart because their
    gestures are three different acts:

      * :data:`REFUSAL_NO_GOVERNED_GRAIN` -- the metric heads no live grain. Declare
        one.
      * :data:`REFUSAL_DIMENSION_NOT_IN_GRAIN` -- a requested dimension appears in no
        live version of any grain this metric heads. It is named, with the metric
        and with the grain versions that DO exist, so the refusal says what the
        governed answer is instead of only that the request was wrong.
      * :data:`REFUSAL_BREAKDOWN_SPANS_GRAINS` -- each dimension is governed, but by
        different grains, so no ONE version sanctions the cut. Serving it from the
        union would present a grain nobody declared.

    A sanctioned breakdown returns ``sliced_by`` carrying the grain id AND the
    version id, never the id alone.
    """
    metric = _text(metric_field_id)
    wanted = [_text(item) for item in dimension_field_ids or []]
    requested = {"metric": metric, "dimensions": list(wanted)}
    if not metric:
        return {
            "requested": requested,
            "sanctioned": False,
            "refusal": {
                "code": REFUSAL_NO_GOVERNED_GRAIN,
                "reason": "A breakdown names the metric it cuts. None was named.",
                "gesture": "Name the canonical metric field the breakdown is measured on.",
            },
        }

    grains = _live_grain_versions(conn, project_id=project_id, metric_field_id=metric)
    if not grains:
        return {
            "requested": requested,
            "sanctioned": False,
            "refusal": {
                "code": REFUSAL_NO_GOVERNED_GRAIN,
                "reason": (
                    f"No live measurement grain is headed by {metric}, so nothing in the "
                    f"MDM states which dimensions this measure is reported against."
                ),
                "gesture": (
                    "Declare a measurement grain for this metric in Governance > Master "
                    "Data, naming the dimensions it is reported against, then read again."
                ),
            },
        }

    existing_versions = [
        {
            "measurement_grain_id": grain["id"],
            "measurement_grain_name": grain.get("name"),
            "version_id": (grain.get("current_version") or {}).get("id"),
            "dimensions": _member_ids(grain.get("current_version") or {}),
        }
        for grain in grains
    ]
    governed: set[str] = set()
    for entry in existing_versions:
        governed.update(entry["dimensions"])

    ungoverned = [item for item in wanted if item not in governed]
    if ungoverned:
        return {
            "requested": requested,
            "sanctioned": False,
            "grain_versions": existing_versions,
            "refusal": {
                "code": REFUSAL_DIMENSION_NOT_IN_GRAIN,
                "reason": (
                    f"No live measurement grain relates {', '.join(ungoverned)} to {metric}. "
                    f"The grain version(s) that do exist for it are "
                    f"{', '.join(str(entry['version_id']) for entry in existing_versions)}."
                ),
                "gesture": (
                    "Append a version to the measurement grain of this metric that names "
                    f"{', '.join(ungoverned)}, then read again. Adding a dimension is a new "
                    "version, never an edit, so a metric already sliced a given way keeps "
                    "meaning what it meant."
                ),
                "metric": metric,
                "dimensions_not_in_any_grain": ungoverned,
            },
        }

    if not wanted:
        # A TOTAL, and only a declared total serves it. `all([])` is true of every
        # grain, so without this the payload would pin whichever sorted first.
        total = next(
            (
                (grain, entry)
                for grain, entry in zip(grains, existing_versions)
                if not entry["dimensions"]
            ),
            None,
        )
        if total is None:
            return {
                "requested": requested,
                "sanctioned": False,
                "grain_versions": existing_versions,
                "refusal": {
                    "code": REFUSAL_TOTAL_NOT_DECLARED,
                    "reason": (
                        f"The breakdown names no dimension, which asks for a total of "
                        f"{metric}, and no live grain of this metric declares zero "
                        f"dimensions. Every grain it heads is cut by something."
                    ),
                    "gesture": (
                        "Declare a measurement grain for this metric with no dimension -- a "
                        "measure reported at no breakdown is a total -- then read again."
                    ),
                    "metric": metric,
                },
            }
        grains, existing_versions = [total[0]], [total[1]]

    for grain, entry in zip(grains, existing_versions):
        if all(item in entry["dimensions"] for item in wanted):
            from core.metric_dimensions import derived_coverage  # noqa: PLC0415

            version = grain.get("current_version") or {}
            coverage = derived_coverage(
                conn,
                project_id=project_id,
                head=version.get("head") or {},
                members=version.get("members") or [],
            )
            return {
                "requested": requested,
                "sanctioned": True,
                # A grain id WITHOUT its version is the clause the amendment
                # refuses by name. Both, always, and never one.
                "sliced_by": {
                    "measurement_grain_id": grain["id"],
                    "version_id": _text(version.get("id")),
                },
                "measurement_grain_name": grain.get("name"),
                "coverage": _bounded_coverage(coverage),
            }

    return {
        "requested": requested,
        "sanctioned": False,
        "grain_versions": existing_versions,
        "refusal": {
            "code": REFUSAL_BREAKDOWN_SPANS_GRAINS,
            "reason": (
                f"Every requested dimension is governed for {metric}, but no single live "
                f"grain version names them all, so no declaration sanctions this cut. "
                f"Serving it from two grains would present a grain nobody declared."
            ),
            "gesture": (
                "Append a version to one of the measurement grains of this metric naming "
                "every dimension of the breakdown, then read again."
            ),
            "metric": metric,
        },
    }


def _bounded_coverage(coverage: Mapping[str, Any]) -> dict[str, Any]:
    """The grain's coverage, bounded -- and `partial` shown as the partial it is.

    The counts travel whole (they are four integers) and the per-Datastream rows
    are capped with their remainder stated. The partial Datastreams are listed
    FIRST and separately, because a partial folded into a total reads as covered,
    which is the clause `governance.md:84` already refuses for concept coverage.
    """
    if _text(coverage.get("state")) != "available":
        return {
            "state": _text(coverage.get("state")) or "unavailable",
            "reason": (
                "The grain's coverage could not be read. 'I could not look' is not "
                "'nobody binds it', and only one of the two invites a person to map "
                "something."
            ),
        }
    datastreams = list(coverage.get("datastreams") or [])
    partial = [row for row in datastreams if _text(row.get("coverage")) == "partial"]
    return {
        "state": "available",
        "counts": dict(coverage.get("counts") or {}),
        "partial_datastreams": _listing(partial),
        "unknown_datastreams": _listing(list(coverage.get("unknown_datastreams") or [])),
    }


# ---------------------------------------------------------------------------
# The observed rows, read from the registry story 70.2 built for them.
# ---------------------------------------------------------------------------


def _attachments_by_datastream(
    conn, *, project_id: str, datastream_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Every LIVE attachment of this Project, indexed by the Datastream it rode in on.

    ``provenance_reference`` is the column: ``_insert_attachment`` writes the
    ``datastream_id`` there verbatim (`observed_entities.py:561`) and repeats it
    inside ``evidence`` one line further down. The column is read first and the
    evidence is the fallback, because a JSON key is easier to lose than a column.
    """
    from core.observed_entities import list_attachments  # noqa: PLC0415

    wanted = {_text(item) for item in datastream_ids if _text(item)}
    indexed: dict[str, list[dict[str, Any]]] = {item: [] for item in wanted}
    for row in list_attachments(conn, project_id=project_id):
        origin = _text(row.get("provenance_reference"))
        if not origin:
            evidence = row.get("evidence")
            if isinstance(evidence, Mapping):
                origin = _text(evidence.get("datastream_id"))
        if origin in indexed:
            indexed[origin].append(row)
    return indexed


def _node_labels(conn, *, project_id: str) -> dict[str, str]:
    """node id -> the label MASTER DATA stores. Never re-derived here.

    A label recomputed beside the one `master_data` holds is a second place for it
    to be contradicted -- the rule story 70.1 set for the schema-split provenance
    names and 70.3 set for the three added columns.
    """
    from core.master_data import list_nodes  # noqa: PLC0415
    from core.observed_entities import fetch_observed_entity_registry  # noqa: PLC0415

    registry = fetch_observed_entity_registry(conn, project_id=project_id)
    if not registry:
        return {}
    return {
        _text(node.get("id")): _text(node.get("label"))
        for node in list_nodes(conn, project_id=project_id, registry_id=registry["id"])
    }


def _sides_from_attachments(
    attachments: Sequence[Mapping[str, Any]], labels: Mapping[str, str]
) -> tuple[tuple[AlignmentSide, ...], int, frozenset[str]]:
    """One :class:`AlignmentSide` per attachment, and how many could not be read.

    ``row_key`` is the attachment's ``raw_value`` -- the canonical path, which is
    the identity story 70.2 chose precisely because an entity id alone is not one.
    ``entity_id`` is the path's own-level id, which is what `id_exact` compares.
    ``entity_key`` is ``None``: see :data:`ENTITY_KEY_NOT_DERIVABLE_REASON`.

    The third value is the set of PLATFORMS these attachments name. It is returned
    rather than recomputed by the caller because the platform is the first
    component of the path and this is the only place the path is decoded --
    :data:`SAME_PLATFORM_PAIR` is decided on it.
    """
    from core.observed_entities import ObservedEntityError, path_from_mapping  # noqa: PLC0415

    sides: list[AlignmentSide] = []
    unreadable = 0
    platforms: set[str] = set()
    for row in attachments:
        evidence = row.get("evidence")
        try:
            path = path_from_mapping(evidence if isinstance(evidence, Mapping) else {})
        except (ObservedEntityError, ValueError):
            # Counted, never dropped in silence: an attachment this reader cannot
            # decode is a row nobody will see in the alignment, and a count is the
            # only way that becomes visible.
            unreadable += 1
            continue
        platforms.add(path.platform)
        node_id = _text(row.get("node_id"))
        sides.append(
            AlignmentSide(
                row_key=_text(row.get("raw_value")) or path.canonical_key,
                entity_id=path.entity_id or None,
                entity_key=None,
                entity_name=labels.get(node_id) or None,
            )
        )
    return tuple(sides), unreadable, frozenset(platforms)


def _designating_column_counts(
    conn, *, project_id: str, datastream_ids: Sequence[str]
) -> dict[str, int]:
    """How many columns of each Datastream's PUBLISHED mapping designate an object kind.

    The wire an attachment rides in on (`fields[].binding.designates_object_kind`,
    read through `object_kind_registry.entity_designations` and not re-parsed
    here). It is a count and not a list: a reader needs to know the wire exists,
    and the columns themselves belong to the mapping surface that owns them.
    """
    from core.object_kind_registry import entity_designations  # noqa: PLC0415

    wanted = [_text(item) for item in datastream_ids if _text(item)]
    if not wanted:
        return {}
    counts = {item: 0 for item in wanted}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, m.mapping_payload
                  FROM app.datastreams d
                  LEFT JOIN app.datastream_mapping_versions m
                         ON m.id = d.current_mapping_version_id
                 WHERE d.project_id = %s AND d.id = ANY(%s)
                """,
                (_text(project_id), wanted),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- a count that could not be read is absent
        logger.warning("alignment read: designation count failed: %s", exc)
        return {}
    for datastream_id, payload in rows:
        counts[_text(datastream_id)] = len(
            entity_designations(payload if isinstance(payload, Mapping) else None)
        )
    return counts


def registry_reading(
    conn, *, project_id: str, pair: Mapping[str, Any]
) -> tuple[ObservedReading | None, dict[str, Any]]:
    """Build the two sides of *pair* from the observed-entity registry.

    Returns ``(reading, provenance)``. ``reading`` is ``None`` when a side has no
    live attachment or the registry could not be read, and ``provenance`` then
    carries the code, the reason, the Datastream NAMED, and the gesture.

    This is the caller `capabilities/analytics-alignment.md` §2 said would come.
    It READS -- `list_attachments`, `list_nodes` and one mapping count -- and
    writes nothing: attaching an observed entity stays `observed_entities`'s
    command, and a read that wrote would make re-reading a figure change it.
    """
    left_id = _text(pair.get("left_datastream_id"))
    right_id = _text(pair.get("right_datastream_id"))
    try:
        indexed = _attachments_by_datastream(
            conn, project_id=project_id, datastream_ids=(left_id, right_id)
        )
        labels = _node_labels(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- fail closed, and say which failure
        logger.warning("alignment read: observed-entity registry read failed: %s", exc)
        return None, {
            "state": "unavailable",
            "code": REGISTRY_UNREADABLE,
            "reason": (
                "The observed-entity registry could not be read, so nothing states which "
                "entities either Datastream published. That is not the same fact as "
                "neither of them having any."
            ),
            "gesture": (
                "Read the alignment again; if it persists, the Master Data store is "
                "unreachable and no alignment can be stated until it answers."
            ),
        }

    empty = [item for item in (left_id, right_id) if not indexed.get(item)]
    designations = _designating_column_counts(
        conn, project_id=project_id, datastream_ids=(left_id, right_id)
    )
    if empty:
        return None, {
            "state": "unavailable",
            "code": NO_ATTACHMENTS,
            # NAMED, and both of them when both are empty: a refusal that reveals
            # its blockers one at a time makes a person fix, retry, and be refused
            # again -- the rule `resolve_dependencies` already obeys.
            "datastreams_without_attachment": empty,
            "reason": (
                f"{', '.join(empty)} carries no live observed-entity attachment, so nothing "
                f"states which entities it published. Aligning the other side against an "
                f"empty one would report every one of its rows as unmatched and call that "
                f"the work."
            ),
            "gesture": ATTACH_GESTURE,
            "columns_designating_an_object_kind": designations,
        }

    left, left_unreadable, left_platforms = _sides_from_attachments(indexed[left_id], labels)
    right, right_unreadable, right_platforms = _sides_from_attachments(
        indexed[right_id], labels
    )
    provenance: dict[str, Any] = {
        "rows_from": ROWS_FROM_REGISTRY,
        "attachment_counts": {left_id: len(left), right_id: len(right)},
        "unreadable_attachments": {left_id: left_unreadable, right_id: right_unreadable},
        "entity_key": ENTITY_KEY_NOT_DERIVABLE,
        "entity_key_reason": ENTITY_KEY_NOT_DERIVABLE_REASON,
        "columns_designating_an_object_kind": designations,
    }
    shared_platforms = sorted(left_platforms & right_platforms)
    if shared_platforms:
        # AN ATTRIBUTION IS BEING STOLEN, AND NOTHING ELSE ON THIS PAYLOAD WOULD
        # SAY SO. An attachment is unique per PATH (story 70.2), and a path begins
        # with its platform. So when both Datastreams of the pair observe the same
        # platform, an entity they BOTH publish exists as ONE alias row naming
        # whichever attached it first: the other side does not have it, its rows
        # come back `unmatched`, and the count reads as work when it is an artefact
        # of the registry's identity model. Stated rather than refused -- what IS
        # attached is still true and still worth reading -- but stated on the
        # payload AND in the headline, because a distortion buried under a green
        # `available` is the shape of defect this repository keeps finding.
        provenance["attribution_risk"] = {
            "code": SAME_PLATFORM_PAIR,
            "datastreams": [left_id, right_id],
            "platforms": shared_platforms,
            "shared_platform_count": len(shared_platforms),
            "reason": (
                f"Both Datastreams of this pair publish observed entities of "
                f"{', '.join(shared_platforms)}. An attachment is keyed by path and a path "
                f"begins with its platform, so an entity both sides observe is a single "
                f"binding attributed to whichever Datastream attached it first. The other "
                f"side's rows are therefore reported unmatched for a reason that is not "
                f"about the data."
            ),
            "gesture": (
                "Align two Datastreams of different platforms, or give each source its own "
                "platform name when attaching its observed entities."
            ),
        }
    # No metric, no volume and no day: the registry aligns IDENTITIES, exactly as
    # `analytics_alignment.py` says of itself -- "four fields and no metric". So a
    # registry-built reading ventilates nothing, and the ventilation block says
    # why rather than returning an empty split.
    return ObservedReading(left=left, right=right), provenance


# ---------------------------------------------------------------------------
# The cascade, run over a reading -- the registry's, or a caller's.
# ---------------------------------------------------------------------------


#: The keys of a row payload that are meaningful only on a DECIDED row. Dropped
#: when null rather than carried: `decided_by: null` on a row four automatic
#: stages resolved says nothing a reader can act on, and eighteen of them spend
#: the model-channel budget on absence. The three ADDED COLUMNS are never dropped
#: -- their null IS the contract ("null on every row that is not matched, never a
#: sentinel"), so removing them would make an unaligned row unreadable.
_DECISION_KEYS = ("decided_by", "decided_at", "reason")


def _row_payload(row: AlignedRow) -> dict[str, Any]:
    payload = row.as_dict()
    if row.state != MATCHING_STATE_AMBIGUOUS:
        payload.pop("candidates", None)
    for key in _DECISION_KEYS:
        if payload.get(key) is None:
            payload.pop(key, None)
    return payload


def _run_reading(
    reading: ObservedReading,
    *,
    capability_state: str,
    decisions: Sequence[Any],
) -> dict[str, Any]:
    """Everything one supplied reading produces -- or the refusal it earns.

    The refusal is RETURNED and not raised: an unconverted currency is the answer
    to "what does this pair align to", and a bounded block that swallowed it would
    show a pair with no rows and no reason, which reads as an empty alignment.
    """
    try:
        rows = run_cascade(
            list(reading.left),
            list(reading.right),
            decisions=decisions,
            currencies=reading.currencies,
        )
    except AlignmentRefused as exc:
        return {
            "state": "refused",
            "refusal": {"code": exc.code, "reason": exc.message},
        }

    counts = alignment_counts(rows)
    unmatched = [_row_payload(row) for row in rows if row.state == MATCHING_STATE_UNMATCHED]
    ambiguous = [_row_payload(row) for row in rows if row.state == MATCHING_STATE_AMBIGUOUS]
    matched = [_row_payload(row) for row in rows if row.state == MATCHING_STATE_MATCHED]

    block: dict[str, Any] = {
        "state": "available",
        "alignment_counts": counts,
        # `unmatched` is the work, so it is LISTED. Bounded with its remainder
        # stated -- never folded into the count above it.
        "unmatched": _listing(unmatched),
        # An ambiguity whose candidates are not carried beside it is a decoration.
        "ambiguous": _listing(ambiguous),
        "matched": _listing(matched),
        "added_columns": list(added_columns(capability_state)),
    }
    block["ventilation"] = _ventilation_block(reading, rows, capability_state=capability_state)
    return block


def _ventilation_block(
    reading: ObservedReading, rows: Sequence[AlignedRow], *, capability_state: str
) -> dict[str, Any]:
    """The observed block and the ventilated block, side by side and never summed.

    No basis means no split: there is no default volume and equal parts is refused
    by name, so a reading that declared no volume gets ``ran: False`` with the
    reason -- a run that did not happen, which is a different fact from one where
    every key was refused.
    """
    if reading.basis is None:
        return {
            "ran": False,
            "reason": (
                "No volume was declared, so nothing was split. A prorata share rides on a "
                "declared volume and its version; there is no default and no equal parts."
            ),
            "columns": [],
        }
    outcome: VentilationOutcome = ventilate(
        list(reading.groups), basis=reading.basis, capability_state=capability_state
    )
    metrics = sorted({str(name) for row in reading.observed_by_row.values() for name in row})
    block: dict[str, Any] = {
        "ran": outcome.ran,
        "basis": outcome.basis.as_dict() if outcome.basis is not None else None,
        "counts": ventilation_counts(outcome),
        "refusals": _listing([item.as_dict() for item in outcome.refusals]),
        "columns": list(ventilation_columns(capability_state, metrics)),
    }
    if not outcome.ran or reading.day is None:
        return block

    try:
        composed = ventilated_rows(
            rows,
            outcome=outcome,
            observed_by_key=reading.observed_by_row,
            day=reading.day,
        )
    except AlignmentRefused as exc:
        block["refusal"] = {"code": exc.code, "reason": exc.message}
        return block

    observed_block: list[dict[str, Any]] = []
    ventilated_block: list[dict[str, Any]] = []
    for row in composed:
        payload = {
            "row_key": row.aligned.row_key,
            "state": row.aligned.state,
            **{key: _plain(value) for key, value in row.columns().items()},
        }
        if row.share is None:
            observed_block.append(payload)
        else:
            payload["share"] = row.share.as_dict()
            ventilated_block.append(payload)
    # Two disjoint blocks. A reader may take either alone and its total stays
    # true; nothing here ever adds one to the other.
    block["observed"] = _listing(observed_block)
    block["ventilated"] = _listing(ventilated_block)
    return block


def _plain(value: Any) -> Any:
    """JSON-safe, and a Decimal never becomes a float on the way out."""
    from decimal import Decimal  # noqa: PLC0415

    if isinstance(value, Decimal):
        return str(value)
    return value


# ---------------------------------------------------------------------------
# The read the generic capability surface calls.
# ---------------------------------------------------------------------------


def read_alignment(
    conn,
    *,
    project_id: str,
    capability_state: str,
    left_datastream_id: str | None = None,
    right_datastream_id: str | None = None,
    breakdown: Mapping[str, Any] | None = None,
    reading: ObservedReading | None = None,
) -> dict[str, Any]:
    """The bounded Analytics Alignment block. A READ: it writes nothing, ever.

    The pair is the one the caller named, or -- said explicitly, never silently --
    the FIRST alignable pair. `alignable_pairs` sorts its result, so "the first"
    is the same pair on every read rather than whichever row the planner returned
    first.
    """
    if not capability_is_active(capability_state):
        # OFF NAMES NOTHING. No pair, no count, and not one of the three added
        # column names or one ventilated column name anywhere in this payload.
        return {
            "schema": ALIGNMENT_READ_SCHEMA,
            "capability": ANALYTICS_ALIGNMENT_CAPABILITY_KEY,
            "capability_state": _text(capability_state),
            "active": False,
            "headline": (
                "Analytics Alignment is off on this Project, so it aligns nothing and adds "
                "no column anywhere."
            ),
            "gap_code": "capability_not_active",
            "gesture": (
                "Prepare and confirm a Project Change Set in Project Settings > "
                "Capabilities to turn Analytics Alignment on."
            ),
        }

    pairs = alignable_pairs(conn, project_id=project_id)
    head, withheld = bounded(pairs)
    block: dict[str, Any] = {
        "schema": ALIGNMENT_READ_SCHEMA,
        "capability": ANALYTICS_ALIGNMENT_CAPABILITY_KEY,
        "capability_state": _text(capability_state),
        "active": True,
        "alignable_pairs": {"items": head, "count": len(pairs), "withheld": withheld},
    }
    if not pairs:
        block["headline"] = NO_ALIGNABLE_PAIR
        block["gap_code"] = "no_alignable_pair"
        block["gesture"] = (
            "Declare a common key in Governance > Master Data, publish the mapping of the "
            "two Datastreams that share it, then add and publish a Semantic View "
            "relationship that crosses them."
        )
        return block

    pair = _select_pair(pairs, left_datastream_id, right_datastream_id)
    if pair is None:
        block["headline"] = (
            "No published Semantic View relationship crosses the two Datastreams that were "
            "named, so this pair cannot be aligned."
        )
        block["gap_code"] = "pair_not_alignable"
        block["gesture"] = (
            "Add the relationship between these two Datastreams to a Semantic View, pin "
            "the common key version it crosses on, and publish that view."
        )
        return block

    block["pair"] = pair
    #: Said explicitly. A default nobody was told about is a default nobody chose.
    block["pair_is_the_default"] = not (_text(left_datastream_id) and _text(right_datastream_id))
    dependencies = resolve_dependencies(
        conn,
        project_id=project_id,
        left_datastream_id=pair["left_datastream_id"],
        right_datastream_id=pair["right_datastream_id"],
    )
    block["dependencies"] = dependencies.as_dict()

    decisions = list_alignment_decisions(
        conn,
        project_id=project_id,
        left_datastream_id=pair["left_datastream_id"],
        right_datastream_id=pair["right_datastream_id"],
        common_key_version_id=pair["common_key_version_id"],
    )
    block["decisions"] = _listing(
        [
            {
                "left_row_key": item.left_row_key,
                "decision": item.decision,
                "right_row_key": item.right_row_key,
                "reason": item.reason,
                # The author and the date, on every one: a decision that loses
                # who took it and when is indistinguishable from a row somebody
                # clicked past.
                "decided_by": item.decided_by,
                "decided_at": item.decided_at,
            }
            for item in decisions
        ]
    )

    provenance: dict[str, Any] = {"rows_from": ROWS_FROM_CALLER}
    if reading is None:
        # THE FIRST CALLER OF THE REGISTRY. Story 70.2 built it and said no caller
        # reached it; this is the one. A caller that HAS fuller rows passes them in
        # and this branch is skipped entirely.
        reading, provenance = registry_reading(conn, project_id=project_id, pair=pair)
    if reading is None:
        block["reading"] = provenance
        # NO `alignment_counts` here, deliberately: four zeros over a population
        # nobody attached would be an alignment nobody ran.
    else:
        block["reading"] = {
            **_run_reading(reading, capability_state=capability_state, decisions=decisions),
            **provenance,
        }
    # ON BOTH BRANCHES. `capabilities/analytics-alignment.md`'s 70.4 MCP row
    # promises "read a split and the volume it rode on", and a promise kept only
    # when the cascade could NOT run is the shape of promise nobody notices is
    # broken: the moment a Project attaches its entities, the splits it was
    # already shown would disappear from the payload.
    block["stored_shares"] = _stored_shares(conn, project_id=project_id, pair=pair)

    if breakdown is not None:
        block["breakdown"] = _breakdown_block(
            conn,
            project_id=project_id,
            breakdown=breakdown,
            reading=reading,
            served=block.get("reading") or {},
        )

    block["headline"] = _headline(block)
    return block


def _select_pair(
    pairs: Sequence[Mapping[str, Any]],
    left_datastream_id: str | None,
    right_datastream_id: str | None,
) -> dict[str, Any] | None:
    left, right = _text(left_datastream_id), _text(right_datastream_id)
    if not left and not right:
        return dict(pairs[0])
    for pair in pairs:
        if _text(pair.get("left_datastream_id")) == left and (
            not right or _text(pair.get("right_datastream_id")) == right
        ):
            return dict(pair)
    return None


def _stored_shares(conn, *, project_id: str, pair: Mapping[str, Any]) -> dict[str, Any]:
    """The splits already written for this pair, each naming its volume AND version.

    Read back out of the database rather than recomputed: a weight is the record of
    HOW a published figure was split, and the volume it was taken over moves.
    """
    shares = list_ventilation_weights(
        conn,
        project_id=project_id,
        left_datastream_id=str(pair["left_datastream_id"]),
        right_datastream_id=str(pair["right_datastream_id"]),
        common_key_version_id=str(pair["common_key_version_id"]),
    )
    return _listing([item.as_dict() for item in shares])


def _breakdown_block(
    conn,
    *,
    project_id: str,
    breakdown: Mapping[str, Any],
    reading: ObservedReading | None,
    served: Mapping[str, Any],
) -> dict[str, Any]:
    sanction = sanction_breakdown(
        conn,
        project_id=project_id,
        metric_field_id=_text(breakdown.get("metric")),
        dimension_field_ids=list(breakdown.get("dimensions") or []),
    )
    if not sanction.get("sanctioned"):
        return sanction

    metric = _text(breakdown.get("metric"))
    if reading is None or _text(served.get("state")) != "available":
        sanction["values"] = {
            "state": "unavailable",
            "code": VALUES_UNAVAILABLE,
            "reason": (
                "The grain sanctions this breakdown, and no aligned reading is available to "
                "carry its figures. A zero here would state that the measure was taken and "
                "came to nothing."
            ),
            # The gesture of the reading's OWN refusal, never a second one invented
            # here: the two would come to disagree the day the first is corrected.
            "gesture": _text(served.get("gesture")) or ATTACH_GESTURE,
        }
        return sanction

    ventilation = served.get("ventilation") or {}
    measured = {
        name for row in (reading.observed_by_row or {}).values() for name in row
    }
    if metric not in measured:
        registry_built = _text(served.get("rows_from")) == ROWS_FROM_REGISTRY
        sanction["values"] = {
            "state": "unavailable",
            "code": VALUES_UNAVAILABLE,
            "reason": (
                f"The reading carries no observed value for {metric}, so the sanctioned "
                f"breakdown has nothing to cut."
                + (
                    " This reading was built from the observed-entity registry, which binds "
                    "identities and carries no metric -- `analytics_alignment` says of "
                    "itself 'four fields and no metric', and the registry is its input."
                    if registry_built
                    else ""
                )
            ),
            "gesture": (
                "Read the alignment from a surface that carries the measured figures of "
                "this pair; the identity side is already governed."
                if registry_built
                else ATTACH_GESTURE
            ),
        }
        return sanction

    # No new aggregation engine: the figures are the observed and ventilated
    # blocks the reading already produced, pointed at rather than recomputed.
    sanction["values"] = {
        "state": "available",
        "metric": metric,
        "observed": ventilation.get("observed", {"items": [], "count": 0, "withheld": 0}),
        "ventilated": ventilation.get("ventilated", {"items": [], "count": 0, "withheld": 0}),
        "note": (
            "The observed block and the ventilated block are disjoint and are never summed "
            "together."
        ),
    }
    return sanction


def _headline(block: Mapping[str, Any]) -> str:
    pairs = (block.get("alignable_pairs") or {}).get("count", 0)
    pair = block.get("pair") or {}
    reading = block.get("reading") or {}
    parts = [
        f"{pairs} alignable pair(s); reading "
        f"{_text(pair.get('left_datastream_id'))} against "
        f"{_text(pair.get('right_datastream_id'))}."
    ]
    missing = (block.get("dependencies") or {}).get("missing") or []
    if missing:
        parts.append(f"{len(missing)} unmet dependency/dependencies, each with its gesture.")
    state = _text(reading.get("state"))
    if state == "available":
        counts = reading.get("alignment_counts") or {}
        parts.append(
            f"{counts.get('matched', 0)} matched, {counts.get('ambiguous', 0)} ambiguous, "
            f"{counts.get('unmatched', 0)} unmatched, {counts.get('accepted', 0)} accepted."
        )
    elif state == "refused":
        parts.append(f"Refused: {(reading.get('refusal') or {}).get('code')}.")
    else:
        parts.append(
            f"No aligned reading is available ({_text(reading.get('code')) or 'unknown'}), "
            f"so no state is counted."
        )
    # AFTER the three-way chain, never inside it. Placed between the `elif` and the
    # `else` it hijacked that `else`, and a headline announced "40 matched" and "no
    # aligned reading is available" in one sentence. Caught by the envelope
    # measurement of review round 1, which is the only reason anybody read it.
    risk = reading.get("attribution_risk")
    if isinstance(risk, dict):
        # In the TEXT channel too: the model-channel guard routes the whole block
        # into the app channel, and a distortion nobody can read is not stated.
        parts.append(
            f"Warning {risk.get('code')}: both Datastreams publish "
            f"{', '.join(risk.get('platforms') or [])}, so an entity both observe is "
            f"attributed to one of them only."
        )
    breakdown = block.get("breakdown")
    if isinstance(breakdown, dict):
        if breakdown.get("sanctioned"):
            sliced = breakdown.get("sliced_by") or {}
            parts.append(
                f"Breakdown sanctioned by grain {sliced.get('measurement_grain_id')} at "
                f"version {sliced.get('version_id')}."
            )
        else:
            parts.append(
                f"Breakdown refused: {(breakdown.get('refusal') or {}).get('code')}."
            )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The row a person may arbitrate. PREPARE ONLY -- nothing here writes.
# ---------------------------------------------------------------------------


def prepare_row_decision(
    conn,
    *,
    project_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
    left_row_key: str,
    reading: ObservedReading | None = None,
) -> dict[str, Any]:
    """Freeze what a confirmation would act on. NON-AUTHORIZING, and it writes nothing.

    It returns the row, its candidates when a reading can name them, the decision
    that ALREADY stands if there is one -- the first decision stands, so a row that
    is settled is reported settled BEFORE anybody confirms -- and a review
    reference: the canonical hash of the frozen selector, which identifies which
    review is being confirmed and authorizes nothing on its own.
    """
    pairs = alignable_pairs(conn, project_id=project_id)
    pair = _select_pair(pairs, left_datastream_id, right_datastream_id)
    if pair is None:
        raise AlignmentRefused(
            "pair_not_alignable",
            "No published Semantic View relationship crosses these two Datastreams, so "
            "there is no pair to arbitrate a row of. Add the relationship to a Semantic "
            "View, pin the common key version, and publish that view.",
        )
    row_key = _text(left_row_key)
    if not row_key:
        raise AlignmentRefused(
            "decision_names_no_row",
            "An arbitration names the row it settles. Name the left row key.",
        )

    dependencies = resolve_dependencies(
        conn,
        project_id=project_id,
        left_datastream_id=pair["left_datastream_id"],
        right_datastream_id=pair["right_datastream_id"],
    )
    standing = None
    for item in list_alignment_decisions(
        conn,
        project_id=project_id,
        left_datastream_id=pair["left_datastream_id"],
        right_datastream_id=pair["right_datastream_id"],
        common_key_version_id=pair["common_key_version_id"],
    ):
        if item.left_row_key == row_key:
            standing = {
                "decision": item.decision,
                "right_row_key": item.right_row_key,
                "reason": item.reason,
                "decided_by": item.decided_by,
                "decided_at": item.decided_at,
            }
            break

    candidates: dict[str, Any]
    provenance: dict[str, Any] = {"rows_from": ROWS_FROM_CALLER}
    if reading is None:
        reading, provenance = registry_reading(conn, project_id=project_id, pair=pair)
    if reading is None:
        # The candidates cannot be NAMED, so they are not invented and not left
        # as an empty list either -- an empty list would state that nothing on the
        # other side answers this row, which is a decision nobody took.
        candidates = provenance
    else:
        rows = run_cascade(
            list(reading.left), list(reading.right), currencies=reading.currencies
        )
        found = next((row for row in rows if row.row_key == row_key), None)
        candidates = {
            "state": "available",
            # `None` when the reading carries no such row -- distinct from a row
            # the cascade left `unmatched`, and the two must not read as one.
            "row_state": found.state if found is not None else None,
            "row_is_observed": found is not None,
            "items": list(found.candidates) if found is not None else [],
            **provenance,
        }

    frozen = {
        "project_id": _text(project_id),
        "left_datastream_id": pair["left_datastream_id"],
        "right_datastream_id": pair["right_datastream_id"],
        "common_key_version_id": pair["common_key_version_id"],
        "left_row_key": row_key,
        "standing_decision": standing,
    }
    return {
        "pair": pair,
        "left_row_key": row_key,
        "dependencies": dependencies.as_dict(),
        "candidates": candidates,
        "standing_decision": standing,
        # The first decision stands, so a settled row is reported settled here
        # rather than after a confirmation that would have written nothing.
        "already_decided": standing is not None,
        "review_reference": _canonical_hash(frozen),
        "authorizing": False,
    }


#: A decision may only be taken on a row the reading actually carries. Four
#: refusals, and the order below is the order a person meets them.
REFUSAL_LEFT_ROW_NOT_OBSERVED = "left_row_not_observed"
REFUSAL_ROW_ALREADY_CASCADED = "row_already_resolved_by_the_cascade"
REFUSAL_RIGHT_ROW_NOT_PUBLISHED = "arbitration_names_an_absent_row"
REFUSAL_ROW_UNVERIFIABLE = "row_cannot_be_verified"


def refuse_undecidable_decision(
    conn,
    *,
    project_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
    left_row_key: str,
    decision: str,
    right_row_key: str | None,
) -> dict[str, Any]:
    """Refuse, BEFORE the write, a decision the alignment could never honour.

    THE DEFECT THIS EXISTS FOR, measured 2026-08-28. Nothing checked the row an
    arbitration picked. A decision naming a row the right side does not publish is
    accepted by the store's CHECKs (they only require *a* row key), and then
    `run_cascade` raises `arbitration_names_an_absent_row` on EVERY subsequent
    read: the pair's reading is permanently `refused`, no state is counted, and
    because the FIRST decision stands and migration 309 grants no `UPDATE` and no
    withdrawal gesture exists, nothing can undo it. One unvalidated string bricks
    a pair for good. The validation therefore belongs before the INSERT, and here
    rather than in the MCP surface, because a console door would need exactly the
    same one.

    Returns the aligned row it validated against, so a caller can report what the
    cascade thought of it. Raises :class:`AlignmentRefused` otherwise.
    """
    pairs = alignable_pairs(conn, project_id=project_id)
    pair = _select_pair(pairs, left_datastream_id, right_datastream_id)
    if pair is None:
        raise AlignmentRefused(
            "pair_not_alignable",
            "No published Semantic View relationship crosses these two Datastreams.",
        )
    reading, provenance = registry_reading(conn, project_id=project_id, pair=pair)
    if reading is None:
        # NOT a silent pass. A decision cannot be checked against a reading that
        # does not exist, and writing it anyway is what produced the defect above.
        raise AlignmentRefused(
            REFUSAL_ROW_UNVERIFIABLE,
            f"{_text(provenance.get('reason')) or 'No reading of this pair is available'} "
            f"So neither the row nor the entity picked can be checked, and a decision "
            f"that cannot be checked is the one that brick this pair for good: the first "
            f"decision stands and nothing withdraws it. {_text(provenance.get('gesture'))}",
        )

    rows = run_cascade(list(reading.left), list(reading.right), currencies=reading.currencies)
    row_key = _text(left_row_key)
    found = next((row for row in rows if row.row_key == row_key), None)
    if found is None:
        raise AlignmentRefused(
            REFUSAL_LEFT_ROW_NOT_OBSERVED,
            f"{row_key!r} is not a row this alignment observes, so there is nothing to "
            f"decide about it. Read the alignment and decide one of the rows it lists.",
        )
    if found.state == MATCHING_STATE_MATCHED and found.method != METHOD_HUMAN_ARBITRATION:
        # `run_cascade` IGNORES a decision on a row an automatic stage resolved
        # (`analytics_alignment.py:485`: the decision applies only when
        # `state != matched`). Writing one anyway announces an act that has no
        # effect -- and that then takes effect silently the day the mapping moves
        # and the row stops matching. The card's own rule: if an `id_exact`
        # alignment is wrong, the MAPPING is wrong.
        raise AlignmentRefused(
            REFUSAL_ROW_ALREADY_CASCADED,
            f"{row_key!r} was resolved automatically by {found.method}, and a stored "
            f"decision never overwrites a row an automatic stage settled -- it would be "
            f"recorded, do nothing, and then take effect silently the day the row stops "
            f"matching. If this alignment is wrong, the mapping is wrong: correct the "
            f"Datastream mapping or the observed-entity attachment.",
        )
    if decision == DECISION_ARBITRATED:
        published = {_text(side.row_key) for side in reading.right}
        picked = _text(right_row_key)
        if picked not in published:
            raise AlignmentRefused(
                REFUSAL_RIGHT_ROW_NOT_PUBLISHED,
                f"{picked!r} is not a row the right-hand Datastream publishes, so this "
                f"arbitration names nothing. Pick one of the rows the alignment lists on "
                f"that side; the candidates of an ambiguous row are carried beside it.",
            )
    return {"row": _row_payload(found), "pair": pair, "provenance": provenance}


def review_reference_for(
    conn, *, project_id: str, left_datastream_id: str, right_datastream_id: str, left_row_key: str
) -> str:
    """Re-derive the frozen reference from the state as it is NOW.

    A confirmation compares this with the one it was handed: if a decision landed
    in between, the reference moved and the review is stale. That is the same
    contract `confirm_project_capability_change` has with
    `prepared_payload_hash` -- a review reference identifies which review is being
    confirmed, and a review whose subject changed is no longer that review.
    """
    prepared = prepare_row_decision(
        conn,
        project_id=project_id,
        left_datastream_id=left_datastream_id,
        right_datastream_id=right_datastream_id,
        left_row_key=left_row_key,
    )
    return str(prepared["review_reference"])


__all__ = [
    "ALIGNMENT_READ_SCHEMA",
    "MAX_LISTED_ROWS",
    "ATTACH_GESTURE",
    "ENTITY_KEY_NOT_DERIVABLE",
    "ENTITY_KEY_NOT_DERIVABLE_REASON",
    "NO_ATTACHMENTS",
    "REGISTRY_UNREADABLE",
    "ROWS_FROM_CALLER",
    "ROWS_FROM_REGISTRY",
    "REFUSAL_BREAKDOWN_SPANS_GRAINS",
    "REFUSAL_DIMENSION_NOT_IN_GRAIN",
    "REFUSAL_NO_GOVERNED_GRAIN",
    "VALUES_UNAVAILABLE",
    "ObservedReading",
    "REFUSAL_LEFT_ROW_NOT_OBSERVED",
    "REFUSAL_RIGHT_ROW_NOT_PUBLISHED",
    "REFUSAL_ROW_ALREADY_CASCADED",
    "REFUSAL_ROW_UNVERIFIABLE",
    "REFUSAL_TOTAL_NOT_DECLARED",
    "SAME_PLATFORM_PAIR",
    "bounded",
    "refuse_undecidable_decision",
    "prepare_row_decision",
    "read_alignment",
    "registry_reading",
    "review_reference_for",
    "sanction_breakdown",
]
