"""toorow -- Context layer domain service (Story 11.1).

Manages context_topics and procedures CRUD, YAML frontmatter validation,
version-append history in context_topics_versions / procedures_versions,
and AD-5 scope resolution.
"""

from __future__ import annotations

from typing import Any

import psycopg
import yaml
from ulid import ULID

from core.audit import (
    ACTION_CONTEXT_TOPIC_ARCHIVED,
    ACTION_CONTEXT_TOPIC_CREATED,
    ACTION_CONTEXT_TOPIC_UPDATED,
    ACTION_GRAPH_EDGE_CREATED,
    ACTION_GRAPH_EDGE_DELETED,
    ACTION_PROCEDURE_ARCHIVED,
    ACTION_PROCEDURE_CREATED,
    ACTION_PROCEDURE_UPDATED,
    insert_audit_row,
)


class DuplicateProcedureNameError(ValueError):
    """Raised when a procedure name already exists within the target project/platform scope."""

    pass


def validate_procedure_frontmatter(yaml_text: str) -> dict[str, Any]:
    """Validate procedure YAML frontmatter.

    Requires a YAML mapping containing both 'name' (non-empty string) and
    'description' (string). Additional keys are preserved.
    Raises ValueError with French message on failure.
    """
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise ValueError("Le frontmatter YAML est vide ou invalide.")

    try:
        data = yaml.safe_load(yaml_text)
    except Exception as exc:
        raise ValueError(f"Le frontmatter YAML ne peut pas être analysé: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Le frontmatter YAML doit être un dictionnaire d'attributs.")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Le frontmatter YAML doit contenir une clé 'name' (chaîne non vide).")

    description = data.get("description")
    if not isinstance(description, str):
        raise ValueError(
            "Le frontmatter YAML doit contenir une clé 'description' (chaîne de caractères)."
        )

    parsed = dict(data)
    parsed["name"] = name.strip()
    parsed["description"] = description
    return parsed


def _format_datetime(dt: Any) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _row_to_topic(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for col, val in zip(cols, row):
        if col in ("created_at", "updated_at") and val is not None:
            record[col] = _format_datetime(val)
        else:
            record[col] = val
    return record


# ---------------------------------------------------------------------------
# Topics Domain Logic
# ---------------------------------------------------------------------------


def create_topic(
    conn: Any,
    *,
    project_id: str | None,
    title: str,
    body_md: str = "",
    created_by: str,
) -> dict[str, Any]:
    """Create a context topic and append version 1."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Le titre du topic ne peut pas être vide.")

    topic_id = f"top_{ULID()}"
    clean_title = title.strip()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_topics
                (id, project_id, title, body_md, status, created_by, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, 'active', %s, now(), now())
            RETURNING id, project_id, title, body_md, status, created_by, created_at, updated_at
            """,
            (topic_id, project_id, clean_title, body_md, created_by),
        )
        cols = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        topic = _row_to_topic(row, cols)

        cur.execute(
            """
            INSERT INTO app.context_topics_versions
                (topic_id, project_id, title, body_md, status, created_by, created_at, updated_at,
                 version_number, changed_by, changed_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, 1, %s, now())
            """,
            (
                topic["id"],
                topic["project_id"],
                topic["title"],
                topic["body_md"],
                topic["status"],
                topic["created_by"],
                topic["created_at"],
                topic["updated_at"],
                created_by,
            ),
        )

        insert_audit_row(
            conn,
            identity=created_by,
            action=ACTION_CONTEXT_TOPIC_CREATED,
            provider_account="platform",
            connection_ref="",
            metadata={
                "topic_id": topic_id,
                "project_id": project_id,
                "title": clean_title,
                "version_number": 1,
            },
        )

    topic["version_number"] = 1
    return topic


