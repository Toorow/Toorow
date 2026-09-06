"""toorow -- the immutable history of a transformation rule (Story 60.5).

Two families of rules were shipped by this batch with no history at all: the
client's own value mapping tables (60.1, migration 235) and the cleanup rules
(60.3, migration 240). Every write was an UPDATE in place, so what a pattern or a
normalised value was yesterday was lost and nobody could date the change.
Migration 242 gives each family its own append-only ledger; this module is the
ONLY writer of both.

FOUR CONTRACTS THIS MODULE EXISTS TO HOLD.

1. **The hash shown before the act is the hash that gets written.** The
   confirmation preview and the write call the SAME :func:`content_hash` over the
   SAME body built by the same function. A preview computed by a second code path
   would eventually disagree with what landed, and the number a person confirmed
   would stop being the number that happened.

2. **A version is per OBJECT, never per pair.** The body of a value table carries
   its name, its description, its scope and EVERY pair sorted by source value, so
   importing five hundred pairs is one version. `145:373` is the precedent for the
   `UNIQUE (object, content_hash)` that comes with it -- read here as the IDENTITY
   of a body: the ledger holds each DISTINCT body the object has carried, numbered
   in order of first appearance, and the parent's `current_version_id` says which
   one it is at. Editing a pattern from B back to A moves the head back to the
   version that already holds A and records nothing new. A DQ Monitor can refuse
   that outright because republishing a policy is a no-op; a rule a client edits
   freely cannot, and a ledger that refused it would forbid an undo.

3. **A fan-out that could not be read is `unknown`, never `0`.** The two impact
   readers this module calls (`value_mapping_tables.assess_table_impact` and
   `cleanup_rules.assess_rule_impact`) both RAISE rather than answering an
   encouraging zero, and :func:`read_fan_out` keeps that distinction all the way
   to the surface. "I could not check" and "nothing depends on this" are two
   facts and only one of them is safe to act on.

4. **Nothing here is applied at read time, and this module does not pretend
   otherwise.** Story 60.5 delivers the HISTORY of a rule. Whether the client's
   value table or the governed conformance store of migration 052 wins at render
   time is AI-238, and the precedence it will be closed with is written in the
   story rather than coded here -- see :data:`AI_238_PRECEDENCE`. No resolver is
   shipped, because code nothing calls is what this batch has already been
   rejected for.

AND NO REPLAY IS OFFERED. `run_origins.py:114-115` declares `bounded_reprocess`
with ``has_engine=False`` -- a measurement of the build, not an opinion -- and
`122:23-30` states why a rule applied at read needs no replay at all: "changing a
regex costs nothing -- no refetch, no 16-month backfill". This module adds no
verb of its own.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from core.metric_semantics import _mint_id

logger = logging.getLogger(__name__)

KIND_VALUE_TABLE = "value-mapping-table"
KIND_CLEANUP_RULE = "cleanup-rule"

#: True of every governed change applied at READ -- a cleanup rule, a value
#: mapping table, a geographic regrouping -- because none of them touches the raw
#: zone, which is the append-only evidence of AD-7.
#:
#: It used to live in `core.geographic_change` and be imported from here. That
#: module carried the preview/confirm cycle of the retired project geographic
#: posture and was removed with it; this sentence was the only thing in it that
#: any production module still read, so it moved to its reader rather than
#: keeping a 700-line module alive to hold one string.
NO_BACKFILL_FACT = "No raw rewrite, no provider pull and no backfill are required."

#: The only status this module ever writes, and it never updates it afterwards.
#: There is no draft state for either family -- a pair the client typed and a
#: pattern the warehouse accepted are both live the moment they are written (235,
#: 240) -- and there is no `superseded` transition either, because which version
#: is current is a POINTER on the parent (`current_version_id`) rather than a
#: state on the row. That is what lets migration 242 refuse every UPDATE of a
#: version without an exception clause.
STATUS_PUBLISHED = "published"

#: AI-238, WRITTEN AND NOT CODED. The two stores answer with disjoint keys:
#: migration 052 resolves `(canonical_dimension, connector, source_value)` -- a
#: GOVERNED vocabulary that names no field -- and migration 235 resolves
#: `(datastream_id, source_field, source_value)` -- the vocabulary of ONE client,
#: explicitly assigned to ONE column. An assignment that names a field is a more
#: specific statement than a match at the scale of a connector, so the client's
#: table wins ON THE FIELD IT IS ASSIGNED TO TRANSLATE and 052 wins everywhere
#: else. This constant is documentation for whoever closes AI-238; nothing in this
#: repository reads either store at render time today, and shipping a resolver
#: here would be a second unreachable object beside the two this story is giving a
#: history to.
AI_238_PRECEDENCE = (
    "A client value mapping table wins on the exact field its assignment names; "
    "the governed conformance store of migration 052 wins everywhere else. The "
    "keys are disjoint, so this is a precedence and not a merge."
)


class RuleVersionError(Exception):
    """Base for every refusal of the version ledgers."""


class UnknownRuleFamily(RuleVersionError):
    """The object kind is not one of the two families this module versions."""


class RuleVersionUnavailable(RuleVersionError):
    """The ledger could not be read. The surface says so; it never says 'none'."""


class RuleVersionUnchanged(RuleVersionError):
    """The object is already at this exact body. Nothing to record.

    Raised only when the CURRENT head already carries the proposed body, which is
    a no-op rather than an edit. Returning to an OLDER body is not this: it moves
    the head and is reported through :func:`record_version`'s return value.
    """

    def __init__(self, message: str, *, version_number: int | None = None):
        super().__init__(message)
        self.version_number = version_number


@dataclass(frozen=True, slots=True)
class Ledger:
    """One family's ledger, and the two identifiers it is keyed on."""

    kind: str
    table: str
    object_column: str
    id_prefix: str
    parent_table: str
    #: True when the parent may be ORG-scoped, so `project_id` on a version row
    #: mirrors the parent and may legitimately be NULL (a value table, 235).
    project_optional: bool


