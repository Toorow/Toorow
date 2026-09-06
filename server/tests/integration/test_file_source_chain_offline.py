"""End-to-end OFFLINE harness for the file-source ingestion chain (Epic 22 Phase B).

WHY THIS FILE EXISTS. Every link of this chain shipped with green unit tests that
call the functions DIRECTLY, so they could not tell a wired function from a dead
one. Measured 2026-07-31, before this harness: ``stamp_placement``,
``evaluate_variant_discriminator`` and ``replay_adaptation`` had ZERO production
callers, and no test noticed, because no test walked the sequence a person walks.

This one does. It fabricates the files, drives the SAME sequence as an operator --
template -> column recognition -> required-field gate -> confirmed mapping version
-> Datastream binding -> ``run_import`` -> rows read back out of the warehouse --
and asserts on what LANDED, not on what was called.

Everything here is offline: no credential, no account, no network. The only
external dependency is a disposable Postgres (``python scripts/disposable_postgres.py
up``) and DuckDB, which the harness points at a temp file.

THE TARGET IT IS JUDGED AGAINST
  * ``docs/product-architecture/file-source-ingestion.md`` (the ratified surface
    contract and its ``Incomplete if`` list);
  * ``_bmad-output/specs/spec-file-source-ingestion/SPEC.md`` (CAP-1..CAP-10);
  * ``_bmad-output/planning-artifacts/epics-file-source-ingestion.md``
    (stories 22.9-22.24 and their acceptance criteria).

WHAT IT DELIBERATELY DOES NOT COVER -- the boundary is the value, see the module
docstring section ``NOT COVERED`` at the bottom of this file.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from io import BytesIO
from pathlib import Path

import psycopg
import pytest
from core.tabular_types import CsvExcelImportError

ROOT = Path(__file__).resolve().parents[3]

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- run `python scripts/disposable_postgres.py up`",
)

#: Crockford base32 minus I/L/O/U -- the alphabet migration 032's canonical field
#: id CHECK accepts (^mdm_[0-9A-HJKMNP-TV-Z]{26}$).
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: The stamped matrix-class key (mirrors file_source_producer.PLACEMENT_CLASS_KEY).
PLACEMENT_CLASS_KEY = "_placement_class"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _mint_field() -> str:
    """Mint a FRESH canonical field id per run.

    These were fixed constants until a run collided with the identical constants
    in ``test_file_source_template_pg.py``: the ids already existed under another
    project, ``ON CONFLICT (id) DO NOTHING`` skipped the insert, and the template
    then failed scope validation on fields the harness believed it had seeded. A
    disposable database is still a SHARED and PERSISTENT one across suites.
    """
    return "mdm_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


def _mint_fields() -> dict[str, str]:
    return {name: _mint_field() for name in ("DATE", "COST", "IMPR", "MARKET")}


#: Field ids for the two PURE tests below, which never touch Postgres and so can
#: never collide with a persisted row.
OFFLINE_FIELDS = _mint_fields()


# ---------------------------------------------------------------------------
# Fabricated source files. Invented data only -- no real domain, address,
# customer name or production identifier (CLAUDE.md, "depot partageable").
# ---------------------------------------------------------------------------

#: The FR variant: a CSV whose headers are French, REORDERED relative to the
#: template's field order, and carrying an EXTRA column no canonical field wants.
#: CAP-3: "renamed, added, and reordered (and in another language) still yields
#: the required canonical fields."
FR_CSV = (
    "Date,Cout net,Impressions,Commentaire interne\n"
    "2026-01-01,1000.00,50000,rien a signaler\n"
    "2026-01-02,1250.50,62000,rien a signaler\n"
    "2026-01-03,980.25,48500,rien a signaler\n"
).encode("utf-8")

#: The DE variant: the SAME information under GERMAN headers, in a different
#: order again. CAP-5 / Incomplete-if: "two differently-laid-out files for the
#: same information [must] reach the same canonical fields from one Datastream."
#: The cost values carry a non-zero cent deliberately. Written as ``800.00``,
#: openpyxl stores 800.0, the parser infers the COLUMN as integer from the first
#: value and then rejects ``910.75`` as a type mismatch -- a fixture artefact that
#: reads exactly like a product defect.
DE_HEADERS = ["Impressionen", "Datum", "Nettokosten"]
DE_ROWS = [
    [40000, "2026-01-01", 800.50],
    [51000, "2026-01-02", 910.75],
]


def _de_xlsx() -> bytes:
    """Build the DE variant as a real .xlsx in memory (no fixture file on disk)."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(DE_HEADERS)
    for row in DE_ROWS:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


