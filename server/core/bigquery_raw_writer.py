"""A raw writer that speaks the connectors' DuckDB idiom to BigQuery.

Twenty-one connector modules already land through one seam --
``warehouse_write.open_raw_writer(path, project_id)`` -- and then use exactly
three statements against it, all of them module-authored constants:

    CREATE TABLE IF NOT EXISTS <t> (<col> <type>, ...)
    ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <col> <type>
    INSERT INTO <t> (<cols>) VALUES (?, ?, ...)

So rather than rewrite sixty-six landing functions -- each with its own nullable
handling, its own column guards and its own signature, which is sixty-six
chances to mis-map a column silently -- this object presents the same
``execute`` / ``executemany`` / ``close`` interface and translates those three
shapes to BigQuery. A module converts by dropping its ``db_mode`` guard.

STRICT BY DESIGN
    The grammar below is deliberately narrow and every parse REFUSES rather than
    guesses. A statement this does not recognise raises, loudly, at the moment it
    is issued. The alternative -- a permissive parser that does something
    plausible with SQL it half-understood -- would put wrong data in the
    warehouse and report a success, which is the one failure mode that is not
    recoverable by reading logs later.

BUFFERED UNTIL close()
    ``executemany`` accumulates; ``close`` performs one landing per table. A pull
    therefore costs one load job (or one streaming request) rather than one per
    batch, and a failure surfaces before anything partial is reported as done.
    Callers already call ``close()`` -- that contract is unchanged.

AD-2: no provider names, no imports from ``server/modules``.
"""

from __future__ import annotations

import ast
import logging
import re

from core.raw_landing import RawLandingError, land_raw_rows

logger = logging.getLogger(__name__)

# Sentinel: a column with no DEFAULT clause is not the same as one defaulting to
# NULL -- the second must still be written when the INSERT omits the column.
_NO_DEFAULT = object()

_CREATE = re.compile(
    r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*;?\s*$",
    re.I | re.S,
)
_ALTER = re.compile(
    r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z0-9_]+(?:\s+DEFAULT\s+\S+)?)\s*;?\s*$",
    re.I | re.S,
)
# `name TYPE DEFAULT <literal>`. The default is CARRIED, not dropped: a column the
# INSERT omits takes it on DuckDB, so dropping it here would land NULL instead --
# a difference between the two backends that nothing would report.
_DEFAULT = re.compile(r"^(.*?)\s+DEFAULT\s+(.+)$", re.I | re.S)
_INSERT = re.compile(
    r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)\s*;?\s*$",
    re.I | re.S,
)

# DuckDB's declared types, mapped onto the vocabulary core.raw_landing accepts.
# An unmapped type is refused: inventing a column type is how a warehouse ends up
# with a number stored as text and a chart that sorts 10 before 9.
_TYPE_MAP = {
    "VARCHAR": "STRING",
    "TEXT": "STRING",
    "STRING": "STRING",
    "DOUBLE": "FLOAT",
    "FLOAT": "FLOAT",
    "REAL": "FLOAT",
    "DECIMAL": "FLOAT",
    "NUMERIC": "FLOAT",
    "BIGINT": "INTEGER",
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "SMALLINT": "INTEGER",
    # BOOLEAN maps to itself, not to STRING. Mapping it to STRING type-checked
    # fine and then failed at the wire: the modules put a Python ``bool`` in the
    # tuple, and storage_write serialises it into a TYPE_STRING proto field --
    # "bad argument type for built-in operation". Same reasoning as _PROTO_TYPES
    # preferring INT64 over its string form: the declared type IS the contract.
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "JSON": "JSON",
}


def _split_default(declared: str) -> tuple[str, object]:
    """Separate ``TYPE DEFAULT <literal>`` into its type and its default value.

    Only a literal is accepted. A default that is an expression -- ``now()``,
    ``nextval(...)`` -- would have to be evaluated to be reproduced, so it is
    refused here rather than quietly becoming NULL on one backend only.
    """
    match = _DEFAULT.match((declared or "").strip())
    if not match:
        return declared, _NO_DEFAULT
    kind, literal = match.group(1).strip(), match.group(2).strip().rstrip(";")
    upper = literal.upper()
    if upper == "NULL":
        return kind, None
    if upper in ("TRUE", "FALSE"):
        return kind, upper == "TRUE"
    try:
        return kind, ast.literal_eval(literal)
    except (ValueError, SyntaxError) as exc:
        raise RawLandingError(
            f"bigquery raw writer: column default {literal!r} is not a literal"
        ) from exc


def _map_type(declared: str) -> str:
    base = (declared or "").strip().upper().split("(")[0].strip()
    if base not in _TYPE_MAP:
        raise RawLandingError(
            f"bigquery raw writer: column type {declared!r} has no BigQuery mapping"
        )
    return _TYPE_MAP[base]