LEDGERS: dict[str, Ledger] = {
    KIND_VALUE_TABLE: Ledger(
        kind=KIND_VALUE_TABLE,
        table="value_mapping_table_versions",
        object_column="table_id",
        id_prefix="vmtv_",
        parent_table="value_mapping_tables",
        project_optional=True,
    ),
    KIND_CLEANUP_RULE: Ledger(
        kind=KIND_CLEANUP_RULE,
        table="cleanup_rule_versions",
        object_column="rule_id",
        id_prefix="crlv_",
        parent_table="cleanup_rules",
        project_optional=False,
    ),
}


def ledger_for(kind: str) -> Ledger:
    try:
        return LEDGERS[kind]
    except KeyError as exc:
        raise UnknownRuleFamily(f"no version ledger for object kind {kind!r}") from exc


# ---------------------------------------------------------------------------
# The body and its hash. PURE -- no I/O, offline-testable.
# ---------------------------------------------------------------------------


def content_hash(body: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of one body.

    `sort_keys` and a fixed separator are what make the hash a property of the
    BODY rather than of the dict that happened to carry it: two equal bodies built
    in two different orders must hash the same, or the UNIQUE content constraint
    of migration 242 would refuse nothing.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def value_table_body(
    table: dict[str, Any], pairs: list[tuple[str, str]] | list[list[str]]
) -> dict[str, Any]:
    """The whole body of a value mapping table, pairs SORTED.

    Sorted because the ledger records what the table IS, not the order a cursor
    returned it in: an identical table read twice must produce one hash.
    """
    ordered = sorted(([str(a), str(b)] for a, b in pairs), key=lambda pair: (pair[0], pair[1]))
    return {
        "name": table.get("name"),
        "description": table.get("description"),
        "scope_level": table.get("scope_level"),
        "pairs": ordered,
    }


def cleanup_rule_body(rule: dict[str, Any]) -> dict[str, Any]:
    """The whole body of a cleanup rule.

    `enabled` is deliberately absent, and migration 242 states the same exclusion
    in its header: switching a rule off is its LIFECYCLE, the way
    `lifecycle_status` is the lifecycle of a DQ Monitor and lives on the parent in
    migration 145. Hashing it would make the UNIQUE content constraint refuse the
    third toggle of a rule that was switched off and back on.
    """
    return {
        "name": rule.get("name"),
        "source_field": rule.get("source_field"),
        "rule_kind": rule.get("rule_kind"),
        "pattern": rule.get("pattern"),
        "datastream_id": rule.get("datastream_id"),
    }


# ---------------------------------------------------------------------------
# Reading the ledger.
# ---------------------------------------------------------------------------


def _rows(cur) -> list[dict[str, Any]]:
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _projection(row: dict[str, Any], ledger: Ledger) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "object_kind": ledger.kind,
        "object_id": str(row[ledger.object_column]),
        "version_number": int(row["version_number"]),
        "status": row["status"],
        "content_hash": row["content_hash"],
        "predecessor_version_id": row.get("predecessor_version_id"),
        "body": row.get("body"),
        "created_by": row.get("created_by"),
        "created_at": _iso(row.get("created_at")),
    }