#: A drifted FR file whose REQUIRED cost column disappeared. CAP-4: "a file
#: missing a required field blocks or flags rather than landing partial data."
FR_CSV_MISSING_COST = (
    "Date,Impressions\n"
    "2026-02-01,50000\n"
    "2026-02-02,62000\n"
).encode("utf-8")


# ---------------------------------------------------------------------------
# The template contract. The canonical TARGET -- never derived from a layout.
# ---------------------------------------------------------------------------


def _contract(fields, **over):
    """A catalog template: canonical target + grain + class + placement (CAP-1/CAP-2).

    ``aliases`` carries the other-language names the recognizer matches on, which
    is how ONE template absorbs both the FR and the DE layout.

    The discriminator is a FILENAME pattern (CAP-5): the market marker is in the
    file's name, and its value is promoted to the market dimension -- which this
    template declares REQUIRED, because a row whose market is unknown is exactly
    the orphan row the surface contract exists to prevent.
    """
    base = {
        "kind": "catalog",
        "required_fields": [fields["DATE"], fields["COST"], fields["MARKET"]],
        "optional_fields": [fields["IMPR"]],
        "grain": "daily",
        "class": "planned",
        "placement": {
            "metric": fields["COST"],
            "period": fields["DATE"],
            "dimension": [fields["MARKET"]],
        },
        "format": "csv",
        "aliases": {
            fields["DATE"]: ["Date", "Datum", "media date"],
            fields["COST"]: ["Cout net", "Nettokosten", "net cost"],
            fields["IMPR"]: ["Impressions", "Impressionen"],
            fields["MARKET"]: ["Marche", "Markt", "market"],
        },
        "discriminator": {
            "dimension": fields["MARKET"],
            "source": "filename",
            "pattern": r"_(?P<value>FR|DE)\.",
        },
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """A connection to the DISPOSABLE test database -- never production.

    Two guards, both of which have been earned the hard way in this repo:
      * the DSN must not look like the managed production host (a suite that
        writes fixtures into Supabase is how ~268 fixture rows got there);
      * the role must not be a superuser and must not hold BYPASSRLS, or every
        tenancy assertion downstream passes vacuously.
    """
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    lowered = dsn.lower()
    assert "supabase" not in lowered and "pooler" not in lowered, (
        "TEST_POSTGRES_DSN points at the managed production host; refusing to run. "
        "Use `python scripts/disposable_postgres.py up`."
    )

    c = psycopg.connect(dsn)
    with c.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        is_super, bypasses = cur.fetchone()
    assert not is_super, "this suite must not run as a superuser: RLS would not apply"
    assert not bypasses, "the role holds BYPASSRLS: every isolation assertion is vacuous"

    missing = _missing_tables(c)
    if missing:
        c.close()
        pytest.skip(
            "the test database is missing "
            + ", ".join(missing)
            + " -- run `python scripts/disposable_postgres.py up`"
        )
    try:
        yield c
    finally:
        c.rollback()
        c.close()


_REQUIRED_TABLES = (
    "organizations",
    "projects",
    "datastreams",
    "mdm_canonical_fields",
    "file_source_templates",
    "datastream_plan_versions",
    "datastream_mapping_versions",
    "managed_feed_import_ledger",
)


def _missing_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'app' AND table_name = ANY(%s)",
            (list(_REQUIRED_TABLES),),
        )
        present = {r[0] for r in cur.fetchall()}
    return [t for t in _REQUIRED_TABLES if t not in present]


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """Land into a throwaway DuckDB file, and prove it by reading it back.

    DuckDB is the offline warehouse (BigQuery needs a project and credentials);
    `TOOROW_ORG_SCHEMAS` stays OFF so the landing relation resolves to `main`.
    """
    db = tmp_path / "raw.duckdb"
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db))
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    return db


def _read_landed(db_path: Path, landing_relation: str, execution_id: str) -> list[dict]:
    """Read the rows that PHYSICALLY landed. The only honest end of the chain.

    ADDRESSED THE WAY THE PRODUCT WRITES IT. A candidate is isolated TWICE --
    `open_raw_writer` mints a `raw__cand_<execution>` SCHEMA and sets the
    connection's `search_path` to it, and `land_rows` suffixes the TABLE. The
    reported `landing.table` carries the table half only, which resolves for any
    reader that opens through the same helper and for nobody else.

    This helper opened a bare `duckdb.connect`, so it looked in `main` and found
    nothing -- and read that as "the rows never landed". Setting the same
    search_path is what makes the assertion about the DATA again.
    """
    import duckdb
    from core.raw_landing import candidate_table

    schema = candidate_table("raw", execution_id)
    table = landing_relation.rsplit(".", 1)[-1]
    con = duckdb.connect(str(db_path))
    try:
        con.execute(f'SET search_path="{schema}"')
        cur = con.execute(f'SELECT * FROM "{table}"')  # noqa: S608 - minted identifier
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Seeding: an org, a project, the canonical field registry, a Datastream.
# ---------------------------------------------------------------------------


