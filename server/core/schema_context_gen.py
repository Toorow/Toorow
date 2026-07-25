"""toorow -- Schema Context Generator (Story 11.2).

Profiles warehouse relations (DuckDB/Postgres) and persists versioned
columns.md, description.md, and preview.md docs to app.schema_context in Postgres.
Enforces single-writer idempotency (AD-17), append-only historisation
(app.schema_context_versions), and PII redaction.

Fix pass (2026-07-20, 3-reviewer REJECT):
  - AD-17 single writer: this module is the ONLY writer of app.schema_context.
    Human/API paths never write it (structural: no CRUD route targets the table).
  - Historisation (Jean): every content-changing UPDATE appends the PREVIOUS body
    to app.schema_context_versions before overwriting. Identical content => no
    UPDATE and no version row (idempotent, zero-churn).
  - SQL identifier injection: every interpolated relation/column identifier is
    validated (^[A-Za-z_][A-Za-z0-9_]*$) and double-quoted before interpolation.
  - Warehouse opened read_only=True (structural AD-8 enforcement).
  - Per-relation atomicity: the 3 doc upserts run inside one SAVEPOINT.
  - No exception text leaks into body_md.
  - Deterministic preview ordering (ORDER BY 1) for byte-identical repeat runs.
"""

from __future__ import annotations

import argparse
import logging
import re
from typing import Any

from ulid import ULID

logger = logging.getLogger(__name__)

# PII column-name heuristics (LOW fix: extended with api_key, private_key,
# card_number, dob, birth). Matched case-insensitively as substrings.
PII_PATTERN = re.compile(
    r"(email|token|password|secret|phone|ssn|credit_card|card_number|"
    r"api_key|private_key|dob|birth)",
    re.IGNORECASE,
)

DEFAULT_ALLOWLIST_PATTERNS = (r"^stg_", r"^mart_", r"^dim_", r"^fct_")

# Safe SQL identifier: must start with a letter/underscore, then alnum/underscore.
_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnsafeIdentifierError(ValueError):
    """Raised when a relation/column identifier fails the safe-identifier check."""


def _quote_ident(identifier: str) -> str:
    """Validate and double-quote a SQL identifier for safe interpolation.

    HIGH fix (SQL identifier injection): DuckDB/psycopg cannot parameterise
    identifiers, so every relation/column name interpolated into an f-string
    query is validated against ^[A-Za-z_][A-Za-z0-9_]*$ and then double-quoted
    (with `"` doubled) before use. An identifier that fails validation raises
    UnsafeIdentifierError so a malicious/mangled name can never reach the DB.
    """
    if not isinstance(identifier, str) or not _SQL_IDENT_RE.match(identifier):
        raise UnsafeIdentifierError(f"Unsafe SQL identifier: {identifier!r}")
    return '"' + identifier.replace('"', '""') + '"'


def is_pii_column(column_name: str) -> bool:
    """Check if column name matches PII patterns."""
    return bool(PII_PATTERN.search(column_name))


def is_relation_allowed(
    relation_name: str, allowlist: list[str] | tuple[str, ...] | None = None
) -> bool:
    """Check if relation is in explicit allowlist or matches default safe patterns.

    LOW fix: an EXPLICIT empty allowlist ([]) means "nothing allowed" -- it must
    NOT fall through to the default safe patterns. Only `allowlist is None`
    (caller supplied nothing) uses the default patterns.
    """
    if allowlist is not None:
        return relation_name in allowlist
    return any(
        re.match(pattern, relation_name, re.IGNORECASE)
        for pattern in DEFAULT_ALLOWLIST_PATTERNS
    )


