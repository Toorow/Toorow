"""The header of the day grid: the stream's facts, and its mapping's pairs.

EXTRACTED FROM `datastream_daily_breakdown_api.py` -- lot B2. That module was 1293
lines against a repository that refuses 1000, and this lot added to it. What moved
is one responsibility whole: WHICH FIELDS the active mapping declares and what each
one becomes. Not one line of it changed on the way; the route imports the same
names and the tests that patch them still reach them.

AND IT IS THE KEY BETWEEN THE TWO READINGS. `source_field -> target_field` is what
`collected_mapped_pairing` joins on (amendment 12): the collected relation carries
the source names, the mapped relation the target names, and this module is where
the pairs are read. The mart's `metric` literal keys nothing here and never did.
"""

from __future__ import annotations

import re
from typing import Any


class DatastreamNotFound(LookupError):
    """No Datastream with this id in this project -- or in another one.

    Declared where it is RAISED. `datastream_daily_breakdown_api` imports it to
    turn it into a `404`; two declarations of one absence would let the route
    answer `503` to a stream it had itself decided was missing.
    """


#: WHICH OF THE TWO MAPPING STORES ANSWERED (story 58.2, arbitrage 2).
#:
#: There are two, and they describe DISJOINT populations. Measured on the
#: disposable cluster 2026-08-06: 44 Datastreams carry a row in the flat
#: `app.datastream_mappings`, 328 carry a version in
#: `app.datastream_mapping_versions`, and ZERO carry both -- both stores were
#: written the same day, so neither is dead. Reading only the flat one, which is
#: what 58.1 shipped, leaves 328 streams out of 372 with an empty header and no
#: way to tell that it was the READER and not the flux.
#:
#: So the versioned store answers first, the flat table is the fallback, and the
#: payload names the one that spoke. Without that name an empty header has to be
#: guessed at; with it, it can be read.
COLUMNS_FROM_MAPPING_VERSION = "mapping_version"
COLUMNS_FROM_FLAT_TABLE = "flat_table"

#: THE HEADER AND THE ROWS DO NOT JOIN, AND THE ROUTE SAYS SO.
#: `app.datastream_mappings.target_field` references `app.target_fields`; the
#: mart's `metric` is a LITERAL written into the dbt model
#: (`dbt/models/marts/fact_daily_kpi.sql`). There is no key between them, in

#: `WHERE d.id = %s AND d.project_id = %s` -- the shape AI-219 settled on. The
#: versions are read here and not derived: `app.datastreams` holds the CURRENT
#: plan and mapping version of the stream, and a screen that showed rows without
#: saying which mapping produced them is showing an unattributable number.
_FACTS_SQL = """
    SELECT d.module_name,
           d.current_plan_version_id,
           d.current_mapping_version_id,
           d.report_profile_id
    FROM app.datastreams d
    WHERE d.id = %s AND d.project_id = %s
"""

#: The double pill of story 58.2, read from the MAPPING and never from the mart.
#: The mart is long-form and has no column per field; the pairs a header needs
#: exist only here.
_COLUMNS_SQL = """
    SELECT m.source_field, m.target_field, m.is_key_column
    FROM app.datastream_mappings m
    JOIN app.datastreams d ON d.id = m.datastream_id
    WHERE m.datastream_id = %s AND d.project_id = %s
    ORDER BY m.is_key_column DESC, m.source_field ASC
"""

#: The ACTIVE mapping version of the stream, which is where 328 of the 372
#: Datastreams keep their fields. The current version wins; when the stream
#: points at none, the highest version number is read rather than nothing --
#: a version that exists and is not yet current still names the source fields.
_MAPPING_VERSION_SQL = """
    SELECT v.mapping_payload
    FROM app.datastream_mapping_versions v
    JOIN app.datastreams d ON d.id = v.datastream_id
    WHERE v.datastream_id = %s AND d.project_id = %s AND v.project_id = %s
    ORDER BY (v.id = d.current_mapping_version_id) DESC, v.version_number DESC
    LIMIT 1
"""