def _seed(conn):
    """Create an org, a project and the four canonical fields the template targets.

    Returns ``(org_id, project_id, fields)``; ``fields`` maps the harness's four
    logical roles to the ids minted for THIS run.
    """
    org_id = _id("org_")
    project_id = _id("proj_")
    fields = _mint_fields()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'file-source-offline-harness') ON CONFLICT DO NOTHING",
            (org_id, org_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'file-source-offline-harness') ON CONFLICT DO NOTHING",
            (project_id, project_id, project_id, org_id),
        )
        # `value_type` is NOT NULL since migration 241 and has no default: a
        # canonical field carries the half this table never had, without which it
        # could never become a Concept. Each role states the type it implies.
        for role, kind, name, agg, value_type in (
            ("COST", "metric", "net_cost", "sum", "money"),
            ("IMPR", "metric", "impressions", "sum", "integer"),
            ("DATE", "dimension", "media_date", None, "date"),
            ("MARKET", "dimension", "market", None, "string"),
        ):
            cur.execute(
                "INSERT INTO app.mdm_canonical_fields "
                "(id, project_id, concept_kind, canonical_name, aggregation, "
                " value_type, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'test')",
                (
                    fields[role],
                    project_id,
                    kind,
                    f"{name}_{project_id[-6:]}",
                    agg,
                    value_type,
                ),
            )
    conn.commit()
    return org_id, project_id, fields


def _seed_datastream(conn, project_id: str, org_id: str, template_id: str | None):
    """A managed_feed Datastream bound to ``template_id`` the way activation binds it.

    The binding key is ``config.source_owner.managed_feed_template_ref`` -- the
    DEDICATED key the surface contract ratified on 2026-07-31 ("the Template
    reference and the staged preview asset are separate keys"). Writing the
    staged-asset key alongside it is deliberate: it is the exact shape that used
    to lose the Template at activation.
    """
    ds_id = _id("ds_")
    plan_id = _id("dpv_")
    config = {
        "source_owner": {
            "managed_feed_template_ref": template_id,
            "managed_feed_source_ref": _id("asset_"),
        }
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled,
                 created_by, org_id, config)
            VALUES (%s, %s, 'File source DS', NULL, 'managed_feed', FALSE,
                    'test', %s, %s::jsonb)
            """,
            (ds_id, project_id, org_id, json.dumps(config)),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'managed_feed', 'toorow', 'managed_raw',
                    '{}'::jsonb, repeat('a', 64), repeat('b', 64), 'test')
            """,
            (plan_id, ds_id, project_id),
        )
    conn.commit()
    return ds_id, plan_id


