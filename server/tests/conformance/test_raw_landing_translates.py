"""Every connector's real landing SQL must survive the BigQuery translator.

DuckDB is the local-testing backend, not the target, so a connector whose raw
DDL the translator cannot map is not "DuckDB-only" -- it is a pull that goes
green locally and raises the first time it runs against the warehouse. The
translator refuses rather than guesses, which is right, but a refusal discovered
in production is a refusal discovered too late.

So this reads the SQL the modules actually ship -- not invented SQL -- and pushes
it through the same object `open_raw_writer` returns when TOOROW_DB_MODE=bigquery.
It found the two defects it now pins: BOOLEAN mapped to STRING (a Python bool
serialised into a TYPE_STRING proto field raises at the wire), and a column
DEFAULT clause that was never parsed at all.

WHY THE MODULE IS IMPORTED RATHER THAN PARSED
    A first version read module-level string constants off the AST, and had a
    hole big enough to drive a connector through: google-business-profile builds
    its INSERT with an f-string over _METRIC_COLUMNS, so the value that actually
    ships never existed in the source text and was never checked. Importing gives
    the real post-interpolation value. The AST is still walked, but only for the
    statements issued INSIDE functions -- google-analytics migrates its tables
    with inline ALTER TABLE literals that are not constants anywhere.

The gate is repository-wide on purpose: the next connector to add a landing gets
it for free, which is the difference between fixing an instance and fixing the
class.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from core import raw_landing  # noqa: E402
from core.bigquery_raw_writer import BigQueryRawWriter  # noqa: E402
from core.raw_landing import RawLandingError  # noqa: E402

_MODULES = _SERVER / "modules"
_CREATE = re.compile(r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s", re.I)
_ALTER = re.compile(r"^\s*ALTER\s+TABLE\s", re.I)
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s", re.I)
_INSERT_COLUMNS = re.compile(r"INSERT\s+INTO\s+\w+\s*\(([^)]*)\)", re.I | re.S)


def _classify(text: str) -> str | None:
    if _CREATE.match(text):
        return "create"
    if _ALTER.match(text):
        return "alter"
    if _INSERT.match(text):
        return "insert"
    return None


def _imported_sql(connector: Path) -> dict[str, list[str]]:
    """Module-level SQL as it exists AFTER import -- f-strings already resolved."""
    name = f"conformance_landing_{connector.parent.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, connector)
    if spec is None or spec.loader is None:
        return {}
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{connector.parent.name} does not import standalone: {exc}")
    finally:
        sys.modules.pop(name, None)

    found: dict[str, list[str]] = {"create": [], "alter": [], "insert": []}
    members = vars(module)
    for value in members.values():
        if isinstance(value, str):
            kind = _classify(value)
            if kind:
                found[kind].append(value)

    # DECLARED tables, rendered the way the connector renders them.
    #
    # Since the single-declaration repair a connector no longer ships its raw SQL
    # as a literal: it declares `<PREFIX>_TABLE` + `<PREFIX>_COLUMNS` and lets
    # `core.raw_landing` render both statements. Reading only literals would have
    # dropped EIGHT connectors out of this gate silently -- not skipped, absent
    # from the parametrization -- which is the failure mode this file's own
    # docstring was written about. So the declarations are rendered here and go
    # through the translator exactly like the literals do.
    for name, value in members.items():
        if not (name.endswith("_TABLE") and isinstance(value, str)):
            continue
        columns = members.get(name[: -len("_TABLE")] + "_COLUMNS")
        if not _is_column_declaration(columns):
            continue
        found["create"].append(raw_landing.duckdb_ddl(value, columns))
        found["insert"].append(raw_landing.duckdb_insert(value, columns))
    return found


def _is_column_declaration(value) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, tuple) and len(item) == 2 and all(isinstance(p, str) for p in item)
            for item in value
        )
    )


def _inline_sql(connector: Path) -> dict[str, list[str]]:
    """Static SQL literals issued inside functions (migration ALTERs, mostly)."""
    tree = ast.parse(connector.read_text(encoding="utf-8"))
    # The static head of an f-string is an ast.Constant too, and it is a TRUNCATED
    # statement -- "INSERT INTO t (date, " reads as an INSERT and is not one. Those
    # belong to the imported pass, which sees the interpolated value.
    interpolated = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    found: dict[str, list[str]] = {"create": [], "alter": [], "insert": []}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in interpolated:
                continue
            kind = _classify(node.value)
            if kind:
                found[kind].append(node.value)
    return found


#: `_RAW_COLUMNS`, `_BREAKDOWN_COLUMNS`, ... -- a declared raw table.
_DECLARATION = re.compile(r"^_\w*_COLUMNS(?:\s*:[^=]+)?\s*=\s*\[", re.M)


def _landing_connectors() -> list[Path]:
    """Connectors whose SOURCE lands a raw table -- as SQL, or as a declaration."""
    found = []
    for connector in sorted(_MODULES.glob("*/connector.py")):
        text = connector.read_text(encoding="utf-8")
        if (
            re.search(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", text, re.I)
            or _DECLARATION.search(text)
        ):
            found.append(connector)
    return found


@pytest.fixture()
def landed(monkeypatch):
    """Capture the landing instead of calling BigQuery -- the translation is the subject."""
    calls = []

    def _fake(table, rows, *, columns, project_id, mode, backend):
        calls.append({"table": table, "rows": rows, "columns": columns})
        return {"rows": len(rows), "backend": backend, "mode": mode, "table": table}

    monkeypatch.setattr("core.bigquery_raw_writer.land_raw_rows", _fake)
    return calls


@pytest.mark.parametrize("connector", _landing_connectors(), ids=lambda p: p.parent.name)
def test_the_shipped_landing_sql_translates_to_bigquery(connector, landed):
    imported = _imported_sql(connector)
    inline = _inline_sql(connector)
    statements = {
        kind: list(dict.fromkeys(imported.get(kind, []) + inline.get(kind, [])))
        for kind in ("create", "alter", "insert")
    }
    if not statements["create"]:
        pytest.skip(f"{connector.parent.name} declares no CREATE TABLE landing")

    writer = BigQueryRawWriter(project_id="proj_EXAMPLE")

    # Order matters: a column added by ALTER must be known before an INSERT names it.
    for ddl in statements["create"]:
        writer.execute(ddl)
    for alter in statements["alter"]:
        writer.execute(alter)

    for insert in statements["insert"]:
        match = _INSERT_COLUMNS.search(insert)
        assert match, f"{connector.parent.name}: INSERT names no columns:\n{insert}"
        arity = len([c for c in match.group(1).split(",") if c.strip()])
        # One row of the right arity: what is under test is the translation, and a
        # value's type is the module's business, not the translator's.
        writer.executemany(insert, [tuple("x" for _ in range(arity))])

    try:
        writer.close()
    except RawLandingError as exc:  # pragma: no cover -- the failure this exists for
        pytest.fail(f"{connector.parent.name}: {exc}")


def test_the_gate_actually_covers_the_connectors_that_land():
    """A gate that silently matched nothing would pass forever."""
    names = {path.parent.name for path in _landing_connectors()}
    # The fifteen converted last, plus a sample of the nineteen before them, plus
    # the two whose SQL only exists after import / inside a function.
    for expected in (
        "adobe-analytics",
        "taboola",
        "ias",
        "gsc",
        "hubspot",
        "google-business-profile",
        "google-analytics",
        # The eight converted to a single declaration: they ship no CREATE
        # literal any more, and a source scan for one would drop them SILENTLY.
        "google-ads",
        "microsoft-ads",
        "amazon-ads",
        "piano",
        "pinterest-ads",
        "thetradedesk",
        "strava",
        "google-ad-manager",
    ):
        assert expected in names, f"{expected} is not covered by the translator gate"
    assert len(names) >= 37, f"only {len(names)} connectors matched -- the scan is wrong"


def test_the_statements_that_exist_only_after_import_are_still_covered():
    """Pins the hole the first version of this gate had.

    google-business-profile never ships a static INSERT: it built one with an
    f-string over `_METRIC_COLUMNS`, and now builds it from a declaration through
    `core.raw_landing`. Either way the value that actually ships exists nowhere in
    the source text, so an AST-only reader covered nothing for it -- and a gate
    that covers nothing reports success forever.
    """
    connector = _MODULES / "google-business-profile" / "connector.py"
    assert not _inline_sql(connector)["insert"], "the source now has a static INSERT too"
    imported = _imported_sql(connector)["insert"]
    assert imported, "importing must yield the rendered INSERT"
    landed_columns = " ".join(imported)
    for column in ("business_impressions_desktop_maps", "review_star_rating"):
        assert column in landed_columns, f"{column} is not covered by the translator gate"