def update_topic(
    conn: Any,
    *,
    topic_id: str,
    patch: dict[str, Any],
    changed_by: str,
) -> dict[str, Any]:
    """Update a topic and append a new version row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, title, body_md, status, created_by, created_at, updated_at
            FROM app.context_topics
            WHERE id = %s
            FOR UPDATE
            """,
            (topic_id,),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(f"Topic '{topic_id}' non trouvé.")

        cols = [desc[0] for desc in cur.description]
        current = _row_to_topic(row, cols)

        new_title = patch.get("title", current["title"])
        if isinstance(new_title, str):
            new_title = new_title.strip()
        if not new_title:
            raise ValueError("Le titre du topic ne peut pas être vide.")

        new_body = patch["body_md"] if patch.get("body_md") is not None else current["body_md"]
        new_status = patch.get("status", current["status"])
        if new_status not in ("active", "archived"):
            raise ValueError("Statut invalide.")

        if new_status == "archived" and current["status"] == "archived":
            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 1)
                FROM app.context_topics_versions
                WHERE topic_id = %s
                """,
                (topic_id,),
            )
            max_ver_row = cur.fetchone()
            current["version_number"] = max_ver_row[0] if max_ver_row else 1
            return current

        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0)
            FROM app.context_topics_versions
            WHERE topic_id = %s
            """,
            (topic_id,),
        )
        max_ver = cur.fetchone()[0]
        next_ver = max_ver + 1

        cur.execute(
            """
            UPDATE app.context_topics
            SET title = %s, body_md = %s, status = %s, updated_at = now()
            WHERE id = %s
            RETURNING id, project_id, title, body_md, status, created_by, created_at, updated_at
            """,
            (new_title, new_body, new_status, topic_id),
        )
        updated_row = cur.fetchone()
        updated_topic = _row_to_topic(updated_row, cols)

        cur.execute(
            """
            INSERT INTO app.context_topics_versions
                (topic_id, project_id, title, body_md, status, created_by, created_at, updated_at,
                 version_number, changed_by, changed_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s, %s, now())
            """,
            (
                updated_topic["id"],
                updated_topic["project_id"],
                updated_topic["title"],
                updated_topic["body_md"],
                updated_topic["status"],
                updated_topic["created_by"],
                updated_topic["created_at"],
                updated_topic["updated_at"],
                next_ver,
                changed_by,
            ),
        )

        action = (
            ACTION_CONTEXT_TOPIC_ARCHIVED
            if new_status == "archived" and current["status"] != "archived"
            else ACTION_CONTEXT_TOPIC_UPDATED
        )

        insert_audit_row(
            conn,
            identity=changed_by,
            action=action,
            provider_account="platform",
            connection_ref="",
            metadata={
                "topic_id": topic_id,
                "project_id": updated_topic["project_id"],
                "title": updated_topic["title"],
                "version_number": next_ver,
            },
        )

    updated_topic["version_number"] = next_ver
    return updated_topic


def archive_topic(conn: Any, *, topic_id: str, changed_by: str) -> dict[str, Any]:
    """Archive a topic (sets status='archived' and appends version)."""
    return update_topic(
        conn, topic_id=topic_id, patch={"status": "archived"}, changed_by=changed_by
    )


