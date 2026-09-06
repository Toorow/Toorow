"""Story 71.1 -- the MDM measurement grain: a metric names the dimensions it is
reported against. The mirror of the common key on the OTHER axis.

WHAT THIS OWNS, AND WHY IT IS THE FOURTH FACT. Crossing two Datastreams needs
three facts (`mdm_common_keys.py`); slicing one measure needs a fourth, and it
had no owner:

    these canonical fields are ONE business identity   <- common key (identity axis)
    this physical column IS that canonical field       <- datastream mapping
    the cross goes this way, at this cardinality       <- semantic view relationship
    this measure is reported against these dimensions  <- HERE (measure axis)

The MDM models a metric and a dimension as INDEPENDENT canonical fields --
neither references the other -- and its only relational object, the common key,
is dimensions-only and refuses a metric by name (`component_is_metric`). So there
was nowhere to say the one thing a report is made of: *spend is reported by day,
campaign, publisher, format*. A measurement grain says it, through the MDM, and
refuses everything the mirror refuses.

IT IS THE COMMON KEY'S REFUSAL, REFLECTED. The common key requires dimensions and
refuses a metric component; this requires a metric HEAD and refuses a dimension
head (`head_is_dimension`), requires dimension MEMBERS and refuses a metric one
(`member_is_metric`). One divergence, and only one: a grain of ZERO dimensions is
legal -- a measure reported at no breakdown is a total -- where a common key of
zero components is not.

IDENTITY IS THE HEAD PLUS THE ORDERED MEMBERS, NOT THE NAME. Adding a dimension
is a NEW version, never an edit, and the `content_hash` proves it, so a measure
already sliced a given way keeps meaning what it meant. A name is what a human
reads; it is never what a consumer pins. Consumers pin a VERSION id.

NO SECOND VOCABULARY. The head and every member are `app.mdm_canonical_fields.id`
resolved through `canonical_field_registry.list_visible_canonical_fields` -- the
same predicate (`project_id IS NULL OR project_id = %s`) the common key and every
`datastream_field_mapping` binding read. A field this module would accept is a
field a binding can name, because both read one function.

WHAT THIS DELIBERATELY LEAVES TO 71.2. Derived coverage (which Datastreams
implement the head and each member, and which bind the metric but not one of its
dimensions -- *partial*), and used-by (what a live read cuts by the grain, so it
cannot be archived under one). This module owns the object, its immutable
versions, its refusals and its door. The physical-type disagreement refusal below
reads the published mappings for exactly the columns it compares -- the minimal
read that refusal needs, not the coverage surface 71.2 renders.

THE FIRST CALLER LANDED (story 71.4), AND IT IS NOT A ROW IN `used_by`. DO NOT GO
LOOKING FOR ONE. `core.analytics_alignment_read.sanction_breakdown` is the
consumer `grain_used_by` below was written in anticipation of: it resolves the
live grains a metric heads, refuses a breakdown by a dimension none of them names
(`breakdown_dimension_not_in_grain`), and carries `sliced_by:
{measurement_grain_id, version_id}` on the payload it serves -- the version, never
the id alone, because *"a consumer pins a grain without a version"* is a clause of
that amendment's `Incomplete if`.

That caller is a READ. It persists nothing, so it writes no pin, and
:func:`grain_used_by` is UNCHANGED by it: it still lists only what a published
Semantic View or Concept version records in its `master_data_refs`. A read cannot
be a used-by row without a store to put it in, and storing every read would make
the archive gate refuse on a question somebody asked once. The pin travels on the
payload instead, where the reader that asked for the slice can see the exact
version it was cut by. A pin becomes a row here the day a PERSISTED consumer -- a
saved card, a published view -- slices by a grain; nothing does today.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ulid import ULID

from core.audit import declare_action, insert_audit_row
from core.canonical_field_registry import list_visible_canonical_fields
from core.datastream_field_mapping import physical_type_class

# ---------------------------------------------------------------------------
# What this module writes to the audit trail -- AD-42: an action is declared
# where it is WRITTEN, never in a central list nobody owns. The same reason the
# common key declares its three: publishing a Semantic View left a trace, and
# declaring or archiving the GRAIN a read is cut by left none -- yet it is the
# decision that sanctions a breakdown. The journal carries the gesture and its
# author; the versions are already immutable and guarded by a trigger.
# ---------------------------------------------------------------------------
ACTION_MEASUREMENT_GRAIN_DECLARED = declare_action("mdm.measurement_grain.declared")
ACTION_MEASUREMENT_GRAIN_VERSIONED = declare_action("mdm.measurement_grain.versioned")
ACTION_MEASUREMENT_GRAIN_ARCHIVED = declare_action("mdm.measurement_grain.archived")


def _record(conn, action: str, *, actor: str, project_id: str, **metadata: Any) -> None:
    """One journal row, ON THE SAME TRANSACTION as what it records.

    `insert_audit_row` rather than `write_audit_row`: the second opens its own
    connection and never raises, which suits a caller that has already committed.
    Here the caller has not -- `metric_dimensions_api` calls then `conn.commit()`
    -- so a journal outside the transaction would assert a declaration a rollback
    erased. The gesture and its proof commit together or not at all.
    """
    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=action,
        provider_account="",
        connection_ref="",
        metadata={"project_id": project_id, **metadata},
    )


#: A measure reported against more dimensions than this is not a grain, it is a
#: row dump. Bounded here AND in the CHECK of the migration, so neither door can
#: be the wide one. Larger than the common key's 8: a key beyond eight components
#: is a row identity, but a measure is legitimately cut by many axes at once (day,
#: campaign, publisher, format, country, device, placement, creative...).
MAX_MEMBERS = 16

#: Binding statuses that count as an implementation of the head or a member. The
#: same reading `datastream_field_mapping.py:644` uses: a `suggested` or
#: `blocking` binding is a proposal, not a physical fact.
IMPLEMENTING_BINDING_STATUSES: frozenset[str] = frozenset({"confirmed", "resolved"})

#: Bumped when the hashed document changes shape, so a stored hash is only ever
#: compared to a hash produced by the same contract.
MEASUREMENT_GRAIN_CONTRACT_VERSION = "mdm-measurement-grain.v1"

#: A consumer version whose reference can still come into force, so it blocks the
#: archive of a grain it cuts by. A `draft` or a `candidate` is not permission to
#: execute anything, but it CAN be published tomorrow -- so a grain it slices is
#: not free to be retired today. `superseded` and `archived` are the opposite:
#: history, which never comes back and never blocks. The exact posture of
#: `mdm_common_keys.LIVE_VIEW_STATUSES`, on the measure axis.
LIVE_CONSUMER_STATUSES: frozenset[str] = frozenset({"draft", "candidate", "published"})


class MeasurementGrainRefused(ValueError):
    """A refusal with a code a caller can act on, and a sentence a human reads.

    The sentence NAMES THE GESTURE THAT REPAIRS, never an identifier -- the same
    rule `test_refusals_never_name_an_identifier` sweeps the whole of `core/` for.
    Where a field has a resolved name it is named by it; where only an id sent by
    the caller is in hand, the sentence names what to do without printing it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MeasurementGrainNotFound(LookupError):
    """Foreign, denied and nonexistent are ONE answer.

    Telling them apart tells an unauthorized caller that the object exists, so
    every read that cannot serve raises this and the route answers 404. The
    distinguishing reason belongs in audit.
    """