def list_versions(conn, *, kind: str, object_id: str) -> list[dict[str, Any]]:
    """Every recorded version of one object, newest first.

    Raises:
        RuleVersionUnavailable: the ledger could not be read. An empty list means
            the ledger answered and this object has no version yet; the two are
            different sentences on the screen and they are different answers here.
    """
    ledger = ledger_for(kind)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, {ledger.object_column}, version_number, status, content_hash, "  # noqa: S608 -- identifiers come from LEDGERS, never from a caller
                f"predecessor_version_id, body, created_by, created_at "
                f"FROM app.{ledger.table} WHERE {ledger.object_column} = %s "
                f"ORDER BY version_number DESC",
                (object_id,),
            )
            rows = _rows(cur)
    except Exception as exc:  # noqa: BLE001 -- an unreadable ledger is not an empty one
        raise RuleVersionUnavailable(
            f"the version history of {object_id} could not be read: {type(exc).__name__}"
        ) from exc
    return [_projection(row, ledger) for row in rows]


def head_version(conn, *, kind: str, object_id: str) -> dict[str, Any] | None:
    """The version the object is AT right now, or None when it has no history.

    Read from the parent's ``current_version_id`` rather than from the highest
    number, because those two are not the same fact: an object that was edited
    back to a body it already carried points at that older version, and calling
    the highest number "current" would report a body the object does not have.
    """
    ledger = ledger_for(kind)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT v.id, v.{ledger.object_column}, v.version_number, v.status, "  # noqa: S608 -- identifiers come from LEDGERS, never from a caller
                f"v.content_hash, v.predecessor_version_id, v.body, v.created_by, v.created_at "
                f"FROM app.{ledger.parent_table} p "
                f"JOIN app.{ledger.table} v ON v.id = p.current_version_id "
                f"WHERE p.id = %s",
                (object_id,),
            )
            rows = _rows(cur)
    except Exception as exc:  # noqa: BLE001
        raise RuleVersionUnavailable(
            f"the version history of {object_id} could not be read: {type(exc).__name__}"
        ) from exc
    return _projection(rows[0], ledger) if rows else None


# ---------------------------------------------------------------------------
# Writing one version. Append, then advance the head -- one transaction.
# ---------------------------------------------------------------------------