def _split_columns(body: str) -> tuple[list[tuple[str, str]], dict[str, object]]:
    """Split a CREATE TABLE body into (name, type) pairs plus its column defaults.

    Depth-aware so a parameterised type -- DECIMAL(18, 2) -- is not torn in half
    at its own comma.
    """
    parts, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))

    columns: list[tuple[str, str]] = []
    defaults: dict[str, object] = {}
    for part in parts:
        tokens = part.strip().split()
        if not tokens:
            continue
        if tokens[0].upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"}:
            raise RawLandingError(
                f"bigquery raw writer: table constraint {part.strip()!r} is not translatable"
            )
        if len(tokens) < 2:
            raise RawLandingError(f"bigquery raw writer: cannot read column {part.strip()!r}")
        declared, default = _split_default(" ".join(tokens[1:]))
        columns.append((tokens[0], _map_type(declared)))
        if default is not _NO_DEFAULT:
            defaults[tokens[0]] = default
    return columns, defaults


class BigQueryRawWriter:
    """DuckDB-shaped writer that lands into BigQuery. Returned by open_raw_writer."""

    def __init__(self, project_id: str | None = None, mode: str | None = None):
        self.project_id = project_id
        self.mode = mode
        # table -> ordered (name, type); table -> accumulated row dicts
        self._schemas: dict[str, list[tuple[str, str]]] = {}
        self._defaults: dict[str, dict[str, object]] = {}
        self._pending: dict[str, list[dict]] = {}
        self.landed: list[dict] = []

    # -- the DuckDB connection interface the modules already use ---------------

    def execute(self, sql: str, parameters=None):
        """Run one statement. A single-row INSERT is accepted as a 1-row batch."""
        create = _CREATE.match(sql or "")
        if create:
            table, body = create.group(1), create.group(2)
            self._schemas[table], self._defaults[table] = _split_columns(body)
            return self

        alter = _ALTER.match(sql or "")
        if alter:
            table, column, declared = alter.group(1), alter.group(2), alter.group(3)
            declared, default = _split_default(declared)
            existing = self._schemas.setdefault(table, [])
            if column not in [name for name, _ in existing]:
                existing.append((column, _map_type(declared)))
            if default is not _NO_DEFAULT:
                self._defaults.setdefault(table, {})[column] = default
            return self

        if _INSERT.match(sql or ""):
            self.executemany(sql, [parameters] if parameters is not None else [])
            return self

        raise RawLandingError(
            f"bigquery raw writer: statement not translatable: {(sql or '').strip()[:120]!r}"
        )

    def executemany(self, sql: str, values):
        """Buffer a batch of positional rows against the INSERT's column list."""
        insert = _INSERT.match(sql or "")
        if not insert:
            raise RawLandingError(
                f"bigquery raw writer: not an INSERT: {(sql or '').strip()[:120]!r}"
            )
        table = insert.group(1)
        columns = [part.strip() for part in insert.group(2).split(",") if part.strip()]
        placeholders = [part.strip() for part in insert.group(3).split(",") if part.strip()]
        if len(columns) != len(placeholders):
            raise RawLandingError(
                f"bigquery raw writer: {table}: {len(columns)} columns but "
                f"{len(placeholders)} placeholders"
            )
        if any(mark != "?" for mark in placeholders):
            # A literal in the VALUES list means the positional mapping below is
            # wrong for every row after it.
            raise RawLandingError(f"bigquery raw writer: {table}: non-parameter in VALUES")

        rows = self._pending.setdefault(table, [])
        for record in values or []:
            if len(record) != len(columns):
                raise RawLandingError(
                    f"bigquery raw writer: {table}: row has {len(record)} values "
                    f"for {len(columns)} columns"
                )
            rows.append(dict(zip(columns, record)))
        return self

    def close(self):
        """Land every buffered table, then clear. One landing per table."""
        try:
            for table, rows in self._pending.items():
                if not rows:
                    continue
                schema = self._schemas.get(table)
                if not schema:
                    raise RawLandingError(
                        f"bigquery raw writer: {table}: rows inserted with no CREATE TABLE seen"
                    )
                # A column the INSERT names but the CREATE did not declare would be
                # dropped silently by the landing, so it is caught here instead.
                declared = {name for name, _ in schema}
                unknown = {key for row in rows for key in row} - declared
                if unknown:
                    raise RawLandingError(
                        f"bigquery raw writer: {table}: inserted columns not in the "
                        f"table definition: {sorted(unknown)}"
                    )
                # A column the INSERT never names takes its declared default, the
                # way DuckDB applies it. Without this the two backends disagree on
                # exactly the columns someone bothered to give a default to.
                for column, value in (self._defaults.get(table) or {}).items():
                    for row in rows:
                        row.setdefault(column, value)
                self.landed.append(
                    land_raw_rows(
                        table,
                        rows,
                        columns=schema,
                        project_id=self.project_id,
                        mode=self.mode,
                        backend="bigquery",
                    )
                )
        finally:
            self._pending.clear()

    # DuckDB connections support `with`; modules may use either form.
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False