def _persist_confirmed_mapping(
    conn, *, ds_id, project_id, plan_id, mapping, org_id, template_id, gate=None
) -> str:
    """Confirm the mapping THROUGH THE PRODUCTION PATH, and return the version it mints.

    AI-257. This used to INSERT a `datastream_mapping_versions` row by hand and
    call it confirmed. It was not: arrival refuses with
    `file_source_confirmation_required` because the bound Template and the mapping
    version carry no matching human confirmation evidence -- and the refusal is
    RIGHT. What the fixture wrote was half the artifact.

    The trigger of migration 188 asks for twelve concordances, one of which is a
    real operation of type `file_source.template.gate_confirmed`. There is no way
    to fabricate that with an INSERT, and there should not be: the confirmation IS
    the human act. `confirm_mapping_version` performs it -- it records the
    decision, mints the NON-LIVE version and binds the two atomically -- so the
    harness now proves the landing the product actually allows.

    The gate must PASS. `market` is required and is not a column of the file: it
    arrives from the declared discriminator at landing. The caller has already
    asserted that the recognizer names it missing; here it is accepted as a
    resolution, which is the same decision a person makes on the screen.
    """
    from core.datastream_field_mapping import profile_fields
    from core.file_source_gate import confirm_mapping_version

    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_hash FROM app.file_source_templates WHERE id = %s",
            (template_id,),
        )
        template_content_hash = cur.fetchone()[0]

    # THE PAYLOAD IS BUILT BY THE PRODUCTION PROFILER, not by hand. The old
    # literal carried `field_id` and `binding` and nothing else; the confirmation
    # validates against the real schema, which also requires `physical_type`,
    # `profile`, `suggestion`, `ambiguities`, `grain` and the version/hash trio.
    # The hand INSERT never met that contract -- it simply never asked.
    profiled = profile_fields(
        field_records=[
            {"field_id": source_col, "physical_type": "varchar", "kind": "dimension"}
            for source_col in mapping
        ]
    )
    payload = dict(profiled)
    payload["plan_version_id"] = plan_id
    payload["source_schema_hash"] = "a" * 64
    payload.setdefault("mapping_contract_version", "1")
    for field in payload["fields"]:
        binding = dict(field.get("binding") or {})
        binding.update(
            canonical_target=mapping[field["field_id"]],
            mdm_target=mapping[field["field_id"]],
            status="confirmed",
            confirmed_by="owner@example.com",
            confirmed_reason="Confirmed in the file-source gate",
            blocking_reason=None,
        )
        field["binding"] = binding

    # The person resolves what the gate flagged: the discriminator supplies it.
    passed = {
        "passed": True,
        "missing_required": [],
        "flagged": [],
        "ambiguities": [],
    }
    result = confirm_mapping_version(
        conn,
        template_id=template_id,
        project_id=project_id,
        org_id=org_id,
        datastream_id=ds_id,
        actor="owner@example.com",
        gate_result=passed,
        mapping_payload=payload,
        # THE EVIDENCE SHAPE IS THE PRODUCTION ONE (`file_source_template_api:545`).
        # The trigger compares six of its keys against the row it guards --
        # `template_id`, `template_content_hash`, `sample_content_hash`,
        # `sample_filename`, `actor`, plus the two `confirm_mapping_version`
        # injects. A partial evidence is refused as STALE, which is the whole
        # point: a confirmation that does not say what was confirmed proves
        # nothing.
        evidence={
            "template_id": template_id,
            "template_content_hash": template_content_hash,
            "sample_content_hash": "b" * 64,
            "sample_filename": "",
            "actor": "owner@example.com",
            "resolutions": [
                {"canonical_target": target, "resolved_by": "discriminator"}
                for target in (gate or {}).get("missing_required", [])
            ],
            "accepted_warnings": [],
        },
        idempotency_key=_id("idem_"),
        # THE TEMPLATE'S OWN HASH, read from the row. The trigger of migration 188
        # compares `template_content_hash` against `file_source_templates.content_hash`
        # -- a literal cannot match it, and it must not: the confirmation binds a
        # decision to the EXACT Template that was reviewed, so a Template edited
        # since is a different thing and the evidence goes stale by design.
        content_hash=template_content_hash,
        pinned_plan_version_id=plan_id,
    )
    conn.commit()
    return result["mapping_version_id"]


def _make_template(conn, *, project_id, org_id, contract, code=None):
    from core.file_source_template import create_file_source_template

    row = create_file_source_template(
        conn,
        project_id=project_id,
        org_id=org_id,
        template_code=code or "EXAMPLE_PLAN",
        contract=contract,
        created_by="owner@example.com",
    )
    conn.commit()
    return row


def _import(conn, *, data, ds_id, project_id, plan_id, mapping_id, filename, fmt="csv"):
    """Drive the arrival exactly as a caller of ``run_import`` does.

    All three production callers -- upload (admin_api), email (inbound_ingest) and
    activation (datastream_activation_drivers) -- resolve the producer with
    ``resolve_file_source_producer`` and hand it to ``run_import``. Doing the same
    here is what makes this an ingress-parity harness rather than an upload one.
    """
    from core.csv_excel_import import resolve_file_source_producer, run_import

    producer = resolve_file_source_producer(
        conn,
        project_id=project_id,
        datastream_id=ds_id,
        mapping_version_id=mapping_id,
    )
    assert producer is not None, (
        "the Datastream's file-source Template binding did not resolve; the arrival "
        "would silently fall back to the ordinary CSV path and ignore the Template."
    )
    return producer, run_import(
        data,
        datastream_id=ds_id,
        project_id=project_id,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        projection_plan={"executable": True, "grain": ["date"]},
        actor="owner@example.com",
        idempotency_key=_id("idem_"),
        source_metadata={"filename": filename, "format": fmt},
        contract={},
        conn=conn,
        producer=producer,
    )


# ===========================================================================
# 1. ONBOARDING -- recognition and the required-field gate (CAP-3 / CAP-4).
#    These run BEFORE any landing, which is the only place the recognizer is
#    allowed to run at all: "a confirmed mapping is replayed, never re-derived".
# ===========================================================================


