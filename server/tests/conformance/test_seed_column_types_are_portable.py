"""Seed `column_types` are spelled so that BOTH warehouses accept them -- AI-314.

2026-08-30, nightly on production: five seeds failed to load on BigQuery with
`Invalid value for type: BIGINT is not a valid value` and `VARCHAR is not a
valid value`. A seed's `column_types` goes straight into the load job's
schema -- the BigQuery API validates it against ITS type names (INT64, STRING,
FLOAT64, NUMERIC, BOOL, DATE, TIMESTAMP...), and DuckDB accepts INT64 and
STRING as aliases (proved 2026-08-30: `create table t (x INT64)` -> BIGINT,
`STRING` -> VARCHAR). A DuckDB-only spelling is green locally and red every
night, so this guard reads every seed schema of the repo and refuses any
spelling outside the set both dialects accept. FLOAT64 is NOT in the set:
DuckDB has no such alias. A column whose precision matters locally (a
`DECIMAL(5,4)` confidence) spells BOTH dialects in one jinja expression --
`"{{ 'NUMERIC' if target.type == 'bigquery' else 'DECIMAL(5,4)' }}"` -- and the
guard checks each branch against its own warehouse (DuckDB rendered it to
DECIMAL(5,4) on 2026-08-30; BigQuery NUMERIC is its own 38,9).
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]

# Spellings accepted verbatim by BigQuery's load-job schema AND by DuckDB.
PORTABLE_TYPES = {"INT64", "STRING", "NUMERIC", "BOOL", "BOOLEAN", "DATE", "TIMESTAMP"}
# What BigQuery's load-job schema enum accepts (TableFieldSchema.type).
BIGQUERY_TYPES = PORTABLE_TYPES | {"INTEGER", "FLOAT", "FLOAT64", "BIGNUMERIC", "BYTES",
                                   "TIME", "DATETIME", "JSON"}
# Parametrised decimals are a DuckDB spelling; BigQuery wants precision on the field.
_DUCKDB_DECIMAL = re.compile(r"^(DECIMAL|NUMERIC)\(\d+,\s*\d+\)$", re.I)
_DUAL = re.compile(
    r"^\{\{\s*'([^']+)'\s+if\s+target\.type\s*==\s*'bigquery'\s+else\s+'([^']+)'\s*\}\}$"
)


def _accepted(typ: str) -> bool:
    spelled = typ.strip()
    dual = _DUAL.match(spelled)
    if dual:
        bigquery, duckdb = dual.group(1).upper(), dual.group(2).upper()
        return bigquery in BIGQUERY_TYPES and (
            duckdb in PORTABLE_TYPES or bool(_DUCKDB_DECIMAL.match(duckdb))
        )
    return spelled.upper() in PORTABLE_TYPES


def _seed_schema_files() -> list[Path]:
    files = sorted((REPO / "dbt" / "seeds").glob("*.yml"))
    files += sorted((REPO / "server" / "modules").glob("*/dbt/**/*.yml"))
    files.append(REPO / "dbt" / "dbt_project.yml")
    return [f for f in files if f.exists()]


def _column_types(node, path: str = "") -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("column_types", "+column_types") and isinstance(value, dict):
                found += [(path, str(col), str(typ)) for col, typ in value.items()]
            else:
                found += _column_types(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found += _column_types(item, f"{path}[{i}]")
    return found


def test_every_seed_column_type_is_a_spelling_both_warehouses_accept():
    offenders: list[str] = []
    declared = 0
    for file in _seed_schema_files():
        doc = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        for where, column, typ in _column_types(doc):
            declared += 1
            if _accepted(typ):
                continue
            offenders.append(f"{file.relative_to(REPO)}:{where} {column}: {typ}")
    assert declared, "no seed column_types found -- the guard measures nothing"
    assert not offenders, (
        "seed column_types that BigQuery or DuckDB refuses (use INT64 / STRING / "
        "NUMERIC / BOOL / DATE / TIMESTAMP):\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "bad",
    ["bigint", "VARCHAR", "double", "integer", "DECIMAL(5,4)",
     "{{ 'DECIMAL(5,4)' if target.type == 'bigquery' else 'NUMERIC' }}"],
)
def test_the_guard_refuses_a_dialect_only_spelling(bad: str):
    assert not _accepted(bad)


@pytest.mark.parametrize(
    "good",
    ["INT64", "string", "BOOLEAN",
     "{{ 'NUMERIC' if target.type == 'bigquery' else 'DECIMAL(5,4)' }}"],
)
def test_the_guard_accepts_what_both_warehouses_load(good: str):
    assert _accepted(good)


_DECIMAL_LITERAL = re.compile(r"^-?\d+\.\d+$")
_INTEGRAL = re.compile(r"^-?\d+(\.0+)?$")


def _declared_types() -> dict[str, set[str]]:
    declared: dict[str, set[str]] = {}
    for file in _seed_schema_files():
        doc = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        for seed in doc.get("seeds") or []:
            if not isinstance(seed, dict) or "name" not in seed:
                continue
            config = seed.get("config") or {}
            declared.setdefault(str(seed["name"]), set()).update(
                str(c) for c in (config.get("column_types") or {})
            )
    return declared


def test_an_integral_decimal_column_declares_its_type():
    """2026-08-30, second night: `value_decimal` holds "100.00" and blanks. Both
    warehouses infer INT64 from all-integral values; DuckDB then parses "100.00"
    into it, BigQuery's CSV loader refuses the literal (`Unable to parse ...
    column_type: INT64 value: "100.00"`). Inference is not a contract -- such a
    column names its type, in the dual form."""
    declared = _declared_types()
    offenders: list[str] = []
    for file in sorted((REPO / "dbt" / "seeds").glob("*.csv")):
        with file.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            continue
        for column in rows[0]:
            values = [row.get(column) or "" for row in rows]
            if not any(_DECIMAL_LITERAL.match(v) for v in values):
                continue
            integral = all(v == "" or _INTEGRAL.match(v) for v in values)
            if integral and column not in declared.get(file.stem, set()):
                offenders.append(f"{file.stem}.{column}")
    assert not offenders, (
        "seed columns whose integral decimals infer INT64 and fail BigQuery's CSV loader "
        "-- declare column_types for: " + ", ".join(offenders)
    )
