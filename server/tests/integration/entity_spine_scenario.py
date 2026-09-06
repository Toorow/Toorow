"""The chain of epics 68-69, driven as a scenario rather than as an assertion -- AI-303.

WHY IT IS A MODULE AND NOT A COPY INSIDE THE CAPTURE SCRIPT. The seed lot has to
be what the product PRODUCES, or it is demonstration content wearing a fixture's
clothes. The only thing that can produce it is the chain, and the chain is
already written down once, in
`test_entity_chain_end_to_end_pg.py`. So the mappings, the CSV shapes and the
rule are IMPORTED from that file rather than retyped: a change to the chain that
this scenario would no longer survive breaks here, loudly, instead of quietly
seeding a shape the product stopped producing.

WHAT THIS ADDS TO THE CHAIN, and why the chain does not have it. The test proves
the seven links up to a crossable classification, and its facts Datastream never
imports anything -- `_record_verdicts` doubles the raw read on purpose, because
building the warehouse half there would have made a Postgres test a disguised dbt
build. A seed lot needs exactly that half: a FACTS file, landed in a warehouse
relation, keyed by the entity. It is added here, where a warehouse is the point.

THE FACTS FILE TAKES THE WAREHOUSE ROUTE BY DECLARATION, NOT BY LUCK.
`entity_reference_import.reference_designation` sends a file to the reference
route only when the designating column IS the file's identity and the projection
declares no measure. This mapping keys on `(day, video_id)` and declares `views`
additive, so it is facts about the entity -- the same two sentences that module
uses to tell the two apart.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

_SERVER_DIR = Path(__file__).resolve().parents[2]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

#: The chain, imported. Every constant below that names a column, a rule or a
#: CSV comes from the file that proves the chain works.
from tests.integration import test_entity_chain_end_to_end_pg as chain  # noqa: E402

FACTS_MAPPING: dict[str, Any] = {
    "grain": ["day", "video_id"],
    "fields": [
        {
            "field_id": "day",
            "physical_type": "date",
            # THE ROLE, DECLARED. `app.managed_feed_grain_v` derives `date_column`
            # from `suggestion.semantic_role`, and `fact_daily_kpi`'s managed
            # branch emits nothing for a feed with no day. A first draft of this
            # scenario omitted the roles and produced a warehouse where the whole
            # chain built and NOT ONE managed row reached the fact -- green, and
            # about nothing.
            "suggestion": {"semantic_role": "primary_date"},
            "binding": {"status": "confirmed", "canonical_target": "date"},
        },
        {
            "field_id": "video_id",
            "physical_type": "string",
            "suggestion": {"semantic_role": "dimension"},
            "binding": {
                "status": "confirmed",
                "canonical_target": "video_id",
                # The SAME designation the catalogue carries. It is what makes
                # these rows joinable to the entity -- and it is not enough on
                # its own to make them a catalogue, which is the point.
                "designates_object_kind": chain.KIND,
            },
        },
        {
            "field_id": "views",
            "physical_type": "integer",
            "suggestion": {"semantic_role": "measure", "non_additive": False},
            "binding": {"status": "confirmed", "canonical_target": "views"},
        },
    ],
}

FACTS_PROJECTION: dict[str, Any] = {
    "executable": True,
    "grain": ["day", "video_id"],
    # THE LINE THAT DECIDES THE ROUTE. A file with measures carries facts.
    "additive_measures": ["views"],
    "full_grain_relation": {
        "grain_columns": [{"field_id": "day"}, {"field_id": "video_id"}],
        "source_fields": ["day", "video_id", "views"],
    },
}

#: Two videos over two days, the same two the catalogue declares (`v-1` at 42
#: seconds, `v-2` at 3600) so the rule classifies one `short` and one `long` and
#: a cross has both sides of its own answer to show.
FACTS_CSV = (
    b"day,video_id,views\n"
    b"2026-08-01,v-1,120\n"
    b"2026-08-01,v-2,45\n"
    b"2026-08-02,v-1,90\n"
    b"2026-08-02,v-2,60\n"
)


def _facts_landing(conn, *, project_id: str, datastream_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT landing_table FROM app.managed_feed_grain_v "
            "WHERE project_id = %s AND datastream_id = %s",
            (project_id, datastream_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _read_landed(duckdb_path: str, relation: str) -> dict[str, Any]:
    """The rows the import actually wrote, read back from the warehouse.

    Read back rather than taken from the import's return value: what a seed lot
    must replay is what LANDED, columns and types included, not what a function
    said it accepted.
    """
    import duckdb  # noqa: PLC0415

    table = relation.rsplit(".", 1)[-1]
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        described = con.execute(f'DESCRIBE SELECT * FROM "{table}"').fetchall()
        columns = [(str(name), str(dtype)) for name, dtype, *_rest in described]
        cursor = con.execute(f'SELECT * FROM "{table}"')  # noqa: S608
        names = [description[0] for description in cursor.description]
        rows = [
            {
                name: (value if value is None or isinstance(value, (str, int, float, bool))
                       else str(value))
                for name, value in zip(names, record, strict=True)
            }
            for record in cursor.fetchall()
        ]
    finally:
        con.close()
    return {"relation": table, "columns": columns, "rows": rows}


def _promote(landed: dict[str, Any], *, shared_relation: str, project_id: str) -> None:
    """Append the candidate into the shared relation, the product's own way.

    `raw_landing.promote_candidate` is the same append a governed publication
    performs. Writing an INSERT here instead would make this file a second
    authority on what publishing means, and the lot would stop being a capture of
    what the product does.

    The columns are read from the writer seam's own record of what it landed
    (`landed_candidate_columns`), not recomposed from the mapping: a seed replays
    the relation that EXISTS, types included.
    """
    from core.raw_landing import landed_candidate_columns, promote_candidate  # noqa: PLC0415

    candidate = str((landed.get("landing") or {}).get("table") or "")
    execution_id = candidate.rsplit("__cand_", 1)[-1] if "__cand_" in candidate else ""
    if not execution_id:
        raise RuntimeError(
            f"the facts import did not land a candidate relation (table={candidate!r}); "
            "there is nothing to promote, and the shared relation would stay empty"
        )
    columns = landed_candidate_columns(execution_id, candidate)
    if not columns:
        raise RuntimeError(
            f"the writer seam recorded no columns for {candidate!r} -- promoting "
            "would have to guess the schema, which is how a seed stops matching "
            "the relation it claims to be"
        )
    promote_candidate(
        shared_relation,
        execution_id,
        columns=columns,
        project_id=project_id,
    )


def run(conn) -> dict[str, Any]:
    """Drive the whole chain plus a facts import. Returns what the seed lot needs.

    The connection is NOT committed here: the caller decides, because the capture
    reads the mirror views in the same transaction and a test may want it rolled
    back.
    """
    from core import entity_rule_derivation as erd  # noqa: PLC0415
    from core import master_data  # noqa: PLC0415
    from core import object_kind_registry as okr  # noqa: PLC0415

    from tests.integration.epic66_fixtures import make_project  # noqa: PLC0415

    org_id, project_id = make_project(conn, "AI-303 entity spine")

    declared = okr.declare_entity_type(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=chain.KIND,
        canonical_key="video_id",
        display_name="Videos",
        actor=chain.ACTOR,
    )
    registry = declared["registry"]

    catalogue = chain._datastream(
        conn, org_id=org_id, project_id=project_id, name="Catalogue",
        payload=chain.CATALOGUE_MAPPING,
    )
    chain._import(
        conn, catalogue, project_id=project_id,
        csv_bytes=b"video_id,duration_seconds\nv-1,42\nv-2,3600\n",
        payload=chain.CATALOGUE_MAPPING, projection=chain.CATALOGUE_PROJECTION,
    )

    calendar = chain._datastream(
        conn, org_id=org_id, project_id=project_id, name="Calendrier",
        payload=chain.CALENDAR_MAPPING,
    )
    chain._import(
        conn, calendar, project_id=project_id,
        csv_bytes=(
            "day,kind,headline\n"
            f"2026-08-01,{chain._known_event_type()},Spring push\n"
        ).encode(),
        payload=chain.CALENDAR_MAPPING, projection=chain.CALENDAR_PROJECTION,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, label FROM app.master_data_nodes WHERE registry_id = %s",
            (registry["id"],),
        )
        nodes = cur.fetchall()
    for node_id, label in nodes:
        master_data.record_alias(
            conn, org_id=org_id, node_id=str(node_id), namespace="catalogue",
            raw_value=str(label), relation="exact", actor=chain.ACTOR,
        )

    facts = chain._datastream(
        conn, org_id=org_id, project_id=project_id, name="Vues", payload=FACTS_MAPPING
    )
    chain._record_verdicts(conn, facts, project_id, org_id)

    version = erd.draft_entity_rule_set(
        conn, org_id=org_id, project_id=project_id, object_kind=chain.KIND,
        derived_attributes=chain.RULES, label="Video classifications", actor=chain.ACTOR,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.governance_rule_sets "
            "WHERE project_id = %s AND family = 'entity_derivation' AND name = %s",
            (project_id, chain.KIND),
        )
        head_id = cur.fetchone()[0]
    erd.publish_entity_rule_set(
        conn, project_id=project_id, rule_set_id=head_id,
        version_id=version["id"], actor=chain.ACTOR,
    )

    # The facts file, landed in a warehouse of its own. A throwaway DuckDB, never
    # the repository's seed: this function CAPTURES what landed, and the seeder is
    # what puts it into the seed.
    with tempfile.TemporaryDirectory() as tmp:
        warehouse = str(Path(tmp) / "capture.duckdb")
        previous = {
            "TOOROW_DB_MODE": os.environ.get("TOOROW_DB_MODE"),
            # `raw_landing._land_duckdb` reads THIS name, not `DUCKDB_PATH` --
            # measured, after a first run wrote its rows into whichever warehouse
            # the ambient environment named and then failed to read them back.
            "TOOROW_DUCKDB_PATH": os.environ.get("TOOROW_DUCKDB_PATH"),
        }
        os.environ["TOOROW_DB_MODE"] = "duckdb"
        os.environ["TOOROW_DUCKDB_PATH"] = warehouse
        try:
            # PROMOTED, not merely landed. An import writes its rows into an
            # execution-ISOLATED candidate relation (`raw__cand_<id>.…`), and
            # `stg_managed_feed_facts` never names one -- a candidate cannot
            # reach the marts before somebody publishes it. A seed lot made of
            # candidates would seed rows no model reads; measured on the first
            # run of this capture, which landed
            # `raw__cand_dse_….managed_feed_ds_…__cand_dse_…` and then could not
            # find the shared table.
            #
            # The promotion is the PRODUCT'S OWN (`raw_landing.promote_candidate`),
            # the same append the governed publication performs. Not a hand-written
            # INSERT: writing the promotion here would make this file a second
            # authority on what publishing means, and the seed would stop being a
            # capture of what the product does.
            landed = chain._import(
                conn, facts, project_id=project_id, csv_bytes=FACTS_CSV,
                payload=FACTS_MAPPING, projection=FACTS_PROJECTION,
            )
            relation = _facts_landing(conn, project_id=project_id, datastream_id=facts["id"])
            if not relation:
                raise RuntimeError(
                    "the facts import landed no warehouse relation -- "
                    f"outcome={landed.get('outcome')!r}; a seed lot cannot replay "
                    "rows that were never written"
                )
            _promote(landed, shared_relation=relation, project_id=project_id)
            captured_facts = _read_landed(warehouse, relation)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return {
        "org_id": org_id,
        "project_id": project_id,
        "object_kind": chain.KIND,
        "rule_set_version_id": version["id"],
        "datastreams": {
            "catalogue": catalogue["id"],
            "calendar": calendar["id"],
            "facts": facts["id"],
        },
        "facts": captured_facts,
    }


def unique_suffix() -> str:
    """A suffix a caller can use to keep two runs of this scenario apart."""
    return uuid.uuid4().hex[:10]