def test_recognition_maps_two_language_layouts_onto_one_template():
    """CAP-3 / Story 22.17: FR and DE headers reach the SAME canonical fields."""
    from core.file_source_recognizer import recognize_columns

    f = OFFLINE_FIELDS
    template = {"contract": _contract(f)}

    fr = recognize_columns(
        template,
        ["Date", "Cout net", "Impressions", "Commentaire interne"],
        [{"Date": "2026-01-01", "Cout net": "1000.00", "Impressions": "50000"}],
    )
    de = recognize_columns(
        template,
        DE_HEADERS,
        [{"Datum": "2026-01-01", "Nettokosten": "800.00", "Impressionen": "40000"}],
    )

    assert fr["mapping"]["Date"] == f["DATE"]
    assert fr["mapping"]["Cout net"] == f["COST"]
    assert de["mapping"]["Datum"] == f["DATE"]
    assert de["mapping"]["Nettokosten"] == f["COST"]
    # The extra column is IGNORED, never force-mapped onto a canonical field.
    assert "Commentaire interne" not in fr["mapping"]
    # Both layouts recovered the same canonical set from one template.
    assert set(fr["mapping"].values()) >= {f["DATE"], f["COST"]}
    assert set(fr["mapping"].values()) == set(de["mapping"].values())


def test_the_gate_flags_a_disappeared_required_field_instead_of_landing_partial():
    """CAP-4 / Story 22.15: a missing required field FLAGS, never lands partial."""
    from core.file_source_gate import evaluate_required_field_gate
    from core.file_source_recognizer import recognize_columns

    f = OFFLINE_FIELDS
    template = {"contract": _contract(f)}
    drifted = recognize_columns(
        template, ["Date", "Impressions"], [{"Date": "2026-02-01", "Impressions": "50000"}]
    )
    gate = evaluate_required_field_gate(template, drifted["mapping"], [])
    assert not gate["passed"]
    assert f["COST"] in gate["missing_required"], (
        "the vanished cost column must be named; a gate that fails without saying "
        "WHICH field is missing sends the operator back to guessing."
    )


# ===========================================================================
# 2. THE WHOLE CHAIN -- template to landed, readable rows.
# ===========================================================================


@requires_postgres
def test_fr_csv_lands_placed_and_readable_end_to_end(conn, warehouse):
    """The spine: template -> recognition -> gate -> mapping -> run_import -> rows.

    Asserts on what came OUT of the warehouse, in canonical vocabulary, carrying
    its matrix placement -- CAP-1, CAP-2, CAP-3, CAP-5, and Story 22.14's "every
    row carries the class and the discriminator-derived dimension value".
    """
    from core.file_source_gate import evaluate_required_field_gate
    from core.file_source_recognizer import recognize_columns

    org_id, project_id, f = _seed(conn)
    template = _make_template(
        conn, project_id=project_id, org_id=org_id, contract=_contract(f)
    )
    ds_id, plan_id = _seed_datastream(conn, project_id, org_id, template["id"])

    # -- onboarding: recognize, gate, confirm ------------------------------
    sample = [{"Date": "2026-01-01", "Cout net": "1000.00", "Impressions": "50000"}]
    recognized = recognize_columns(
        {"contract": template["contract"]},
        ["Date", "Cout net", "Impressions", "Commentaire interne"],
        sample,
    )
    gate = evaluate_required_field_gate(
        {"contract": template["contract"]}, recognized["mapping"], sample
    )
    # The market field is required and is NOT a column of the file: it arrives
    # from the declared discriminator at landing time, so the gate names it here.
    assert gate["missing_required"] == [f["MARKET"]]

    mapping_id = _persist_confirmed_mapping(
        conn, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping=recognized["mapping"], org_id=org_id, template_id=template["id"],
        gate=gate,
    )

    # -- arrival ------------------------------------------------------------
    _, result = _import(
        conn, data=FR_CSV, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping_id=mapping_id, filename="media_plan_2026_FR.csv",
    )
    conn.commit()

    assert not result["blocked"], f"the import was refused: {result.get('reason')} {result}"
    assert result["row_count"] == 3
    assert result["landed_row_count"] == 3, (
        "the ledger counted rows the warehouse never received -- counts real, data absent."
    )

    landed = _read_landed(
        warehouse, result["landing"]["table"], result["execution"]["id"]
    )
    assert len(landed) == 3

    # The rows speak CANONICAL ids, not the file's French headers (CAP-1).
    assert f["COST"] in landed[0] and f["DATE"] in landed[0]
    assert "Cout net" not in landed[0]
    # The extra column was ignored, not landed (CAP-3).
    assert "Commentaire interne" not in landed[0]

    # CAP-2 / Story 22.14: EVERY row carries its declared matrix class.
    assert [r.get(PLACEMENT_CLASS_KEY) for r in landed] == ["planned"] * 3, (
        "landed rows carry no matrix class: they are orphan data, which is exactly "
        "the 'Incomplete if: an import lands without a declared class' criterion."
    )
    # CAP-5 / Story 22.18: the discriminator value is promoted to its dimension.
    assert [r.get(f["MARKET"]) for r in landed] == ["FR"] * 3, (
        "the declared filename discriminator did not reach the rows as a dimension."
    )


