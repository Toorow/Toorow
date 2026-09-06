"""The selector a person uses to connect THEIR table, pinned offline.

Someone points this connector at whatever table they already have, so the whole
contract is the picking: which tables are offered, which column may serve which
role, and whether a nested one survives the trip into SQL and back out of a row.

The shapes here are the ones a GCP billing export really has -- every dimension
worth breaking cost down by (`service.description`, `sku.description`) is nested
inside a STRUCT, and the date column is a TIMESTAMP. A selector that only walked
top-level columns would offer cost-per-day and nothing to split it by, and would
look entirely healthy while doing it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_MODULES = Path(__file__).resolve().parents[3] / "modules"
if str(_MODULES) not in sys.path:
    sys.path.insert(0, str(_MODULES))

from bigquery.connector import (  # noqa: E402
    HISTORY_DEDUP_KEYS,
    _flatten_schema,
    build_select,
    quote_column_path,
    resolve_column,
    resolve_history_dedup,
    transform,
)


class _Field:
    """The parts of a google.cloud.bigquery.SchemaField this code reads."""

    def __init__(self, name, field_type, mode="NULLABLE", fields=()):
        self.name = name
        self.field_type = field_type
        self.mode = mode
        self.fields = fields
        self.is_nullable = mode == "NULLABLE"


BILLING_SCHEMA = [
    _Field("billing_account_id", "STRING"),
    _Field("usage_start_time", "TIMESTAMP"),
    _Field("service", "RECORD", fields=[_Field("id", "STRING"), _Field("description", "STRING")]),
    _Field("cost", "FLOAT"),
    # An array: a scalar cannot be read out of it without an UNNEST this
    # connector does not emit, so nothing under it may be offered.
    _Field("credits", "RECORD", mode="REPEATED", fields=[_Field("amount", "FLOAT")]),
]


def _candidates(schema, role):
    return [f["name"] for f in _flatten_schema(schema) if role in f["roles"]]


def test_nested_dimensions_are_offered():
    """Every useful billing breakdown is nested; not walking in offers none of them."""
    assert "service.description" in _candidates(BILLING_SCHEMA, "breakdown")


def test_a_struct_is_a_folder_not_a_choice():
    """`service` groups the choices; it is not one -- selecting it yields a dict."""
    struct = next(f for f in _flatten_schema(BILLING_SCHEMA) if f["name"] == "service")
    assert struct["roles"] == []


def test_a_repeated_field_and_its_children_are_never_offered():
    names = {f["name"] for f in _flatten_schema(BILLING_SCHEMA) for role in f["roles"]}
    assert not any(name.startswith("credits") for name in names)


def test_roles_are_narrowed_by_type():
    """The date list must not offer a STRING id, nor the value list a label."""
    assert _candidates(BILLING_SCHEMA, "date") == ["usage_start_time"]
    assert _candidates(BILLING_SCHEMA, "value") == ["cost"]
    assert "billing_account_id" not in _candidates(BILLING_SCHEMA, "value")


def test_a_nested_path_is_quoted_segment_by_segment():
    """Backticking the whole path names ONE column that contains a dot."""
    assert quote_column_path("service.description") == "`service`.`description`"
    assert quote_column_path("cost") == "`cost`"


@pytest.mark.parametrize(
    "path",
    ["service.desc;DROP", "service`.`x", "a b", "", "a.b.c.d"],
)
def test_nesting_widens_what_is_selectable_not_what_is_injectable(path):
    with pytest.raises(ValueError):
        quote_column_path(path)


def test_the_window_predicate_uses_the_quoted_path():
    sql = build_select("p.d.t", "usage_start_time", "2026-07-01", "2026-07-31")
    assert "`usage_start_time` >= @date_from" in sql
    assert "`usage_start_time` < @date_to" in sql


def test_a_nested_value_is_read_back_out_of_the_returned_row():
    """The row arrives with the STRUCT as a dict -- `row.get()` would miss it."""
    row = {"service": {"description": "BigQuery"}}
    assert resolve_column(row, "service.description") == "BigQuery"
    assert resolve_column(row, "service.missing") is None
    assert resolve_column(row, "cost") is None


def test_transform_folds_a_billing_row_on_its_nested_breakdown():
    rows = [
        {
            "usage_start_time": "2026-07-29 14:03:00+00:00",
            "cost": 1.25,
            "service": {"description": "BigQuery"},
        }
    ]
    out = transform(
        rows,
        date_column="usage_start_time",
        value_column="cost",
        metric_name="cloud_cost",
        breakdown_column="service.description",
    )
    assert out == [
        {
            "date": "2026-07-29",  # the hour is dropped: the grain is a DAY
            "metric": "cloud_cost",
            "value": 1.25,
            "breakdown_dimension": "service.description",
            "breakdown_value": "BigQuery",
        }
    ]


# ---------------------------------------------------------------------------
# A VERSIONED HISTORY TABLE -- the shape a Fivetran-style landing writes
# ---------------------------------------------------------------------------
#
# One row per entity PER SYNC BATCH, not one row per entity. Read flat, every
# version of every entity survives into the mart; deduplicated on "the latest
# version" alone, every label a batch happened to blank is lost. The fixture
# carries both traps on purpose, and no real identifier: three generic entities,
# four batches each.

HISTORY_SCHEMA = [
    _Field("entity_id", "STRING"),
    _Field("synced_at", "TIMESTAMP"),
    _Field("entity_label", "STRING"),
    _Field("amount", "FLOAT"),
    _Field("day", "DATE"),
    # A batch tag that reads like a version and cannot order one: "v10" sorts
    # before "v9".
    _Field("version_tag", "STRING"),
]

HISTORY_TABLE = "proj_EXAMPLE.dataset_EXAMPLE.entity_history"

# (entity_id, batch, label, amount) -- the day is the batch day, so the whole
# fixture sits inside the window under test.
HISTORY_ROWS = [
    ("entity-1", 1, "Alpha", 10.0),
    ("entity-1", 2, None, 11.0),
    ("entity-1", 3, None, 12.0),
    # The last batch blanked a label that the FIRST batch carried.
    ("entity-1", 4, None, 13.0),
    ("entity-2", 1, None, 20.0),
    ("entity-2", 2, "Beta", 21.0),
    ("entity-2", 3, "Beta renamed", 22.0),
    ("entity-2", 4, None, 23.0),
    ("entity-3", 1, "Gamma", 30.0),
    ("entity-3", 2, "Gamma", 31.0),
    ("entity-3", 3, "Gamma", 32.0),
    ("entity-3", 4, "Gamma", 33.0),
]

HISTORY_ENTITIES = sorted({row[0] for row in HISTORY_ROWS})

HISTORY_DECLARATION = {
    "entity_key_columns": ["entity_id"],
    "version_column": "synced_at",
    "label_columns": ["entity_label"],
}


def _resolved_history():
    return resolve_history_dedup(HISTORY_DECLARATION, _flatten_schema(HISTORY_SCHEMA))


def _local_dialect(sql: str) -> str:
    """The statement BigQuery will run, handed to the engine the tests have.

    TWO NOTATIONS ARE TRANSLATED AND NOTHING ELSE: BigQuery quotes an identifier
    with a backtick where DuckDB quotes it with a double quote, and BigQuery names
    a query parameter `@name` where DuckDB names it `$name`. The projection, the
    two windows, the QUALIFY and the window predicate are executed exactly as this
    module emitted them -- which is the entire reason for executing at all. Both
    engines implement `QUALIFY` and `LAST_VALUE(... IGNORE NULLS)`; neither is
    emulated here.
    """
    return (
        sql.replace("`", '"')
        .replace("@date_from", "$date_from")
        .replace("@date_to", "$date_to")
    )


def _history_connection():
    """A local table holding the fixture, named exactly as the statement names it."""
    import duckdb

    con = duckdb.connect()
    con.execute(
        f'CREATE TABLE "{HISTORY_TABLE}" ('
        "entity_id VARCHAR, synced_at TIMESTAMP, entity_label VARCHAR, "
        "amount DOUBLE, day DATE, version_tag VARCHAR)"
    )
    con.executemany(
        f'INSERT INTO "{HISTORY_TABLE}" VALUES (?, ?, ?, ?, ?, ?)',
        [
            (
                entity_id,
                f"2026-07-0{batch} 00:00:00",
                label,
                amount,
                f"2026-07-0{batch}",
                f"v{batch}",
            )
            for entity_id, batch, label, amount in HISTORY_ROWS
        ],
    )
    # The reference side of the join: ONE row per entity, which is what a fan-out
    # multiplies. Kept as a separate relation so the count that matters is the
    # join's, not the fixture's.
    con.execute("CREATE TABLE entity_reference (entity_id VARCHAR)")
    con.executemany(
        "INSERT INTO entity_reference VALUES (?)", [(name,) for name in HISTORY_ENTITIES]
    )
    return con


def _run_history_select(con, history_dedup):
    sql = build_select(
        HISTORY_TABLE, "day", "2026-07-01", "2026-07-31", history_dedup=history_dedup
    )
    return con.execute(
        _local_dialect(sql), {"date_from": "2026-07-01", "date_to": "2026-07-31"}
    ).fetchall()


def test_a_flat_table_reads_exactly_as_it_did():
    """No declaration, no change -- the statement is the one that shipped.

    Pinned character for character rather than probed for the absence of a
    keyword: a QUALIFY that only appears in some flat cases would pass a
    `"QUALIFY" not in sql` assertion on the case that was looked at.
    """
    assert build_select("p.d.t", "usage_start_time", "2026-07-01", "2026-07-31") == (
        "SELECT * FROM `p.d.t` "
        "WHERE `usage_start_time` >= @date_from AND `usage_start_time` < @date_to"
    )
    assert build_select(
        "p.d.t", "usage_start_time", "2026-07-01", "2026-07-31", limit=100
    ) == (
        "SELECT * FROM `p.d.t` "
        "WHERE `usage_start_time` >= @date_from AND `usage_start_time` < @date_to "
        "LIMIT 100"
    )
    # A declaration that is absent, empty or None is the same table: flat.
    for absent in (None, {}, False):
        assert build_select(
            "p.d.t", "usage_start_time", "2026-07-01", "2026-07-31", history_dedup=absent
        ) == build_select("p.d.t", "usage_start_time", "2026-07-01", "2026-07-31")


def test_the_row_count_survives_the_join_to_the_unit():
    """N entities, M batches: the join gives back N rows, not N x M.

    The same join against the table as it lands is asserted too -- otherwise a
    deduplication that returned nothing at all would satisfy "no fan-out".
    """
    con = _history_connection()
    try:
        deduplicated = _run_history_select(con, _resolved_history())
        assert len(deduplicated) == len(HISTORY_ENTITIES)

        con.execute(
            "CREATE VIEW deduplicated AS "
            + _local_dialect(
                build_select(
                    HISTORY_TABLE,
                    "day",
                    "2026-07-01",
                    "2026-07-31",
                    history_dedup=_resolved_history(),
                )
            ).replace("$date_from", "DATE '2026-07-01'").replace(
                "$date_to", "DATE '2026-07-31'"
            )
        )
        joined = con.execute(
            "SELECT COUNT(*) FROM entity_reference r "
            "JOIN deduplicated d ON d.entity_id = r.entity_id"
        ).fetchone()[0]
        assert joined == len(HISTORY_ENTITIES)

        # What the same join costs without the deduplication: the fan-out this
        # exists to remove, measured rather than asserted from memory.
        fanned_out = con.execute(
            "SELECT COUNT(*) FROM entity_reference r "
            f'JOIN "{HISTORY_TABLE}" h ON h.entity_id = r.entity_id'
        ).fetchone()[0]
        assert fanned_out == len(HISTORY_ROWS)
    finally:
        con.close()


def test_no_non_null_label_is_lost_to_a_batch_that_blanked_it():
    """The body comes from the latest batch; each label keeps its last non-NULL.

    One window cannot do both, and taking only "the latest version" is exactly
    the shortcut that dropped labels: `entity-1` and `entity-2` were both blanked
    by their last batch, and both carry a label an earlier batch wrote.
    """
    con = _history_connection()
    try:
        rows = _run_history_select(con, _resolved_history())
        by_entity = {row[0]: row for row in rows}
        assert set(by_entity) == set(HISTORY_ENTITIES)

        # The label: the most recent NON-NULL one, never the most recent one.
        assert [by_entity[name][2] for name in HISTORY_ENTITIES] == [
            "Alpha",
            "Beta renamed",
            "Gamma",
        ]
        # The body: the most recent version's, unchanged by the label window.
        assert [by_entity[name][3] for name in HISTORY_ENTITIES] == [13.0, 23.0, 33.0]

        # And the measure that named the defect: nothing that was present is gone.
        present = {row[0] for row in HISTORY_ROWS if row[2] is not None}
        assert {name for name, row in by_entity.items() if row[2] is not None} == present
    finally:
        con.close()


def test_a_declared_column_that_is_not_a_column_is_named_before_anything_runs():
    """A typo is a refusal naming the column, never an empty window."""
    fields = _flatten_schema(HISTORY_SCHEMA)
    for declaration, missing in (
        ({**HISTORY_DECLARATION, "entity_key_columns": ["entity_uuid"]}, "entity_uuid"),
        ({**HISTORY_DECLARATION, "version_column": "loaded_at"}, "loaded_at"),
        ({**HISTORY_DECLARATION, "label_columns": ["display_name"]}, "display_name"),
    ):
        with pytest.raises(ValueError) as raised:
            resolve_history_dedup(declaration, fields)
        assert missing in str(raised.value)
        assert "not a column of this table" in str(raised.value)


def test_a_version_that_cannot_be_ordered_is_refused_with_the_gesture():
    """A STRING batch tag sorts "v10" before "v9" -- an ordering nobody declared."""
    with pytest.raises(ValueError) as raised:
        resolve_history_dedup(
            {**HISTORY_DECLARATION, "version_column": "version_tag"},
            _flatten_schema(HISTORY_SCHEMA),
        )
    message = str(raised.value)
    assert "version_tag" in message
    # The message names what to do, not what the type is called in the warehouse.
    assert "pick a timestamp or numeric column" in message


def test_the_two_history_roles_are_offered_and_narrowed_by_type():
    """What `describe_table` offers is exactly what the declaration accepts."""
    assert _candidates(HISTORY_SCHEMA, "version") == ["synced_at", "amount", "day"]
    assert "version_tag" not in _candidates(HISTORY_SCHEMA, "version")
    assert "entity_id" in _candidates(HISTORY_SCHEMA, "entity_key")
    # A measurement identifies nothing, so it is never offered as a key.
    assert "amount" not in _candidates(HISTORY_SCHEMA, "entity_key")
    # A folder and an array stay out of both, exactly as they stay out of the
    # three roles that were already offered.
    assert _candidates(BILLING_SCHEMA, "entity_key") == [
        "billing_account_id",
        "usage_start_time",
        "service.id",
        "service.description",
    ]
    assert _candidates(BILLING_SCHEMA, "version") == ["usage_start_time", "cost"]
    assert not any(
        name.startswith("credits")
        for role in ("entity_key", "version")
        for name in _candidates(BILLING_SCHEMA, role)
    )
    assert "service" not in _candidates(BILLING_SCHEMA, "entity_key")


def test_the_declaration_reaches_pull_under_the_name_the_manifest_declares():
    """A declared parameter pull() does not have is a choice dropped in silence.

    Same failure `core.queue._account_kwargs` warns about for the selected
    account; here it is caught at rest instead of at the last metre.
    """
    import inspect
    import json

    from bigquery import connector

    manifest = json.loads(
        (Path(connector.__file__).parent / "manifest.json").read_text(encoding="utf-8")
    )
    declared = manifest["history_deduplication"]
    assert declared["pull_parameter"] in inspect.signature(connector.pull).parameters
    # And the keys the manifest says the declaration carries are the ones read.
    assert set(declared["declaration"].values()) == set(HISTORY_DEDUP_KEYS)
    assert set(HISTORY_DECLARATION) == set(HISTORY_DEDUP_KEYS)


class _FakeTable:
    def __init__(self, table_id):
        self.table_id = table_id
        self.table_type = "TABLE"


class _FakeDataset:
    def __init__(self, dataset_id):
        self.dataset_id = dataset_id


class _FakeClient:
    """Enough of bigquery.Client for discovery: two projects, one with tables."""

    project = "toorow"

    def list_projects(self):
        return [type("P", (), {"project_id": "toorow"}), type("P", (), {"project_id": "other"})]

    def list_datasets(self, project=None):
        return [_FakeDataset("billing")] if project == "toorow" else []

    def list_tables(self, ref, max_results=None):
        return [_FakeTable("export_v1")]


def _flatten_ids(nodes, ids=None):
    """The exact walk core.account_topology does to decide what is selectable."""
    ids = set() if ids is None else ids
    for node in nodes or []:
        nid = node.get("id")
        if isinstance(nid, str) and nid:
            ids.add(nid)
        _flatten_ids(node.get("children"), ids)
    return ids


@pytest.fixture()
def _fake_bigquery(monkeypatch):
    from google.cloud import bigquery as real

    monkeypatch.setattr(real, "Client", lambda *a, **k: _FakeClient())
    # BigQuery EST une source Google, donc elle passe par le consentement Google
    # comme les autres : `_get_bq_client` minte un jeton avant de construire le
    # client. Sans ce double, une unite sortirait sur le reseau -- et c'est
    # exactement ce qu'elle faisait, en 401. Meme patron que `gsc`,
    # `google-business-profile` et `google-sheets`.
    monkeypatch.setattr(
        "core.nango_client.get_fresh_token",
        lambda connection_id, provider=None: "fake-token",
    )


def test_discovery_returns_the_tree_core_can_walk(_fake_bigquery):
    """Core does `accounts = discovery_fn(...)` and walks a LIST.

    Returning `{"topology": ..., "accounts": [...]}` reads fine when the function
    is called directly and flattens to ZERO selectable accounts through core --
    the picker comes back empty with nothing to explain why.
    """
    from bigquery.connector import discover_accounts

    tree = discover_accounts("conn_EXAMPLE")
    assert isinstance(tree, list)
    assert _flatten_ids(tree) == {"toorow.billing.export_v1"}


def test_only_a_table_is_selectable(_fake_bigquery):
    """A project and a dataset are navigated, not pulled from.

    Core stores every id it finds as a selectable account, so an id on a folder
    offers a choice that can only fail later at the table-reference guard.
    """
    from bigquery.connector import discover_accounts

    assert all(ident.count(".") == 2 for ident in _flatten_ids(discover_accounts("c")))


def test_the_tree_is_three_levels_and_names_each_one(_fake_bigquery):
    """Project -> dataset -> table, and each node says which it is.

    Step 1 of the wizard browses this tree live in the External BigQuery mode, so
    the levels are a contract and not an implementation detail: a screen that
    cannot tell a dataset from a table cannot draw the walk, and a project the
    principal cannot list must be absent rather than empty-and-unexplained.
    """
    from bigquery.connector import discover_accounts

    tree = discover_accounts("conn_EXAMPLE")
    # Sorted by project id, so the walk is the same list on every call -- a tree
    # that reorders between two openings is a picker nobody can point at.
    assert [node["label"] for node in tree] == ["other", "toorow"]
    assert {node["kind"] for node in tree} == {"project"}
    # A project the principal reaches but that holds no dataset is SHOWN empty
    # rather than hidden: hidden, the person looks for a permission problem that
    # does not exist.
    assert tree[0]["children"] == []
    datasets = tree[1]["children"]
    assert [node["kind"] for node in datasets] == ["dataset"]
    assert [node["kind"] for node in datasets[0]["children"]] == ["table"]


def test_a_dataset_past_the_bound_says_it_was_cut(monkeypatch):
    """A BOUNDED LIST THAT DOES NOT SAY SO IS A FABRICATED COMPLETENESS.

    Discovery stops at `MAX_DISCOVERY_TABLES_PER_DATASET` tables per dataset, and
    the only thing standing between that and a screen claiming "these are all your
    tables" is this flag. `_external_access_options` rebuilds it downstream from a
    count, but the source of truth is here -- and it had no test, on either side
    of the bound.

    The bound is lowered rather than a 501st table faked: what is under test is
    the comparison, not the number.
    """
    from bigquery import connector
    from google.cloud import bigquery as real

    monkeypatch.setattr(connector, "_MAX_DISCOVERY_TABLES", 2)

    class _WideClient(_FakeClient):
        def list_tables(self, ref, max_results=None):
            tables = [_FakeTable(f"export_v{index}") for index in range(5)]
            return tables[:max_results] if max_results else tables

    monkeypatch.setattr(real, "Client", lambda *a, **k: _WideClient())
    # Ce test construit son propre client et ne prend pas `_fake_bigquery` : il
    # doit donc doubler la porte du jeton lui-meme, pour la meme raison.
    monkeypatch.setattr(
        "core.nango_client.get_fresh_token",
        lambda connection_id, provider=None: "fake-token",
    )
    dataset = connector.discover_accounts("conn_EXAMPLE")[1]["children"][0]
    assert dataset["truncated"] is True
    # Cut to the bound, not merely flagged: offering more than was walked would
    # be a list nobody verified.
    assert len(dataset["children"]) == 2

    class _NarrowClient(_FakeClient):
        def list_tables(self, ref, max_results=None):
            return [_FakeTable("export_v1")]

    monkeypatch.setattr(real, "Client", lambda *a, **k: _NarrowClient())
    small = connector.discover_accounts("conn_EXAMPLE")[1]["children"][0]
    assert small["truncated"] is False