def get_topic(
    conn: Any, *, topic_id: str, caller_project_id: str | None = None
) -> dict[str, Any] | None:
    """Fetch topic by ID with version number, enforcing scope."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                t.id, t.project_id, t.title, t.body_md, t.status,
                t.created_by, t.created_at, t.updated_at,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.context_topics t
            LEFT JOIN app.context_topics_versions v ON t.id = v.topic_id
            WHERE t.id = %s
            GROUP BY
                t.id, t.project_id, t.title, t.body_md, t.status,
                t.created_by, t.created_at, t.updated_at
            """,
            (topic_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        cols = [desc[0] for desc in cur.description]
        topic = _row_to_topic(row, cols)

        # AD-5 Scope check: platform topics (project_id IS NULL) readable by all;
        # project-scoped topics readable only if caller_project_id matches.
        if (
            caller_project_id is not None
            and topic["project_id"] is not None
            and topic["project_id"] != caller_project_id
        ):
            return None

        return topic


def list_topics(
    conn: Any, *, project_id: str | None, include_archived: bool = False
) -> list[dict[str, Any]]:
    """List topics visible to project_id (platform + project) with version numbers."""
    where_clauses = ["(t.project_id IS NULL OR t.project_id = %s)"]
    params: list[Any] = [project_id]

    if not include_archived:
        where_clauses.append("t.status = 'active'")

    where_sql = " WHERE " + " AND ".join(where_clauses)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                t.id, t.project_id, t.title, t.body_md, t.status,
                t.created_by, t.created_at, t.updated_at,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.context_topics t
            LEFT JOIN app.context_topics_versions v ON t.id = v.topic_id
            {where_sql}
            GROUP BY
                t.id, t.project_id, t.title, t.body_md, t.status,
                t.created_by, t.created_at, t.updated_at
            ORDER BY t.created_at DESC
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_topic(row, cols) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Procedures Domain Logic
# ---------------------------------------------------------------------------


def create_procedure(
    conn: Any,
    *,
    project_id: str | None,
    frontmatter_yaml: str,
    body_md: str = "",
    created_by: str,
) -> dict[str, Any]:
    """Create a procedure, validating frontmatter and appending version 1."""
    fm = validate_procedure_frontmatter(frontmatter_yaml)
    name = fm["name"]
    description = fm["description"]

    proc_id = f"proc_{ULID()}"

    try:
        with conn.cursor() as cur:
            # NOTE: We do NOT pre-check for name duplicates here.
            # The partial unique indexes (uq_procedures_name_platform / uq_procedures_name_project)
            # exclude archived rows (WHERE status != 'archived'), so a manual SELECT-based check
            # would wrongly 409 on reuse of an archived name. We rely solely on the DB
            # UniqueViolation catch below, which respects the partial index exactly.

            cur.execute(
                """
                INSERT INTO app.procedures (
                    id, project_id, name, description, frontmatter_yaml,
                    body_md, status, created_by, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, now(), now())
                RETURNING
                    id, project_id, name, description, frontmatter_yaml,
                    body_md, status, created_by, created_at, updated_at
                """,
                (proc_id, project_id, name, description, frontmatter_yaml, body_md, created_by),
            )
            cols = [desc[0] for desc in cur.description]
            row = cur.fetchone()
            proc = _row_to_topic(row, cols)

            cur.execute(
                """
                INSERT INTO app.procedures_versions
                    (procedure_id, project_id, name, description, frontmatter_yaml, body_md, status,
                     created_by, created_at, updated_at, version_number, changed_by, changed_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, 1, %s, now())
                """,
                (
                    proc["id"],
                    proc["project_id"],
                    proc["name"],
                    proc["description"],
                    proc["frontmatter_yaml"],
                    proc["body_md"],
                    proc["status"],
                    proc["created_by"],
                    proc["created_at"],
                    proc["updated_at"],
                    created_by,
                ),
            )

            insert_audit_row(
                conn,
                identity=created_by,
                action=ACTION_PROCEDURE_CREATED,
                provider_account="platform",
                connection_ref="",
                metadata={
                    "procedure_id": proc_id,
                    "project_id": project_id,
                    "name": name,
                    "version_number": 1,
                },
            )
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateProcedureNameError(
            "Une procédure portant ce nom existe déjà dans ce périmètre."
        ) from exc

    proc["version_number"] = 1
    return proc


def update_procedure(
    conn: Any,
    *,
    procedure_id: str,
    patch: dict[str, Any],
    changed_by: str,
) -> dict[str, Any]:
    """Update a procedure and append a new version row."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, project_id, name, description, frontmatter_yaml,
                    body_md, status, created_by, created_at, updated_at
                FROM app.procedures
                WHERE id = %s
                FOR UPDATE
                """,
                (procedure_id,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Procédure '{procedure_id}' non trouvée.")

            cols = [desc[0] for desc in cur.description]
            current = _row_to_topic(row, cols)

            new_fm_yaml = patch.get("frontmatter_yaml", current["frontmatter_yaml"])
            fm = validate_procedure_frontmatter(new_fm_yaml)
            new_name = fm["name"]
            new_desc = fm["description"]

            if new_name != current["name"]:
                # Check duplicate name, excluding archived rows to match
                # partial unique index semantics.
                p_id = current["project_id"]
                if p_id is None:
                    cur.execute(
                        """
                        SELECT 1 FROM app.procedures
                        WHERE name = %s AND project_id IS NULL AND id != %s
                          AND status != 'archived'
                        """,
                        (new_name, procedure_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT 1 FROM app.procedures
                        WHERE name = %s AND project_id = %s AND id != %s
                          AND status != 'archived'
                        """,
                        (new_name, p_id, procedure_id),
                    )
                if cur.fetchone():
                    raise DuplicateProcedureNameError(
                        "Une procédure portant ce nom existe déjà dans ce périmètre."
                    )

            new_body = patch["body_md"] if patch.get("body_md") is not None else current["body_md"]
            new_status = patch.get("status", current["status"])
            if new_status not in ("active", "archived"):
                raise ValueError("Statut invalide.")

            if new_status == "archived" and current["status"] == "archived":
                cur.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 1)
                    FROM app.procedures_versions
                    WHERE procedure_id = %s
                    """,
                    (procedure_id,),
                )
                max_ver_row = cur.fetchone()
                current["version_number"] = max_ver_row[0] if max_ver_row else 1
                return current

            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0)
                FROM app.procedures_versions
                WHERE procedure_id = %s
                """,
                (procedure_id,),
            )
            max_ver = cur.fetchone()[0]
            next_ver = max_ver + 1

            cur.execute(
                """
                UPDATE app.procedures
                SET name = %s, description = %s, frontmatter_yaml = %s,
                    body_md = %s, status = %s, updated_at = now()
                WHERE id = %s
                RETURNING
                    id, project_id, name, description, frontmatter_yaml,
                    body_md, status, created_by, created_at, updated_at
                """,
                (new_name, new_desc, new_fm_yaml, new_body, new_status, procedure_id),
            )
            updated_row = cur.fetchone()
            updated_proc = _row_to_topic(updated_row, cols)

            cur.execute(
                """
                INSERT INTO app.procedures_versions
                    (procedure_id, project_id, name, description, frontmatter_yaml, body_md,
                     status, created_by, created_at, updated_at, version_number, changed_by,
                     changed_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz,
                     %s, %s, now())
                """,
                (
                    updated_proc["id"],
                    updated_proc["project_id"],
                    updated_proc["name"],
                    updated_proc["description"],
                    updated_proc["frontmatter_yaml"],
                    updated_proc["body_md"],
                    updated_proc["status"],
                    updated_proc["created_by"],
                    updated_proc["created_at"],
                    updated_proc["updated_at"],
                    next_ver,
                    changed_by,
                ),
            )

            action = (
                ACTION_PROCEDURE_ARCHIVED
                if new_status == "archived" and current["status"] != "archived"
                else ACTION_PROCEDURE_UPDATED
            )

            insert_audit_row(
                conn,
                identity=changed_by,
                action=action,
                provider_account="platform",
                connection_ref="",
                metadata={
                    "procedure_id": procedure_id,
                    "project_id": updated_proc["project_id"],
                    "name": updated_proc["name"],
                    "version_number": next_ver,
                },
            )
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateProcedureNameError(
            "Une procédure portant ce nom existe déjà dans ce périmètre."
        ) from exc

    updated_proc["version_number"] = next_ver
    return updated_proc


def archive_procedure(conn: Any, *, procedure_id: str, changed_by: str) -> dict[str, Any]:
    """Archive a procedure (sets status='archived' and appends version)."""
    existing = get_procedure(conn, procedure_id=procedure_id)
    if existing and existing.get("status") == "archived":
        return existing
    return update_procedure(
        conn, procedure_id=procedure_id, patch={"status": "archived"}, changed_by=changed_by
    )


def get_procedure(
    conn: Any, *, procedure_id: str, caller_project_id: str | None = None
) -> dict[str, Any] | None:
    """Fetch procedure by ID with version number, enforcing scope."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.id, p.project_id, p.name, p.description, p.frontmatter_yaml,
                p.body_md, p.status, p.created_by, p.created_at, p.updated_at,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.procedures p
            LEFT JOIN app.procedures_versions v ON p.id = v.procedure_id
            WHERE p.id = %s
            GROUP BY
                p.id, p.project_id, p.name, p.description, p.frontmatter_yaml,
                p.body_md, p.status, p.created_by, p.created_at, p.updated_at
            """,
            (procedure_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        cols = [desc[0] for desc in cur.description]
        proc = _row_to_topic(row, cols)

        if (
            caller_project_id is not None
            and proc["project_id"] is not None
            and proc["project_id"] != caller_project_id
        ):
            return None

        return proc


def list_procedures(
    conn: Any, *, project_id: str | None, include_archived: bool = False
) -> list[dict[str, Any]]:
    """List procedures visible to project_id (platform + project) with version numbers."""
    where_clauses = ["(p.project_id IS NULL OR p.project_id = %s)"]
    params: list[Any] = [project_id]

    if not include_archived:
        where_clauses.append("p.status = 'active'")

    where_sql = " WHERE " + " AND ".join(where_clauses)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                p.id, p.project_id, p.name, p.description, p.frontmatter_yaml,
                p.body_md, p.status, p.created_by, p.created_at, p.updated_at,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.procedures p
            LEFT JOIN app.procedures_versions v ON p.id = v.procedure_id
            {where_sql}
            GROUP BY
                p.id, p.project_id, p.name, p.description, p.frontmatter_yaml,
                p.body_md, p.status, p.created_by, p.created_at, p.updated_at
            ORDER BY p.created_at DESC
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_topic(row, cols) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Context Graph Edge Domain Logic (Story 11.4)
# ---------------------------------------------------------------------------

#: Valid node types per migration 031 CHECK constraint.
GRAPH_NODE_TYPES = frozenset({"topic", "procedure", "schema_doc"})


def _row_to_edge(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for col, val in zip(cols, row):
        if col == "created_at" and val is not None:
            record[col] = _format_datetime(val)
        else:
            record[col] = val
    return record


def list_graph_edges(
    conn: Any, *, project_id: str | None
) -> list[dict[str, Any]]:
    """List graph edges visible to project_id (platform + project).

    AD-5 scoping: returns edges where edge project_id IS NULL (platform)
    OR edge project_id = requested project_id.
    """
    where_clauses = ["(e.project_id IS NULL OR e.project_id = %s)"]
    params: list[Any] = [project_id]
    where_sql = " WHERE " + " AND ".join(where_clauses)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                e.id, e.from_id, e.from_type, e.to_id, e.to_type,
                e.edge_type, e.project_id, e.created_by, e.created_at
            FROM app.context_graph e
            {where_sql}
            ORDER BY e.created_at DESC
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_edge(row, cols) for row in cur.fetchall()]


def get_graph_edge(conn: Any, *, edge_id: str) -> dict[str, Any] | None:
    """Fetch a single graph edge by ID (unfiltered; caller enforces scope)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                e.id, e.from_id, e.from_type, e.to_id, e.to_type,
                e.edge_type, e.project_id, e.created_by, e.created_at
            FROM app.context_graph e
            WHERE e.id = %s
            """,
            (edge_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in cur.description]
        return _row_to_edge(row, cols)


def _node_exists_in_scope(
    conn: Any,
    *,
    node_id: str,
    node_type: str,
    project_id: str | None,
) -> bool:
    """Check that a referenced node (from/to) exists and is visible in scope.

    For topics and procedures: platform rows (project_id IS NULL) are always
    visible; project rows must match the requested project_id.
    For schema_doc: any existing schema_context row counts (AD-17 — schema
    docs are read-only but linkable).
    """
    with conn.cursor() as cur:
        if node_type == "topic":
            cur.execute(
                """
                SELECT 1 FROM app.context_topics
                WHERE id = %s
                  AND status = 'active'
                  AND (project_id IS NULL OR project_id = %s)
                """,
                (node_id, project_id),
            )
        elif node_type == "procedure":
            cur.execute(
                """
                SELECT 1 FROM app.procedures
                WHERE id = %s
                  AND status = 'active'
                  AND (project_id IS NULL OR project_id = %s)
                """,
                (node_id, project_id),
            )
        elif node_type == "schema_doc":
            # schema_context rows have a mandatory project_id (not nullable).
            # Edges TO a schema_doc are allowed even from platform scope;
            # the schema_doc must exist for the given project.
            cur.execute(
                """
                SELECT 1 FROM app.schema_context
                WHERE id = %s
                """,
                (node_id,),
            )
        else:
            return False
        return cur.fetchone() is not None


def create_graph_edge(
    conn: Any,
    *,
    project_id: str | None,
    from_id: str,
    from_type: str,
    to_id: str,
    to_type: str,
    edge_type: str,
    created_by: str,
) -> dict[str, Any]:
    """Create a graph edge, validating node types and node existence.

    AD-17: edges TO a schema_doc are allowed; writing schema_context docs is not.
    Validates from_type and to_type against the CHECK constraint enum before INSERT.
    Validates that from_id and to_id exist and are in scope.
    Raises ValueError on type/existence validation failures.
    """
    if from_type not in GRAPH_NODE_TYPES:
        raise ValueError(
            f"Type de nœud source invalide : '{from_type}'. "
            f"Valeurs autorisées : {sorted(GRAPH_NODE_TYPES)}."
        )
    if to_type not in GRAPH_NODE_TYPES:
        raise ValueError(
            f"Type de nœud cible invalide : '{to_type}'. "
            f"Valeurs autorisées : {sorted(GRAPH_NODE_TYPES)}."
        )
    if not isinstance(edge_type, str) or not edge_type.strip():
        raise ValueError("Le type de liaison (edge_type) ne peut pas être vide.")

    edge_type = edge_type.strip()

    if not _node_exists_in_scope(
        conn, node_id=from_id, node_type=from_type, project_id=project_id
    ):
        raise ValueError(
            f"Le nœud source '{from_id}' (type '{from_type}') est introuvable dans ce périmètre."
        )
    if not _node_exists_in_scope(
        conn, node_id=to_id, node_type=to_type, project_id=project_id
    ):
        raise ValueError(
            f"Le nœud cible '{to_id}' (type '{to_type}') est introuvable dans ce périmètre."
        )

    edge_id = f"edge_{ULID()}"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_graph
                (id, from_id, from_type, to_id, to_type, edge_type,
                 project_id, created_by, created_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, now())
            RETURNING
                id, from_id, from_type, to_id, to_type, edge_type,
                project_id, created_by, created_at
            """,
            (edge_id, from_id, from_type, to_id, to_type, edge_type, project_id, created_by),
        )
        cols = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        edge = _row_to_edge(row, cols)

        insert_audit_row(
            conn,
            identity=created_by,
            action=ACTION_GRAPH_EDGE_CREATED,
            provider_account="platform",
            connection_ref="",
            metadata={
                "edge_id": edge_id,
                "from_id": from_id,
                "from_type": from_type,
                "to_id": to_id,
                "to_type": to_type,
                "edge_type": edge_type,
                "project_id": project_id,
            },
        )

    return edge


def delete_graph_edge(
    conn: Any, *, edge_id: str, deleted_by: str
) -> bool:
    """Delete a graph edge by ID. Returns True if a row was deleted, False if not found."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.context_graph WHERE id = %s RETURNING id",
            (edge_id,),
        )
        deleted = cur.fetchone() is not None

    if deleted:
        insert_audit_row(
            conn,
            identity=deleted_by,
            action=ACTION_GRAPH_EDGE_DELETED,
            provider_account="platform",
            connection_ref="",
            metadata={"edge_id": edge_id},
        )

    return deleted