def inspect_relation_schema(
    warehouse_conn: Any, relation_name: str
) -> list[dict[str, Any]]:
    """Inspect columns, types, and nullability of a warehouse relation."""
    q_rel = _quote_ident(relation_name)
    columns: list[dict[str, Any]] = []
    with warehouse_conn.cursor() as cur:
        try:
            # PRAGMA table_info requires a quoted identifier (validated above).
            cur.execute(f"PRAGMA table_info({q_rel})")
            rows = cur.fetchall()
            if rows:
                for row in rows:
                    columns.append(
                        {
                            "name": str(row[1]),
                            "type": str(row[2]).upper(),
                            "nullable": not bool(row[3]),
                        }
                    )
                return columns
        except Exception:
            pass

        # Fallback to information_schema.columns (parameterised -- no interpolation).
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position
            """,
            (relation_name,),
        )
        rows = cur.fetchall()
        for row in rows:
            columns.append(
                {
                    "name": str(row[0]),
                    "type": str(row[1]).upper(),
                    "nullable": str(row[2]).upper() in ("YES", "TRUE", "1"),
                }
            )

    return columns


def profile_relation_stats(
    warehouse_conn: Any, relation_name: str, columns: list[dict[str, Any]]
) -> dict[str, Any]:
    """Profile row count, min/max for numeric/dates, and distinct count for columns."""
    q_rel = _quote_ident(relation_name)
    stats: dict[str, Any] = {"row_count": 0, "column_stats": {}}
    with warehouse_conn.cursor() as cur:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {q_rel}")
            row = cur.fetchone()
            stats["row_count"] = row[0] if row else 0
        except Exception as exc:
            logger.warning("Failed to get row count for %s: %s", relation_name, exc)
            return stats

        for col in columns:
            c_name = col["name"]
            c_type = col["type"]
            c_stat: dict[str, Any] = {}

            if is_pii_column(c_name):
                c_stat["pii"] = True
                stats["column_stats"][c_name] = c_stat
                continue

            try:
                q_col = _quote_ident(c_name)
            except UnsafeIdentifierError:
                logger.warning(
                    "Skipping stats for unsafe column identifier %r on %s",
                    c_name,
                    relation_name,
                )
                stats["column_stats"][c_name] = c_stat
                continue

            try:
                numeric_types = (
                    "INT",
                    "FLOAT",
                    "NUMERIC",
                    "DECIMAL",
                    "DOUBLE",
                    "DATE",
                    "TIME",
                    "TIMESTAMP",
                )
                if any(t in c_type for t in numeric_types):
                    cur.execute(
                        f"SELECT MIN({q_col}), MAX({q_col}), COUNT(DISTINCT {q_col}) "
                        f"FROM {q_rel}"
                    )
                    r = cur.fetchone()
                    if r:
                        c_stat["min"] = r[0]
                        c_stat["max"] = r[1]
                        c_stat["distinct"] = r[2]
                else:
                    cur.execute(f"SELECT COUNT(DISTINCT {q_col}) FROM {q_rel}")
                    r = cur.fetchone()
                    if r:
                        c_stat["distinct"] = r[0]
            except Exception:
                pass

            stats["column_stats"][c_name] = c_stat

    return stats


def build_columns_markdown(columns: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    """Build Markdown documentation for columns schema and stats."""
    lines = [
        "# Schema Columns",
        "",
        "| Column | Type | Nullable | Distinct | Min | Max | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    col_stats = stats.get("column_stats", {})
    for col in columns:
        name = col["name"]
        ctype = col["type"]
        nullable = "Yes" if col["nullable"] else "No"
        cs = col_stats.get(name, {})
        notes = "PII (Redacted)" if cs.get("pii") else ""
        distinct = str(cs.get("distinct", "-"))
        c_min = str(cs.get("min", "-")) if cs.get("min") is not None else "-"
        c_max = str(cs.get("max", "-")) if cs.get("max") is not None else "-"
        lines.append(
            f"| `{name}` | {ctype} | {nullable} | {distinct} | {c_min} | {c_max} | {notes} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_description_markdown(
    relation_name: str, row_count: int, dbt_manifest: dict[str, Any] | None = None
) -> str:
    """Build Markdown description doc for a relation."""
    description = f"Warehouse relation `{relation_name}` containing approx {row_count} rows."
    if dbt_manifest:
        nodes = dbt_manifest.get("nodes", {})
        for node in nodes.values():
            if node.get("name") == relation_name:
                description = node.get("description", description)
                break

    lines = [
        f"# Relation: {relation_name}",
        "",
        f"**Description:** {description}",
        f"**Row Count:** {row_count}",
        "**Kind:** Warehouse Table / View",
        "",
    ]
    return "\n".join(lines)


def build_preview_markdown(
    warehouse_conn: Any, relation_name: str, columns: list[dict[str, Any]], limit: int = 5
) -> str:
    """Build Markdown preview doc with sample rows and PII redaction.

    Idempotence (CRITICAL fix): a deterministic ``ORDER BY 1`` is appended so
    two runs over an unchanged relation emit byte-identical preview markdown
    (an unordered LIMIT can return rows in a different physical order between
    runs, causing a spurious diff -> spurious version row).

    body_md leak (MEDIUM fix): on a read error we log the detail at WARNING and
    persist a generic, deterministic ``_No sample rows available._`` line --
    the exception text is NEVER embedded into body_md.
    """
    col_names = [c["name"] for c in columns]
    header = "| " + " | ".join(col_names) + " |"
    divider = "| " + " | ".join(["---"] * len(col_names)) + " |"
    lines = [f"# Preview: {relation_name}", "", header, divider]

    q_rel = _quote_ident(relation_name)
    with warehouse_conn.cursor() as cur:
        try:
            # ORDER BY 1 (first column) makes repeat runs byte-identical.
            cur.execute(f"SELECT * FROM {q_rel} ORDER BY 1 LIMIT {int(limit)}")
            rows = cur.fetchall()
            for row in rows:
                row_vals = []
                for idx, val in enumerate(row):
                    if is_pii_column(col_names[idx]):
                        row_vals.append("[REDACTED]")
                    elif val is None:
                        row_vals.append("`null`")
                    else:
                        row_vals.append(str(val).replace("\n", " "))
                lines.append("| " + " | ".join(row_vals) + " |")
        except Exception as exc:
            # Log the detail; do NOT persist exception text into body_md.
            logger.warning("Failed to fetch preview for %s: %s", relation_name, exc)
            lines.append("_No sample rows available._")

    lines.append("")
    return "\n".join(lines)


def _append_schema_context_version(
    cur: Any,
    *,
    schema_context_id: str,
    project_id: str,
    relation: str,
    doc_kind: str,
    body_md: str,
    generated_at: Any,
    changed_by: str,
) -> None:
    """Append a row to app.schema_context_versions (append-only history).

    Records the PREVIOUS (pre-update) body of the doc that is about to be
    overwritten. version_number = MAX(existing) + 1 for this schema_context_id.
    The versions table is immutable (BEFORE UPDATE/DELETE/TRUNCATE triggers).
    """
    cur.execute(
        """
        SELECT COALESCE(MAX(version_number), 0)
        FROM app.schema_context_versions
        WHERE schema_context_id = %s
        """,
        (schema_context_id,),
    )
    row = cur.fetchone()
    next_ver = (row[0] if row else 0) + 1
    cur.execute(
        """
        INSERT INTO app.schema_context_versions
            (schema_context_id, project_id, relation, doc_kind, body_md,
             generated_at, version_number, changed_by, changed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        """,
        (
            schema_context_id,
            project_id,
            relation,
            doc_kind,
            body_md,
            generated_at,
            next_ver,
            changed_by,
        ),
    )


def upsert_schema_context_doc(
    conn: Any,
    *,
    project_id: str,
    relation: str,
    doc_kind: str,
    body_md: str,
    changed_by: str = "schema_context_gen",
) -> bool:
    """Upsert schema context doc idempotently (AD-17). Returns True if updated/inserted.

    Historisation (Jean, 2026-07-20): when an existing doc's body CHANGES, the
    PREVIOUS body is appended to app.schema_context_versions BEFORE the UPDATE
    overwrites it. This preserves the full history of what each doc looked like.

    Idempotent: identical content => NO update AND NO version row (zero churn).
    Insert of a brand-new doc creates NO version row (there is no prior body to
    historise; the live row IS version "current").
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, body_md, generated_at FROM app.schema_context
            WHERE project_id = %s AND relation = %s AND doc_kind = %s
            """,
            (project_id, relation, doc_kind),
        )
        existing = cur.fetchone()
        if existing:
            existing_id, existing_body, existing_generated_at = (
                existing[0],
                existing[1],
                existing[2],
            )
            if existing_body == body_md:
                return False  # No change: no update, no version (idempotent).

            # Historise the PREVIOUS body before overwriting (append-only).
            _append_schema_context_version(
                cur,
                schema_context_id=existing_id,
                project_id=project_id,
                relation=relation,
                doc_kind=doc_kind,
                body_md=existing_body,
                generated_at=existing_generated_at,
                changed_by=changed_by,
            )

            cur.execute(
                """
                UPDATE app.schema_context
                SET body_md = %s, generated_at = now()
                WHERE id = %s
                """,
                (body_md, existing_id),
            )
            return True

        sctx_id = f"sctx_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.schema_context
                (id, project_id, relation, doc_kind, body_md, generated_at, created_at)
            VALUES
                (%s, %s, %s, %s, %s, now(), now())
            """,
            (sctx_id, project_id, relation, doc_kind, body_md),
        )
        return True