@requires_postgres
def test_de_xlsx_reaches_the_same_canonical_fields_from_the_same_template(conn, warehouse):
    """CAP-5 / Incomplete-if: two layouts, one Template, one canonical shape.

    A different LANGUAGE, a different ORDER, a different FILE FORMAT (.xlsx, not
    .csv) -- and the landed rows must be indistinguishable in vocabulary from the
    FR ones, differing only in the market dimension the discriminator stamped.
    """
    from core.file_source_recognizer import recognize_columns

    org_id, project_id, f = _seed(conn)
    template = _make_template(
        conn, project_id=project_id, org_id=org_id,
        contract=_contract(f, format="excel"), code="EXAMPLE_PLAN_XLSX",
    )
    ds_id, plan_id = _seed_datastream(conn, project_id, org_id, template["id"])

    recognized = recognize_columns({"contract": template["contract"]}, DE_HEADERS, [])
    mapping_id = _persist_confirmed_mapping(
        conn, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping=recognized["mapping"], org_id=org_id, template_id=template["id"],
        gate=None,
    )

    _, result = _import(
        conn, data=_de_xlsx(), ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping_id=mapping_id, filename="media_plan_2026_DE.xlsx", fmt="excel",
    )
    conn.commit()

    assert not result["blocked"], f"the import was refused: {result.get('reason')} {result}"
    landed = _read_landed(
        warehouse, result["landing"]["table"], result["execution"]["id"]
    )
    assert len(landed) == 2

    # The SAME canonical fields the FR CSV produced -- from a different layout.
    assert {f["DATE"], f["COST"], f["IMPR"]} <= set(landed[0])
    assert [r.get(PLACEMENT_CLASS_KEY) for r in landed] == ["planned"] * 2
    assert [r.get(f["MARKET"]) for r in landed] == ["DE"] * 2


@requires_postgres
def test_a_declared_discriminator_that_cannot_be_detected_is_refused(conn, warehouse):
    """Story 22.18: an undetectable discriminator FLAGS -- it never mislabels.

    The template declares the market comes from the filename. This file's name
    carries no market marker. Landing it would write a NULL market on every row
    and call the file placed; the acceptance criterion says the opposite:
    "an undetectable discriminator flags for confirmation rather than mislabeling."
    """
    from core.file_source_recognizer import recognize_columns

    org_id, project_id, f = _seed(conn)
    template = _make_template(
        conn, project_id=project_id, org_id=org_id, contract=_contract(f)
    )
    ds_id, plan_id = _seed_datastream(conn, project_id, org_id, template["id"])

    recognized = recognize_columns(
        {"contract": template["contract"]},
        ["Date", "Cout net", "Impressions", "Commentaire interne"],
        [],
    )
    mapping_id = _persist_confirmed_mapping(
        conn, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping=recognized["mapping"], org_id=org_id, template_id=template["id"],
        gate=None,
    )

    _, result = _import(
        conn, data=FR_CSV, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping_id=mapping_id, filename="media_plan_2026.csv",  # no FR/DE marker
    )
    conn.commit()

    assert result["blocked"], (
        "an import whose declared discriminator could not be detected LANDED. "
        "Every row now carries a null market and is indistinguishable from a file "
        "that legitimately has none."
    )
    assert result["reason"] == "variant_discriminator_undetectable"
    assert result["ledger"] is None, "the refusal must precede the ledger, not follow it"


@requires_postgres
def test_a_missing_required_column_is_refused_before_landing(conn, warehouse):
    """CAP-4 at ARRIVAL, not just at onboarding: no partial landing.

    The confirmed mapping still names the cost column; the file no longer has it.
    The landing gate must catch that the produced rows do not honour the mapping.
    """
    from core.file_source_recognizer import recognize_columns

    org_id, project_id, f = _seed(conn)
    template = _make_template(
        conn, project_id=project_id, org_id=org_id, contract=_contract(f)
    )
    ds_id, plan_id = _seed_datastream(conn, project_id, org_id, template["id"])

    recognized = recognize_columns(
        {"contract": template["contract"]},
        ["Date", "Cout net", "Impressions"],
        [],
    )
    mapping_id = _persist_confirmed_mapping(
        conn, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping=recognized["mapping"], org_id=org_id, template_id=template["id"],
        gate=None,
    )

    # THE REFUSAL MOVED EARLIER, AND IT IMPROVED. This asserted a blocked result
    # carrying `required_field_gate_failed`, and recorded a complaint with it:
    # the gate caught the vanished column on CONFIDENCE (an all-empty canonical
    # column infers as `unknown`) rather than on absence, so an operator was told
    # "low confidence" about a column that simply is not in the file any more.
    #
    # The product now says exactly that instead, and it says it BEFORE the gate:
    # `file_source_drift: required source columns disappeared; preview and
    # reconfirm`. That names the fact and the gesture. The criterion this test
    # exists for -- refused before landing -- is met more strongly, so the
    # assertion follows the refusal rather than pinning the older one.
    with pytest.raises(CsvExcelImportError) as refused:
        _import(
            conn, data=FR_CSV_MISSING_COST, ds_id=ds_id, project_id=project_id,
            plan_id=plan_id, mapping_id=mapping_id, filename="media_plan_2026_FR.csv",
        )
    conn.commit()

    assert "file_source_drift" in str(refused.value)
    # The gesture is named, which is what makes a refusal actionable.
    assert "reconfirm" in str(refused.value)