#: `binding.mdm_target` is an MDM identity (`mdm_<ULID>`), not a name. Rendering
#: it as the canonical pill would put an opaque id where a person expects a
#: field name; leaving it out would report a bound field as unmapped. It is
#: resolved. Measured: 603 of 708 bindings resolve, and the remaining 105 carry
#: no target at all -- a real state of a mapping, not a failure of this read.
_MDM_NAMES_SQL = """
    SELECT id, canonical_name
    FROM app.mdm_canonical_fields
    WHERE id = ANY(%s) AND (project_id = %s OR project_id IS NULL)
"""

#: The identity shape itself, straight off the CHECK of migration 032
#: (`ck_mdm_canonical_fields_id`). It is what tells an MDM IDENTITY apart from a
#: canonical NAME, and both arrive under the same key of the payload: reading
#: `binding.canonical_target` without this test is how
#: `mdm_6D13WZ6E18GSZTTEH1JPZBACYW` ends up rendered as the canonical pill of a
#: field -- an id shown where a person reads a name.
_MDM_IDENTITY = re.compile(r"^mdm_[0-9A-HJKMNP-TV-Z]{26}$")

def read_stream_facts(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    """The connector and the active versions, or `DatastreamNotFound`."""
    with conn.cursor() as cur:
        cur.execute(_FACTS_SQL, (datastream_id, project_id))
        row = cur.fetchone()
    if row is None:
        raise DatastreamNotFound(datastream_id)
    return {
        "connector": row[0] or "",
        "plan_version_id": row[1],
        "mapping_version_id": row[2],
        # NULL on 773 of the 842 live Datastreams (measured 2026-08-06). It is
        # what says WHICH relation of the connector this flux lands in, and story
        # 58.3 turns it from a decorative column into a read address.
        "report_profile_id": row[3],
    }


def _payload_of(value: Any) -> dict[str, Any]:
    """A `jsonb` column, whichever way the driver handed it over."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        import json  # noqa: PLC0415

        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _versioned_columns(conn, *, project_id: str, datastream_id: str) -> list[dict]:
    """The fields of the active mapping VERSION, key columns first.

    `mapping_payload.fields[]` carries `field_id` (the source name) and a
    `binding` whose `mdm_target` is an MDM identity. The identity is resolved to
    its `canonical_name`; a field with no binding keeps `target_field: null`,
    because an unbound field is a real state of a mapping and it is precisely the
    line a person opens this tab to repair.

    The key columns are the payload's OWN `grain`, not a guess: the grain is the
    list of fields the rows are keyed by, which is what "key column" means in the
    flat store beside it.
    """
    with conn.cursor() as cur:
        cur.execute(_MAPPING_VERSION_SQL, (datastream_id, project_id, project_id))
        row = cur.fetchone()
    if row is None:
        return []
    payload = _payload_of(row[0])
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return []

    grain = {
        str(entry)
        for entry in (payload.get("grain") or [])
        if isinstance(entry, (str, int))
    }

    entries: list[dict[str, Any]] = []
    wanted: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        source = field.get("field_id") or field.get("source_identity")
        if not source:
            continue
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        # ONE reference, whichever key of the payload carries it. Measured: 105 of
        # the 708 bindings leave `mdm_target` null and put the SAME identity under
        # `canonical_target`, and all 105 resolve. Reading only the first key made
        # the second one fall through to the raw string.
        reference = (
            binding.get("mdm_target")
            or field.get("mdm_target")
            or binding.get("canonical_target")
            or field.get("canonical_target")
        )
        reference = reference if isinstance(reference, str) and reference else None
        if reference and _MDM_IDENTITY.match(reference):
            wanted.add(reference)
        entries.append(
            {
                "source_field": str(source),
                "target_ref": reference,
                "is_key_column": str(source) in grain,
                "binding_status": binding.get("status"),
                # THE CLASSIFICATION, carried on the header it belongs to (story
                # 58.3, arbitrage 6). It is the only thing that can UNMASK a value
                # of the collected reading, and `unknown` -- what a payload with no
                # declared sensitivity answers -- masks. `mapping_versions`
                # already reads it from this exact place.
                "sensitivity": str(
                    (field.get("suggestion") or {}).get("sensitivity")
                    or field.get("sensitivity")
                    or "unknown"
                ),
            }
        )

    names: dict[str, str] = {}
    if wanted:
        with conn.cursor() as cur:
            cur.execute(_MDM_NAMES_SQL, (sorted(wanted), project_id))
            names = {row[0]: row[1] for row in cur.fetchall()}

    columns = []
    for entry in entries:
        reference = entry.pop("target_ref")
        if reference is None:
            target = None
        elif _MDM_IDENTITY.match(reference):
            # AN IDENTITY IS NEVER SHOWN AS A NAME. `mdm_6D13WZ6E18GSZTTEH1JPZBACYW`
            # in the canonical pill is not a canonical field, it is the absence of
            # one wearing a 30-character disguise -- and the reader has no way to
            # tell it apart from a name. An identity this registry cannot resolve
            # leaves the field UNMAPPED, which is the true state and the one a
            # person can act on.
            target = names.get(reference)
        else:
            # Already a readable canonical name (`media_cost_micros`), which is
            # what the older payloads carry. Passed through, not re-resolved.
            target = reference
        entry["target_field"] = target
        columns.append(entry)

    columns.sort(key=lambda column: (not column["is_key_column"], column["source_field"]))
    return columns


def _flat_columns(conn, *, project_id: str, datastream_id: str) -> list[dict]:
    """The source-to-canonical pairs of the flat store, key columns first.

    `target_field` is nullable by design (migration 023): a field the source
    reports and nothing maps yet is a real state of a mapping, and dropping it
    here would hide exactly the rows a person opens this tab to fix.
    """
    with conn.cursor() as cur:
        cur.execute(_COLUMNS_SQL, (datastream_id, project_id))
        rows = cur.fetchall()
    return [
        {
            "source_field": row[0],
            "target_field": row[1],
            "is_key_column": bool(row[2]),
            # The flat store has no binding lifecycle at all -- one row IS the
            # binding. `null` says that, where a `confirmed` copied over from
            # nowhere would claim a review that never happened.
            "binding_status": None,
            # And it carries no sensitivity either, so every one of its fields is
            # `unknown` -- which MASKS. That is the refusal by default working as
            # intended: an unclassified column is not an allowed one.
            "sensitivity": "unknown",
        }
        for row in rows
    ]


def read_mapping_columns(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    """The header of the grid, and the name of the store that answered.

    Story 58.2, arbitrage 2. The versioned store first, the flat table as the
    fallback -- measured, the two hold disjoint populations, and reading either
    one alone leaves the majority of the fluxes with a header they cannot
    explain.
    """
    columns = _versioned_columns(
        conn, project_id=project_id, datastream_id=datastream_id
    )
    if columns:
        return {"columns": columns, "source": COLUMNS_FROM_MAPPING_VERSION}
    columns = _flat_columns(conn, project_id=project_id, datastream_id=datastream_id)
    if columns:
        return {"columns": columns, "source": COLUMNS_FROM_FLAT_TABLE}
    return {"columns": [], "source": None}

def classifications_of(columns: list[dict[str, Any]]) -> dict[str, str]:
    """`{column name: sensitivity}` for the collected AND the mapped reading.

    ONE mapping row describes two column names -- the source's and the canonical
    one -- and the classification is a property of the FIELD, not of the spelling.
    So both names inherit it; anything neither name covers stays unclassified and
    is therefore masked.
    """
    classifications: dict[str, str] = {}
    for column in columns:
        sensitivity = str(column.get("sensitivity") or "unknown")
        for name in (column.get("source_field"), column.get("target_field")):
            if isinstance(name, str) and name:
                classifications[name] = sensitivity
    return classifications