class MetricDimensionsUnavailable(RuntimeError):
    """A read the archive gate depends on could not be served -- fail CLOSED.

    The mirror of `master_data.MasterDataUnavailable`, and for the identical
    reason it was written: `app.market_bindings`'s used-by reader swallowed its
    errors and returned ``()``, which a mutation guard read as "nothing depends
    on this". So the used-by read here converts ANY read failure into this, and
    the archive treats it as a hard block -- a grain is never retired on a
    dependency read that did not run. "I could not look" is never "nobody cuts
    by it".

    Coverage is deliberately NOT one of these: it is a display surface, so it
    returns ``state: unavailable`` (the mapping-coverage doctrine) rather than
    raising. Used-by GATES a destructive action, so it raises. Two reads, two
    failure modes, on purpose.
    """


@dataclass(frozen=True)
class GrainHead:
    """The measure a grain is reported for."""

    canonical_field_id: str
    canonical_name: str
    value_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_field_id": self.canonical_field_id,
            "canonical_name": self.canonical_name,
            "value_type": self.value_type,
        }


@dataclass(frozen=True)
class Member:
    """One resolved, ordered dimension a measure is cut by."""

    ordinal: int
    canonical_field_id: str
    canonical_name: str
    value_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "canonical_field_id": self.canonical_field_id,
            "canonical_name": self.canonical_name,
            "value_type": self.value_type,
        }


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def grain_hash(head: GrainHead, members: Sequence[Member]) -> str:
    """The identity of a version: the contract, the HEAD and the ORDERED members.

    The names are deliberately absent. Renaming a canonical field does not change
    which measure is cut by which dimensions, and a hash that moved on a rename
    would break every consumer that pinned the version.
    """
    document = {
        "contract": MEASUREMENT_GRAIN_CONTRACT_VERSION,
        "head": head.canonical_field_id,
        "members": [member.canonical_field_id for member in members],
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def clean_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MeasurementGrainRefused(
            "name_required", "A measurement grain needs a name."
        )
    name = value.strip()
    if len(name) > 120:
        raise MeasurementGrainRefused(
            "name_too_long", "A measurement grain name is at most 120 characters."
        )
    return name


def resolve_grain(
    conn,
    *,
    project_id: str,
    head_field_id: Any,
    member_field_ids: Sequence[Any],
) -> tuple[GrainHead, list[Member]]:
    """Turn a head id and an ordered member list into a validated grain.

    Every refusal happens HERE, before any write. Where a field is in the visible
    catalog it is named by its word; where it is not, the sentence names the
    gesture rather than the id the caller sent -- a refusal that printed the id
    would put a `mdm_<ULID>` in front of a person repairing a grain.
    """
    if not isinstance(head_field_id, str) or not head_field_id.strip():
        raise MeasurementGrainRefused(
            "head_required",
            "A measurement grain is headed by one canonical measure. Name the "
            "metric this grain reports.",
        )
    head_id = head_field_id.strip()

    if not isinstance(member_field_ids, (list, tuple)):
        raise MeasurementGrainRefused(
            "members_malformed",
            "A measurement grain's dimensions are a list of canonical fields.",
        )
    if len(member_field_ids) > MAX_MEMBERS:
        raise MeasurementGrainRefused(
            "too_many_members",
            f"A measurement grain carries at most {MAX_MEMBERS} dimensions; "
            f"{len(member_field_ids)} were sent.",
        )

    wanted: list[str] = []
    for raw in member_field_ids:
        if not isinstance(raw, str) or not raw.strip():
            raise MeasurementGrainRefused(
                "member_not_found",
                "Every dimension of a grain is a canonical field. One entry is not.",
            )
        member_id = raw.strip()
        if member_id == head_id:
            # The head is the measure, a member is a dimension: the two concept
            # kinds cannot be the same field, and this reads better than letting
            # it fall through to `member_is_metric`.
            raise MeasurementGrainRefused(
                "member_is_the_head",
                "The measure a grain reports cannot also be one of the dimensions "
                "it is cut by. Drop it from the members.",
            )
        if member_id in wanted:
            raise MeasurementGrainRefused(
                "member_duplicated",
                "A dimension is named once in a grain; one appears more than once. "
                "Remove the repeat.",
            )
        wanted.append(member_id)

    # The SAME visibility predicate a binding is validated against, from the one
    # function that owns it. `list_visible_canonical_fields` returns only ACTIVE
    # rows, at both scopes -- so unknown, archived and foreign arrive as absence.
    visible = {
        row["id"]: row
        for row in list_visible_canonical_fields(conn, project_id=project_id)
    }

    head_row = visible.get(head_id)
    if head_row is None:
        raise MeasurementGrainRefused(
            "head_not_found",
            "The measure this grain names is not an active canonical field of this "
            "project. Declare it, or head the grain with a measure that exists.",
        )
    if head_row.get("concept_kind") != "metric":
        raise MeasurementGrainRefused(
            "head_is_dimension",
            f"{head_row.get('canonical_name')} is a dimension. A measurement grain "
            "is headed by a measure -- what is reported -- and cut by dimensions. "
            "Head it with the metric instead.",
        )
    head = GrainHead(
        canonical_field_id=head_id,
        canonical_name=str(head_row.get("canonical_name") or ""),
        value_type=str(head_row.get("value_type") or ""),
    )

    members: list[Member] = []
    for ordinal, member_id in enumerate(wanted):
        row = visible.get(member_id)
        if row is None:
            raise MeasurementGrainRefused(
                "member_not_found",
                "A dimension named here is not an active canonical field of this "
                "project. Declare it, or drop it from the grain.",
            )
        if row.get("concept_kind") != "dimension":
            raise MeasurementGrainRefused(
                "member_is_metric",
                f"{row.get('canonical_name')} is a measure. A grain is cut by "
                "dimensions, not by other measures -- a measure is what it reports, "
                "not an axis it is sliced by.",
            )
        members.append(
            Member(
                ordinal=ordinal,
                canonical_field_id=member_id,
                canonical_name=str(row.get("canonical_name") or ""),
                value_type=str(row.get("value_type") or ""),
            )
        )

    # A grain of ZERO members is legal (a total) -- the one divergence from the
    # common key, which refuses zero components. So no `members_required` refusal.
    _refuse_disagreeing_physical_types(conn, project_id=project_id, members=members)
    return head, members


def _refuse_disagreeing_physical_types(
    conn, *, project_id: str, members: Sequence[Member]
) -> None:
    """Refuse a member whose implementing physical types disagree, BY NAME.

    The mirror of the common key's `component_physical_types_disagree`
    (`governance.md:1554`). A grain cut by a dimension that is a string in one
    Datastream and an integer in another cannot align that breakdown across the
    two: the same value would not compare, and a slice would silently split. The
    comparison is made HERE, before any write, and the sentence names the columns
    that diverge so a person repairs the mapping that is wrong.

    A read that FAILED does not refuse: "I could not look" is not "they
    disagree", and declaring a grain is not executing a slice. `unknown` is not a
    class -- a type the classifier does not recognize is a mapping ambiguity, not
    evidence two sources disagree.
    """
    if not members:
        return
    implementing = _implementing_physical_types(
        conn,
        project_id=project_id,
        field_ids=[member.canonical_field_id for member in members],
    )
    if implementing is None:  # the read failed -- do not refuse on what we could not see
        return

    by_name = {member.canonical_field_id: member.canonical_name for member in members}
    for field_id, implementations in implementing.items():
        by_class: dict[str, list[str]] = {}
        for datastream_name, physical_type in implementations:
            klass = physical_type_class(physical_type, "")
            if klass == "unknown":
                continue
            by_class.setdefault(klass, []).append(
                f"{datastream_name} ({physical_type or 'untyped'}, {klass})"
            )
        if len(by_class) < 2:
            continue
        named = "; ".join(
            sorted(item for values in by_class.values() for item in values)
        )
        # THE NAME, OR THE GESTURE -- never the id. A member without a resolved
        # word is designated by what it IS, and the next sentence already names
        # the columns that diverge.
        subject = (by_name.get(field_id) or "").strip() or "A dimension of this grain"
        raise MeasurementGrainRefused(
            "member_physical_types_disagree",
            f"{subject} is implemented by columns of disagreeing physical types — "
            f"{named}. A measure cannot be cut consistently by a dimension one "
            "source stores as one type and another as another; repair the mapping "
            "that is wrong, then declare the grain.",
        )


def _implementing_physical_types(
    conn, *, project_id: str, field_ids: Sequence[str]
) -> dict[str, list[tuple[str, str]]] | None:
    """`{field_id: [(datastream_name, physical_type), ...]}` from published mappings.

    The minimal read the physical-type refusal needs -- NOT the derived coverage
    surface (which names the mapping version and the physical field, separates
    unmapped Datastreams, and marks a metric bound-without-its-dimension as
    partial). That surface is story 71.2. Returns ``None`` when the store could
    not be read, so the refusal above can tell "I could not look" from "nobody
    binds it".
    """
    wanted = {str(field_id) for field_id in field_ids}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.name, m.mapping_payload
                  FROM app.datastreams d
                  LEFT JOIN app.datastream_mapping_versions m
                         ON m.id = d.current_mapping_version_id
                 WHERE d.project_id = %s
                 ORDER BY lower(d.name)
                """,
                (project_id,),
            )
            rows = cur.fetchall()
    except Exception:  # pragma: no cover -- exercised through the API's 503 path
        return None

    per_field: dict[str, list[tuple[str, str]]] = {field_id: [] for field_id in wanted}
    for name, payload in rows:
        if not isinstance(payload, dict):
            continue
        for field in payload.get("fields") or []:
            if not isinstance(field, dict):
                continue
            binding = field.get("binding")
            if not isinstance(binding, dict):
                continue
            target = binding.get("mdm_target")
            if target not in wanted:
                continue
            if binding.get("status") not in IMPLEMENTING_BINDING_STATUSES:
                continue
            per_field[str(target)].append(
                (str(name or ""), str(field.get("physical_type") or ""))
            )
            break
    return per_field


def _project_org(conn, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None:
        raise MeasurementGrainNotFound(project_id)
    return str(row[0])


def create_measurement_grain(
    conn,
    *,
    project_id: str,
    name: Any,
    head_field_id: Any,
    member_field_ids: Sequence[Any],
    actor: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Create the grain and its immutable version 1. The caller owns the transaction."""
    grain_name = clean_name(name)
    head, members = resolve_grain(
        conn,
        project_id=project_id,
        head_field_id=head_field_id,
        member_field_ids=member_field_ids,
    )
    org_id = _project_org(conn, project_id)

    grain_id = f"mmd_{ULID()}"
    version_id = f"mmdv_{ULID()}"
    members_payload = [member.as_dict() for member in members]
    content_hash = grain_hash(head, members)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_metric_dimensions
                (id, org_id, project_id, name, description, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (grain_id, org_id, project_id, grain_name, description, actor),
        )
        if cur.fetchone() is None:
            # The partial unique index on the active name refused it. Saying the
            # name back is the difference between a repairable answer and a
            # constraint name.
            raise MeasurementGrainRefused(
                "measurement_grain_name_taken",
                f"{grain_name!r} is already an active measurement grain in this project.",
            )
        cur.execute(
            """
            INSERT INTO app.mdm_metric_dimension_versions
                (id, metric_dimensions_id, org_id, project_id, version_number,
                 head, members, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s::jsonb, %s::jsonb, %s, %s)
            """,
            (
                version_id,
                grain_id,
                org_id,
                project_id,
                canonical_json(head.as_dict()),
                canonical_json(members_payload),
                content_hash,
                actor,
            ),
        )
        cur.execute(
            "UPDATE app.mdm_metric_dimensions SET current_version_id = %s, "
            "updated_at = NOW() WHERE id = %s AND project_id = %s",
            (version_id, grain_id, project_id),
        )

    _record(
        conn,
        ACTION_MEASUREMENT_GRAIN_DECLARED,
        actor=actor,
        project_id=project_id,
        measurement_grain_id=grain_id,
        name=grain_name,
        version_id=version_id,
        version_number=1,
        content_hash=content_hash,
        head=head.canonical_field_id,
        members=[member.canonical_field_id for member in members],
    )

    return {
        "id": grain_id,
        "name": grain_name,
        "description": description,
        "status": "active",
        "current_version": {
            "id": version_id,
            "version_number": 1,
            "content_hash": content_hash,
            "head": head.as_dict(),
            "members": members_payload,
        },
    }


def append_version(
    conn,
    *,
    project_id: str,
    measurement_grain_id: str,
    head_field_id: Any,
    member_field_ids: Sequence[Any],
    actor: str,
) -> dict[str, Any]:
    """Append version N+1 and move the head. Never edits version N.

    Adding a dimension, dropping one, or re-heading the measure are all a NEW
    version -- the `content_hash` proves the shape changed, and a consumer that
    pinned version N keeps meaning what it meant.
    """
    row = _load_grain_row(
        conn, project_id=project_id, measurement_grain_id=measurement_grain_id
    )
    if row["status"] != "active":
        # NAME A GESTURE THAT EXISTS. Declaring again is what the schema allows:
        # the active-name unique index is UNIQUE only WHERE status = 'active', so
        # an archived name is free again. The new grain starts at version 1 with
        # its own id -- the honest outcome, since consumers pin a VERSION and
        # resurrecting the old grain would hand them a history they were detached
        # from.
        raise MeasurementGrainRefused(
            "measurement_grain_archived",
            "This measurement grain is archived and does not take new versions. "
            "Declare a grain with the same name — an archived name is free again — "
            "then pin it where it is needed.",
        )
    head, members = resolve_grain(
        conn,
        project_id=project_id,
        head_field_id=head_field_id,
        member_field_ids=member_field_ids,
    )
    new_hash = grain_hash(head, members)
    if row.get("current_content_hash") == new_hash:
        raise MeasurementGrainRefused(
            "measurement_grain_unchanged",
            "This version declares the same measure and the same dimensions, in "
            "the same order, as the current one. Nothing would change.",
        )

    version_id = f"mmdv_{ULID()}"
    members_payload = [member.as_dict() for member in members]
    next_number = int(row["current_version_number"] or 0) + 1

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_metric_dimension_versions
                (id, metric_dimensions_id, org_id, project_id, version_number,
                 head, members, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            """,
            (
                version_id,
                measurement_grain_id,
                row["org_id"],
                project_id,
                next_number,
                canonical_json(head.as_dict()),
                canonical_json(members_payload),
                new_hash,
                actor,
            ),
        )
        cur.execute(
            "UPDATE app.mdm_metric_dimensions SET current_version_id = %s, "
            "updated_at = NOW() WHERE id = %s AND project_id = %s",
            (version_id, measurement_grain_id, project_id),
        )

    _record(
        conn,
        ACTION_MEASUREMENT_GRAIN_VERSIONED,
        actor=actor,
        project_id=project_id,
        measurement_grain_id=measurement_grain_id,
        name=row["name"],
        version_id=version_id,
        version_number=next_number,
        content_hash=new_hash,
        replaces_content_hash=row.get("current_content_hash"),
        head=head.canonical_field_id,
        members=[member.canonical_field_id for member in members],
    )

    return {
        "id": measurement_grain_id,
        "current_version": {
            "id": version_id,
            "version_number": next_number,
            "content_hash": new_hash,
            "head": head.as_dict(),
            "members": members_payload,
        },
    }


def _load_grain_row(
    conn, *, project_id: str, measurement_grain_id: str
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.id, g.org_id, g.project_id, g.name, g.description, g.status,
                   g.current_version_id, v.version_number, v.content_hash,
                   v.head, v.members, g.created_by, g.created_at, g.updated_at
              FROM app.mdm_metric_dimensions g
              LEFT JOIN app.mdm_metric_dimension_versions v
                     ON v.id = g.current_version_id
             WHERE g.id = %s AND g.project_id = %s
            """,
            (measurement_grain_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise MeasurementGrainNotFound(measurement_grain_id)
    return {
        "id": row[0],
        "org_id": row[1],
        "project_id": row[2],
        "name": row[3],
        "description": row[4],
        "status": row[5],
        "current_version_id": row[6],
        "current_version_number": row[7],
        "current_content_hash": row[8],
        "current_head": row[9],
        "current_members": row[10] or [],
        "created_by": row[11],
        "created_at": row[12],
        "updated_at": row[13],
    }


def list_measurement_grains(conn, *, project_id: str) -> list[dict[str, Any]]:
    """Every measurement grain of the project, current version first-class."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.id, g.name, g.description, g.status, v.id, v.version_number,
                   v.content_hash, v.head, v.members, g.updated_at
              FROM app.mdm_metric_dimensions g
              LEFT JOIN app.mdm_metric_dimension_versions v
                     ON v.id = g.current_version_id
             WHERE g.project_id = %s
             ORDER BY g.status, lower(g.name)
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "status": row[3],
            "current_version": {
                "id": row[4],
                "version_number": row[5],
                "content_hash": row[6],
                "head": row[7],
                "members": row[8] or [],
            }
            if row[4]
            else None,
            "member_count": len(row[8] or []),
            "updated_at": row[9].isoformat() if row[9] is not None else None,
        }
        for row in rows
    ]


def derived_coverage(
    conn, *, project_id: str, head: Mapping[str, Any] | None, members: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Which published Datastream mappings implement the head and each member, NOW.

    The mirror of `mdm_common_keys.mapping_coverage`, with the ONE thing 71.2 adds
    that the common key never needed: a grain has a HEAD, so a Datastream can bind
    the measure but not one of its dimensions. That Datastream is `partial`, and
    its missing dimensions are NAMED -- never rendered as covered, never hidden in
    a zero. The states, per Datastream:

      * `unknown`  -- publishes no mapping version. Never counted as covered, the
                      rule `governance.md:84` already states for concept coverage.
      * `full`     -- binds the head AND every declared member (a grain of zero
                      members is `full` the moment its head is bound: a total).
      * `partial`  -- binds the head but not every member; the gap is the work.
      * `absent`   -- publishes a mapping, but does not bind the head. The grain is
                      anchored on its measure: a source that does not carry the
                      metric reports it at no breakdown at all.

    THREE STATES OF THE READ ITSELF, kept apart exactly as `mapping_coverage`
    keeps them: a Datastream with no published mapping is `unknown` (listed on its
    own row, never folded into a fraction), and a read that FAILED returns
    ``state: unavailable`` with no counts -- "I could not look" is not "nobody
    binds it", and only one of them invites a person to go and map something. This
    surface is a DISPLAY read, so it does not raise; the used-by read, which gates
    the archive, does.
    """
    head_field_id = str((head or {}).get("canonical_field_id") or "")
    member_fields = [
        {
            "canonical_field_id": str((member or {}).get("canonical_field_id") or ""),
            "canonical_name": (member or {}).get("canonical_name"),
            "ordinal": (member or {}).get("ordinal"),
        }
        for member in (members or [])
    ]
    wanted = {head_field_id, *(m["canonical_field_id"] for m in member_fields)}
    wanted.discard("")

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.name, d.current_mapping_version_id, m.mapping_payload
                  FROM app.datastreams d
                  LEFT JOIN app.datastream_mapping_versions m
                         ON m.id = d.current_mapping_version_id
                 WHERE d.project_id = %s
                 ORDER BY lower(d.name)
                """,
                (project_id,),
            )
            rows = cur.fetchall()
    except Exception:  # pragma: no cover - exercised through the API's 503 path
        return {
            "state": "unavailable",
            "head": None,
            "members": [],
            "datastreams": [],
            "unknown_datastreams": None,
            "counts": None,
        }

    # Each published Datastream, reduced to the wanted canonical fields it binds:
    # {canonical_field_id: {physical_field_id, physical_type}}. First implementing
    # binding wins, the same way `mapping_coverage` takes the first and breaks.
    published: list[tuple[str, str, str, dict[str, dict[str, str]]]] = []
    unknown: list[dict[str, str]] = []
    for datastream_id, name, mapping_version_id, payload in rows:
        if not mapping_version_id or not isinstance(payload, dict):
            unknown.append({"id": datastream_id, "name": name})
            continue
        bound: dict[str, dict[str, str]] = {}
        for field in payload.get("fields") or []:
            if not isinstance(field, dict):
                continue
            binding = field.get("binding")
            if not isinstance(binding, dict):
                continue
            target = binding.get("mdm_target")
            if target not in wanted or str(target) in bound:
                continue
            if binding.get("status") not in IMPLEMENTING_BINDING_STATUSES:
                continue
            bound[str(target)] = {
                "physical_field_id": str(field.get("field_id") or ""),
                "physical_type": str(field.get("physical_type") or ""),
            }
        published.append((datastream_id, name, mapping_version_id, bound))

    def _implemented_by(field_id: str) -> list[dict[str, str]]:
        return [
            {
                "datastream_id": datastream_id,
                "datastream_name": name,
                "mapping_version_id": mapping_version_id,
                "physical_field_id": bound[field_id]["physical_field_id"],
                "physical_type": bound[field_id]["physical_type"],
            }
            for datastream_id, name, mapping_version_id, bound in published
            if field_id in bound
        ]

    datastreams: list[dict[str, Any]] = []
    counts = {"full": 0, "partial": 0, "absent": 0, "unknown": len(unknown)}
    for datastream_id, name, mapping_version_id, bound in published:
        head_bound = bool(head_field_id) and head_field_id in bound
        missing = [
            {
                "canonical_field_id": member["canonical_field_id"],
                "canonical_name": member["canonical_name"],
                "ordinal": member["ordinal"],
            }
            for member in member_fields
            if member["canonical_field_id"] not in bound
        ]
        if not head_bound:
            state = "absent"
        elif missing:
            state = "partial"
        else:
            state = "full"
        counts[state] += 1
        datastreams.append(
            {
                "datastream_id": datastream_id,
                "datastream_name": name,
                "mapping_version_id": mapping_version_id,
                "coverage": state,
                "head_bound": head_bound,
                # Named, never a hidden zero: a partial's gap IS the work.
                "missing_dimensions": missing,
            }
        )

    return {
        "state": "available",
        "head": {
            "canonical_field_id": head_field_id,
            "canonical_name": (head or {}).get("canonical_name"),
            "implemented_by": _implemented_by(head_field_id) if head_field_id else [],
        },
        "members": [
            {
                "canonical_field_id": member["canonical_field_id"],
                "canonical_name": member["canonical_name"],
                "ordinal": member["ordinal"],
                "implemented_by": _implemented_by(member["canonical_field_id"]),
            }
            for member in member_fields
        ],
        "datastreams": datastreams,
        # On its own row, never folded into a fraction: a head covered 1/1 beside
        # eight unmapped Datastreams must not read as a finished grain.
        "unknown_datastreams": unknown,
        "counts": counts,
    }


def grain_used_by(
    conn, *, project_id: str, measurement_grain_id: str
) -> list[dict[str, Any]]:
    """What depends on a version of this grain, or an exception -- NEVER a bare ().

    A consumer pins a grain by its VERSION id (the head is a name, the version is
    the contract), and the referenceable surface today is the `master_data_refs`
    of a Semantic View or Semantic Concept version -- the array where a published
    semantic version records the MDM/master-data versions it is built on.

    THE ANALYTICS-ALIGNMENT CALLER ARRIVED IN 71.4 AND DOES NOT APPEAR HERE, ON
    PURPOSE. The sentence this docstring used to carry -- *"when
    `capabilities/analytics-alignment.md` gains a caller it will pin the grain
    version it slices by here (or in a column carved for it)"* -- now has its
    answer, and the answer is no. `analytics_alignment_read.sanction_breakdown` is
    a READ: it persists nothing, so there is no row to list and no column to carve.
    The version it sliced by travels on its payload (`sliced_by`), where the reader
    who asked for the slice needs it. Reading what IS referenceable stays this
    function's whole job, and it returns the pins it finds -- normally none,
    honestly none.

    EVERY pin is returned, history included, each saying the LIFECYCLE of the
    version it lives in, exactly as `mdm_common_keys.used_by` and
    `business_taxonomy.domain_used_by` do: a caller that wants what still holds
    filters on :data:`LIVE_CONSUMER_STATUSES`; one that wants the whole story has
    it. Filtering here would make the two impossible to tell apart.

    ANY read failure raises :class:`MetricDimensionsUnavailable`. The archive gate
    treats that as a hard block, so a grain is never retired on a dependency read
    that did not run -- the `master_data.fetch_used_by` doctrine, the reason a
    swallowed error once read as "nothing depends on this".
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.kind, v.name, v.status, elem->>'version_id'
                  FROM (
                      SELECT id, 'semantic-concept' AS kind, name, status,
                             master_data_refs, project_id
                        FROM app.semantic_concept_versions
                      UNION ALL
                      SELECT id, 'semantic-view' AS kind, name, status,
                             master_data_refs, project_id
                        FROM app.semantic_view_versions
                  ) v
                  CROSS JOIN LATERAL jsonb_array_elements(v.master_data_refs) AS elem
                 WHERE v.project_id = %(project_id)s
                   AND elem->>'version_id' IN (
                       SELECT id
                         FROM app.mdm_metric_dimension_versions
                        WHERE metric_dimensions_id = %(grain)s
                          AND project_id = %(project_id)s
                   )
                 ORDER BY v.kind, lower(v.name)
                """,
                {"project_id": project_id, "grain": measurement_grain_id},
            )
            rows = cur.fetchall()
    except Exception as exc:  # any read failure must fail closed
        raise MetricDimensionsUnavailable(
            "the measurement-grain used-by store is unreadable"
        ) from exc
    return [
        {
            "version_id": row[0],
            "object_type": row[1],
            "name": row[2],
            "status": row[3],
            "grain_version_id": row[4],
            #: Derived, never stored: what a reader actually needs to decide.
            "still_holds": str(row[3]) in LIVE_CONSUMER_STATUSES,
        }
        for row in rows
    ]


def read_measurement_grain(
    conn, *, project_id: str, measurement_grain_id: str
) -> dict[str, Any]:
    """One grain, its versions, its DERIVED coverage and its used-by (story 71.2).

    Coverage is the mapping-coverage read reflected onto the measure axis, with
    the head/partial distinction 71.2 owns; used-by lists what pins a version of
    the grain, and raises rather than return a bare tuple when its store is
    unreadable -- the same door the common key's read grew both onto.
    """
    head = _load_grain_row(
        conn, project_id=project_id, measurement_grain_id=measurement_grain_id
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version_number, content_hash, head, members,
                   created_by, created_at
              FROM app.mdm_metric_dimension_versions
             WHERE metric_dimensions_id = %s AND project_id = %s
             ORDER BY version_number DESC
            """,
            (measurement_grain_id, project_id),
        )
        version_rows = cur.fetchall()

    return {
        "id": head["id"],
        "name": head["name"],
        "description": head["description"],
        "status": head["status"],
        "current_version": {
            "id": head["current_version_id"],
            "version_number": head["current_version_number"],
            "content_hash": head["current_content_hash"],
            "head": head["current_head"],
            "members": head["current_members"],
        }
        if head["current_version_id"]
        else None,
        "versions": [
            {
                "id": row[0],
                "version_number": row[1],
                "content_hash": row[2],
                "head": row[3],
                "members": row[4] or [],
                "created_by": row[5],
                "created_at": row[6].isoformat() if row[6] is not None else None,
            }
            for row in version_rows
        ],
        "coverage": derived_coverage(
            conn,
            project_id=project_id,
            head=head["current_head"],
            members=head["current_members"] or [],
        ),
        "used_by": grain_used_by(
            conn, project_id=project_id, measurement_grain_id=measurement_grain_id
        ),
    }


def archive_measurement_grain(
    conn, *, project_id: str, measurement_grain_id: str, actor: str
) -> dict[str, Any]:
    """Archive a grain, freeing its name -- unless a live read is still cut by it.

    The used-by guard is the fourth property of the amendment: a grain cannot be
    archived while a Semantic View concept or an analytics-alignment read still
    slices by one of its versions. `grain_used_by` raises
    :class:`MetricDimensionsUnavailable` when its store is unreadable, and that
    propagates -- the archive fails CLOSED, never on an unread dependency. Only a
    LIVE pin blocks (`still_holds`): a `superseded` or `archived` consumer version
    is history, and history nobody can revive must not refuse a retirement
    forever -- the defect `mdm_common_keys.archive_common_key` names, reflected.

    Once nothing live depends on it, archiving is the plain retirement: nothing is
    deleted, the versions a consumer pinned stay readable, and the name is free to
    be re-declared.
    """
    head = _load_grain_row(
        conn, project_id=project_id, measurement_grain_id=measurement_grain_id
    )
    live = [
        entry
        for entry in grain_used_by(
            conn, project_id=project_id, measurement_grain_id=measurement_grain_id
        )
        if entry["still_holds"]
    ]
    if live:
        # A draft and a published consumer both block, for different reasons: one
        # serves today, the other the day someone publishes it. Named, not counted
        # blind, so the person knows where to go and retire the slice.
        where = ", ".join(
            sorted({f"{entry['name']} ({entry['status']})" for entry in live})
        )
        plural = "s" if len(live) != 1 else ""
        raise MeasurementGrainRefused(
            "measurement_grain_in_use",
            f"{head['name']!r} is still cut by {len(live)} live read{plural} "
            f"({where}). Retire the reads that slice by this grain before "
            "archiving the grain they use.",
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_metric_dimensions SET status = 'archived', "
            "updated_at = NOW() WHERE id = %s AND project_id = %s AND status = 'active'",
            (measurement_grain_id, project_id),
        )
        if cur.rowcount == 0:
            raise MeasurementGrainRefused(
                "measurement_grain_already_archived",
                f"{head['name']!r} is already archived.",
            )
    _record(
        conn,
        ACTION_MEASUREMENT_GRAIN_ARCHIVED,
        actor=actor,
        project_id=project_id,
        measurement_grain_id=measurement_grain_id,
        name=head["name"],
    )
    return {"id": measurement_grain_id, "status": "archived", "archived_by": actor}