def generate_schema_context(
    conn: Any,
    *,
    project_id: str,
    warehouse_conn: Any,
    allowlist: list[str] | tuple[str, ...] | None = None,
    dbt_manifest: dict[str, Any] | None = None,
    changed_by: str = "schema_context_gen",
) -> dict[str, Any]:
    """Generate and upsert schema context for allowed warehouse relations.

    Per-relation atomicity (MEDIUM fix): the 3 doc upserts (columns/description/
    preview) for one relation run inside a single SAVEPOINT so a relation yields
    all-3 doc writes or none -- a mid-relation failure never leaves a partial
    doc set committed.
    """
    relations: list[str] = []
    with warehouse_conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema IN ('main', 'public', 'app')"
            )
            rows = cur.fetchall()
            for r in rows:
                relations.append(str(r[0]))
        except Exception:
            pass

    if not relations and allowlist:
        relations = list(allowlist)

    stats_summary = {"processed": 0, "updated": 0, "skipped": 0, "errors": []}

    for rel in relations:
        if not is_relation_allowed(rel, allowlist=allowlist):
            continue

        stats_summary["processed"] += 1
        savepoint = f"sp_{ULID()}".replace("-", "")
        try:
            columns = inspect_relation_schema(warehouse_conn, rel)
            if not columns:
                stats_summary["skipped"] += 1
                continue

            rel_stats = profile_relation_stats(warehouse_conn, rel, columns)
            cols_md = build_columns_markdown(columns, rel_stats)
            desc_md = build_description_markdown(
                rel, rel_stats["row_count"], dbt_manifest
            )
            prev_md = build_preview_markdown(warehouse_conn, rel, columns, limit=5)

            # Per-relation SAVEPOINT: all-3 doc upserts commit together or roll back.
            with conn.cursor() as sp_cur:
                sp_cur.execute(f"SAVEPOINT {savepoint}")
            try:
                u1 = upsert_schema_context_doc(
                    conn, project_id=project_id, relation=rel, doc_kind="columns",
                    body_md=cols_md, changed_by=changed_by,
                )
                u2 = upsert_schema_context_doc(
                    conn, project_id=project_id, relation=rel, doc_kind="description",
                    body_md=desc_md, changed_by=changed_by,
                )
                u3 = upsert_schema_context_doc(
                    conn, project_id=project_id, relation=rel, doc_kind="preview",
                    body_md=prev_md, changed_by=changed_by,
                )
                with conn.cursor() as sp_cur:
                    sp_cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                with conn.cursor() as sp_cur:
                    sp_cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                raise

            if u1 or u2 or u3:
                stats_summary["updated"] += 1
            else:
                stats_summary["skipped"] += 1
        except Exception as exc:
            logger.error("Failed schema_context_gen for relation %s: %s", rel, exc)
            stats_summary["errors"].append({"relation": rel, "error": str(exc)})

    conn.commit()
    return stats_summary


def main() -> None:
    """CLI entry point for schema context generator."""
    parser = argparse.ArgumentParser(
        description="Schema Context Generator (Story 11.2)"
    )
    parser.add_argument("--project-id", required=True, help="Target project ID")
    parser.add_argument(
        "--allowlist",
        nargs="*",
        help="Optional space-separated list of allowed relation names",
    )
    args = parser.parse_args()

    from core.db import get_connection, get_warehouse_connection  # noqa: PLC0415

    logging.basicConfig(level=logging.INFO)
    logger.info("Starting schema context generation for project %s...", args.project_id)

    # AD-8 (MEDIUM fix): open the warehouse connection read-only.
    with get_connection() as conn, get_warehouse_connection(read_only=True) as warehouse_conn:
        res = generate_schema_context(
            conn,
            project_id=args.project_id,
            warehouse_conn=warehouse_conn,
            allowlist=args.allowlist,
        )
        logger.info("Schema context generation complete: %s", res)


if __name__ == "__main__":
    main()