def _existing_version(conn, ledger: Ledger, object_id: str, digest: str) -> dict[str, Any] | None:
    """The version that already carries this exact body, if the object had it."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, {ledger.object_column}, version_number, status, content_hash, "  # noqa: S608 -- identifiers come from LEDGERS, never from a caller
            f"predecessor_version_id, body, created_by, created_at "
            f"FROM app.{ledger.table} WHERE {ledger.object_column} = %s AND content_hash = %s",
            (object_id, digest),
        )
        rows = _rows(cur)
    return _projection(rows[0], ledger) if rows else None


def _next_number(conn, ledger: Ledger, object_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COALESCE(MAX(version_number), 0) + 1 "  # noqa: S608 -- identifiers come from LEDGERS, never from a caller
            f"FROM app.{ledger.table} WHERE {ledger.object_column} = %s",
            (object_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 1


def record_version(
    conn,
    *,
    kind: str,
    object_id: str,
    org_id: str,
    project_id: str | None,
    body: dict[str, Any],
    identity: str,
) -> dict[str, Any]:
    """Record where this object now stands, and point its head at it.

    Three outcomes, and each is a different fact:

    * the body is NEW -- one immutable row is appended, numbered after the
      object's highest, and the parent's ``current_version_id`` moves to it;
    * the body is one the object ALREADY carried -- nothing is appended and the
      pointer moves BACK to that version. `revisited` says so in the result;
    * the body is the one the object is already at -- :class:`RuleVersionUnchanged`,
      because there is no change to record.

    Every statement runs on the CALLER's connection and therefore inside the
    caller's transaction: a rule whose row changed without its history moving --
    or the reverse -- is the exact state this ledger exists to make impossible.
    """
    ledger = ledger_for(kind)
    digest = content_hash(body)
    head = head_version(conn, kind=kind, object_id=object_id)
    if head is not None and head["content_hash"] == digest:
        raise RuleVersionUnchanged(
            "Nothing to record: this object is already at version "
            f"{head['version_number']}, whose body is identical.",
            version_number=head["version_number"],
        )

    revisited = _existing_version(conn, ledger, object_id, digest)
    if revisited is not None:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE app.{ledger.parent_table} SET current_version_id = %s WHERE id = %s",  # noqa: S608 -- identifiers come from LEDGERS
                (revisited["id"], object_id),
            )
        return {**revisited, "revisited": True}

    version_id = _mint_id(ledger.id_prefix)
    number = _next_number(conn, ledger, object_id)
    predecessor = None if head is None else head["id"]
    if number > 1 and predecessor is None:
        # The lineage CHECK of migration 242 forbids a numbered version with no
        # predecessor. It can only happen if the pointer was lost, and inventing
        # one would fabricate a chain that was never observed.
        raise RuleVersionUnavailable(
            f"{object_id} carries versions but no current head; its lineage cannot be continued"
        )
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO app.{ledger.table} "  # noqa: S608 -- identifiers come from LEDGERS, never from a caller
            f"(id, {ledger.object_column}, org_id, project_id, version_number, status, "
            f"body, content_hash, predecessor_version_id, created_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)",
            (
                version_id,
                object_id,
                org_id,
                project_id,
                number,
                STATUS_PUBLISHED,
                json.dumps(body, sort_keys=True, ensure_ascii=False),
                digest,
                predecessor,
                identity,
            ),
        )
        cur.execute(
            f"UPDATE app.{ledger.parent_table} SET current_version_id = %s WHERE id = %s",  # noqa: S608 -- identifiers come from LEDGERS
            (version_id, object_id),
        )
    return {
        "id": version_id,
        "object_kind": ledger.kind,
        "object_id": object_id,
        "version_number": number,
        "status": STATUS_PUBLISHED,
        "content_hash": digest,
        "predecessor_version_id": predecessor,
        "body": body,
        "revisited": False,
    }


# ---------------------------------------------------------------------------
# The fan-out. Read BEFORE the refusal, and never a default zero.
# ---------------------------------------------------------------------------


def read_fan_out(
    conn, *, kind: str, object_id: str, project_id: str | None = None
) -> dict[str, Any]:
    """Which Datastreams this change reaches, NAMED, or an honest `unknown`.

    A value table reuses `value_mapping_tables.assess_table_impact` unchanged: it
    already returns `datastream_id`, `datastream_name` and `source_field`, and it
    already raises rather than returning an empty tuple.

    A cleanup rule bound to no Datastream reaches EVERY Datastream of its Project
    -- `app.cleanup_rules.datastream_id` is nullable and that is what NULL means
    (migration 240). `cleanup_rules.assess_rule_impact` answers that count and
    raises when it cannot.

    Never raises: a fan-out that could not be read is reported as
    ``impact_state: "unknown"`` with no count at all, because the caller has to
    render the difference rather than crash on it.
    """
    ledger = ledger_for(kind)
    unknown = {
        "impact_state": "unknown",
        "datastream_count": None,
        "datastreams": [],
        "reason": None,
    }
    try:
        if ledger.kind == KIND_VALUE_TABLE:
            from core.value_mapping_tables import assess_table_impact  # noqa: PLC0415

            impact = assess_table_impact(conn, table_id=object_id)
            return {
                "impact_state": "known",
                "datastream_count": impact.datastream_count,
                "datastreams": [dict(assignment) for assignment in impact.assignments],
                "reason": None,
            }

        from core.cleanup_rules import assess_rule_impact, get_rule  # noqa: PLC0415

        rule = get_rule(conn, rule_id=object_id, project_id=project_id or "")
        impact = assess_rule_impact(
            conn, project_id=rule["project_id"], datastream_id=rule.get("datastream_id")
        )
        return {
            "impact_state": "known",
            "datastream_count": impact.datastream_count,
            "datastreams": [dict(entry) for entry in impact.datastreams],
            "reach": "datastream" if rule.get("datastream_id") else "project",
            "reason": None,
        }
    except Exception as exc:  # noqa: BLE001 -- an unreadable fan-out is never a zero
        logger.warning(
            "rule_versions: fan-out unreadable kind=%s object=%s: %s", kind, object_id, exc
        )
        return {**unknown, "reason": type(exc).__name__}


def preview_change(
    conn,
    *,
    kind: str,
    object_id: str,
    body: dict[str, Any],
    project_id: str | None = None,
) -> dict[str, Any]:
    """What confirming this change would record, BEFORE it is recorded.

    The version number, the content hash of the proposed body, the Datastreams
    the change reaches BY NAME, and the sentence that separates the future from
    the past.

    That sentence is `NO_BACKFILL_FACT`, declared once at the top of this module.
    It is the GENERIC half of a statement the retired `geographic_change` module
    used to compose: the other half -- "retained country data is reclassified
    semantically" -- is true of a posture change and false twice over a table of
    campaign names, which is why only the generic half was ever reused here.
    """
    ledger = ledger_for(kind)
    digest = content_hash(body)
    head: dict[str, Any] | None = None
    revisited: dict[str, Any] | None = None
    try:
        head = head_version(conn, kind=kind, object_id=object_id)
        revisited = _existing_version(conn, ledger, object_id, digest)
        next_number = _next_number(conn, ledger, object_id)
        history_state = "available"
    except Exception as exc:  # noqa: BLE001 -- an unreadable ledger is not an empty one
        logger.warning("rule_versions: ledger unreadable object=%s: %s", object_id, exc)
        next_number = None
        history_state = "unavailable"

    fan_out = read_fan_out(conn, kind=kind, object_id=object_id, project_id=project_id)
    unchanged = head is not None and head["content_hash"] == digest
    return {
        "object_kind": ledger.kind,
        "object_id": object_id,
        "history_state": history_state,
        "current_version_number": None if head is None else head["version_number"],
        "current_content_hash": None if head is None else head["content_hash"],
        # The version this confirmation would create -- or, when the object has
        # already carried this exact body, the version it would RETURN TO. `None`
        # when the ledger could not be read: naming "version 1" over a history
        # nobody could read would be a number invented for a screen.
        "next_version_number": (
            None
            if history_state == "unavailable" or unchanged
            else (revisited["version_number"] if revisited else next_number)
        ),
        "returns_to_existing_version": revisited is not None and not unchanged,
        "content_hash": digest,
        "unchanged": unchanged,
        "backfill_statement": NO_BACKFILL_FACT,
        **fan_out,
    }


__all__ = [
    "AI_238_PRECEDENCE",
    "KIND_CLEANUP_RULE",
    "KIND_VALUE_TABLE",
    "LEDGERS",
    "STATUS_PUBLISHED",
    "Ledger",
    "RuleVersionError",
    "RuleVersionUnavailable",
    "RuleVersionUnchanged",
    "UnknownRuleFamily",
    "cleanup_rule_body",
    "content_hash",
    "head_version",
    "ledger_for",
    "list_versions",
    "preview_change",
    "read_fan_out",
    "record_version",
    "value_table_body",
]