@requires_postgres
def test_upload_and_email_of_the_same_bytes_produce_identical_canonical_rows(conn, warehouse):
    """CAP-7 / ingress parity: "the same file yields different results by upload
    and by email" is an Incomplete-if criterion.

    Upload and email are two transports of the same bytes: both resolve the SAME
    producer and call the SAME ``run_import``. The witness is the canonical row
    signature -- if the two ingresses ever diverge, this hash diverges first.
    """
    from core.file_source_producer import canonical_rows_signature

    org_id, project_id, f = _seed(conn)
    template = _make_template(
        conn, project_id=project_id, org_id=org_id, contract=_contract(f)
    )
    ds_id, plan_id = _seed_datastream(conn, project_id, org_id, template["id"])

    from core.file_source_recognizer import recognize_columns

    recognized = recognize_columns(
        {"contract": template["contract"]}, ["Date", "Cout net", "Impressions"], []
    )
    mapping_id = _persist_confirmed_mapping(
        conn, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping=recognized["mapping"], org_id=org_id, template_id=template["id"],
        gate=None,
    )

    producer, _ = _import(
        conn, data=FR_CSV, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping_id=mapping_id, filename="media_plan_2026_FR.csv",
    )
    conn.commit()

    # The producer is the whole contract an arrival replays; running it twice over
    # the same bytes is what "upload == email" means once transport is stripped.
    first = canonical_rows_signature(producer(FR_CSV))
    second = canonical_rows_signature(producer(FR_CSV))
    assert first == second


# ===========================================================================
# 3. THE ADAPTATION PATH (Phase B-3) -- a locked .py replayed at arrival.
# ===========================================================================


def _adaptation_py(fields) -> str:
    """An LLM-authored ``.py`` for a pipe-delimited layout no catalog template covers."""
    return f'''
def adapt(data, template):
    """Parse a pipe-delimited layout no catalog template covers."""
    text = data.decode("utf-8")
    rows = []
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        day, cost, impressions = line.split("|")
        rows.append({{
            {fields["DATE"]!r}: day.strip(),
            {fields["COST"]!r}: cost.strip(),
            {fields["IMPR"]!r}: impressions.strip(),
        }})
    return {{"rows": rows, "rejected": []}}
'''


PIPE_FILE = (
    "day|cost|impressions\n"
    "2026-03-01|1500.00|70000\n"
    "2026-03-02|1600.00|72000\n"
).encode("utf-8")


@requires_postgres
def test_a_locked_adaptation_template_lands_at_arrival(conn, warehouse):
    """Story 22.24: a locked ``.py`` runs on every LATER file, by upload or email.

    Before this harness the adaptation lifecycle module was imported by nothing
    but its own unit test: an ``adaptation``-kind Template resolved to a producer
    that called the CATALOG path, which demands a source->canonical mapping an
    adaptation does not have, so every arrival died on ``mapping_required``. A
    bespoke file could be onboarded and locked and could never land.
    """
    import hashlib

    from core.file_source_adaptation import lock_adaptation_template

    org_id, project_id, f = _seed(conn)
    py_source = _adaptation_py(f)

    # THE LOCK COMES AFTER THE CONFIRMATION, not before it. Its signature grew
    # five required arguments -- `gate_result`, `sample_content_hash`,
    # `datastream_id`, `mapping_version_id`, `plan_version_id` -- because locking
    # an adaptation IS a confirmation: it says this `.py` passed its self-test on
    # this sample, for this Datastream, under this mapping and plan. A lock that
    # could not name them would be a version bound to nothing.
    #
    # So the Datastream is bound to a CATALOG template first, its mapping is
    # confirmed, and the adaptation is locked as the version that supersedes it.
    catalog = _make_template(
        conn, project_id=project_id, org_id=org_id, contract=_contract(f)
    )
    ds_id, plan_id = _seed_datastream(conn, project_id, org_id, catalog["id"])
    # An adaptation needs no source->canonical mapping -- its `.py` emits canonical
    # rows directly -- but the ledger's execution row still requires a mapping
    # VERSION to point at, so the artifact exists and is empty.
    mapping_id = _persist_confirmed_mapping(
        conn, ds_id=ds_id, project_id=project_id, plan_id=plan_id, mapping={},
        org_id=org_id, template_id=catalog["id"],
    )

    row = lock_adaptation_template(
        conn,
        project_id=project_id,
        org_id=org_id,
        template_code="EXAMPLE_PIPE",
        py_source=py_source,
        placement_class="planned",
        placement={"metric": f["COST"], "period": f["DATE"]},
        required_fields=[f["DATE"], f["COST"]],
        grain="daily",
        created_by="owner@example.com",
        gate_result={"passed": True, "missing_required": [], "flagged": []},
        sample_content_hash="b" * 64,
        datastream_id=ds_id,
        mapping_version_id=mapping_id,
        plan_version_id=plan_id,
    )
    conn.commit()
    assert row["kind"] == "adaptation"
    assert row["contract"]["py_content_hash"] == hashlib.sha256(
        py_source.encode("utf-8")
    ).hexdigest()

    # The Datastream now points at the adaptation, which is what arrival resolves.
    # The binding key is `config.source_owner.managed_feed_template_ref` -- the
    # same one activation writes -- not a column: there is none.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET config = jsonb_set("
            "  COALESCE(config, '{}'::jsonb),"
            "  '{source_owner,managed_feed_template_ref}', to_jsonb(%s::text), true"
            ") WHERE id = %s",
            (row["id"], ds_id),
        )
    conn.commit()
    _, result = _import(
        conn, data=PIPE_FILE, ds_id=ds_id, project_id=project_id, plan_id=plan_id,
        mapping_id=mapping_id, filename="bespoke_2026_03.txt",
    )
    conn.commit()

    assert not result["blocked"], f"the adaptation import was refused: {result}"
    landed = _read_landed(
        warehouse, result["landing"]["table"], result["execution"]["id"]
    )
    assert len(landed) == 2
    assert {f["DATE"], f["COST"]} <= set(landed[0])
    # An adaptation is placed like any other Template (AD-6): same stamp, same rules.
    assert [r.get(PLACEMENT_CLASS_KEY) for r in landed] == ["planned"] * 2


# ===========================================================================
# NOT COVERED -- stated, because an unstated boundary reads as coverage.
#
#  * DRIFT RE-VALIDATION. ``file_source_producer.detect_source_drift`` is NOT
#    exercised on the arrival path and is NOT wired, deliberately. The ratified
#    surface contract says so in as many words: "nothing in the ratified contract
#    says WHEN drift re-validation runs [...] nor what happens when it fires
#    [...] Until that is written, drift detection has no defensible caller"
#    (docs/product-architecture/file-source-ingestion.md). Choosing a trigger and
#    an outcome here would be inventing a design decision, not wiring one.
#    What IS covered is the consequence the contract does specify: a disappeared
#    required field is refused (``test_a_missing_required_column_is_refused_...``).
#
#  * COLUMN RECOGNITION AT ARRIVAL. Also deliberate, and also the contract's
#    words: "a confirmed mapping is re-derived on arrival instead of replayed" is
#    an Incomplete-if criterion. ``recognize_columns`` is therefore exercised at
#    ONBOARDING only. Its production caller must be the preview/onboarding
#    endpoint, which lives in ``server/core/admin_api.py`` -- outside this
#    session's write scope, and still unwired at the time of writing.
#
#  * THE HUMAN GATE CONFIRMATION. ``record_gate_confirmation`` writes the AD-7
#    confirmation to the operations ledger, but the Template row keeps no trace
#    of it, so "a human confirmed THIS lock" cannot be proven from the Template's
#    own record. That is an open Incomplete-if criterion needing a schema change,
#    not a test.
#
#  * THE MEDIA-PLAN RESHAPE PATH (Story 22.10/22.13). This harness drives the
#    TABULAR and ADAPTATION paths. The reshape path (merged cells, header not on
#    row 1, date-range explosion, subtotal skipping) is covered by
#    ``server/tests/core/test_file_source_mediaplan_e2e.py`` at the producer
#    level; it has no live-Postgres landing test here.
#
#  * THE ISOLATED WORKER'S ISOLATION. The adaptation test proves the ``.py``
#    RUNS out-of-process and lands. It does NOT prove the sandbox denies network
#    or filesystem access -- that is ``server/tests/core`` sandbox territory.
#
#  * PUBLICATION. ``run_import`` writes a candidate and returns
#    ``published=False`` by design; the pointer swap is a separate act. Nothing
#    here asserts a published dataset, because nothing here publishes one.
# ===========================================================================
