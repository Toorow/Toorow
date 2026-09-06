"""The Dossier: an ordered composition of frozen Renders plus narrative blocks.

Story 73-1 (epic-73-dossier-partageable.md), the object half of the amendment
ratified 2026-09-01 in `docs/product-architecture/visualization-and-rendering.md`:
the deliverable of an analysis is a dossier -- several sections, several
figures, a narrative -- and nothing composed several Renders into one document.
This module owns exactly that object: immutable per version, pinned to the
EXACT Render identities it shows.

WHAT A DOSSIER DELIBERATELY DOES NOT DO. It never re-executes anything: a
render block pins a Render that already exists, frozen, in this project -- the
same rule the Chart Template materialisation follows. It composes no content of
its own: the narrative blocks are the person's words, stored verbatim. And it
resolves each pinned Render's provenance at READ time (result, visualization
spec version, runtime build), because that is what 73-2's share page and 73-3's
PDF must print -- storing a copy would be a second answer free to rot.

The Share half (73-2) and the PDF (73-3) build on these versions; nothing here
knows about bearers or stylesheets.
"""

from __future__ import annotations

import logging
from typing import Any

from ulid import ULID

from core.analyze_artifacts import (
    ArtifactNotFound,
    ArtifactRefused,
    Refusal,
    canonical_hash,
)

logger = logging.getLogger("uvicorn.error")

DOSSIER_CONTRACT_VERSION = "dossier.v1"

#: A dossier stays a document a person reads, not a dump: the caps are generous
#: for a real analysis and refuse the pathological payload by name.
MAX_BLOCKS = 64
MAX_NARRATIVE_CHARS = 8000

_BLOCK_KINDS = ("render", "narrative")

#: Who wrote a narrative block -- D3 of the amendment of 2026-09-02
#: (`visualization-and-rendering.md`, "the model composes the Dossier"). A
#: figure is governed; a narrative is generated or human; the page shows which.
#: A stored block without the field is read as `human`, because only the console
#: wrote blocks before that date.
NARRATIVE_AUTHORS = ("human", "model")
DEFAULT_NARRATIVE_AUTHOR = "human"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _validated_blocks(
    conn,
    *,
    org_id: str,
    project_id: str,
    blocks: Any,
    narrative_author: str = DEFAULT_NARRATIVE_AUTHOR,
) -> list[dict[str, Any]]:
    """The blocks as a version stores them, or the refusal that names the gap.

    Order is preserved -- the order IS the document. Every render block must pin
    a Render of THIS org and project (one query for all of them, so a version
    cannot half-validate), and at least one render block is required: a dossier
    with no figure is a note, and notes have their own surfaces.

    `narrative_author` is the word a narrative block carries when the block
    itself names none: the console passes nothing and gets `human`; the MCP door
    passes `model` and also stamps every block before calling, so a caller
    cannot claim the other word through that door.
    """
    if narrative_author not in NARRATIVE_AUTHORS:
        raise ArtifactRefused(
            "unknown_narrative_author",
            "a narrative is written by `human` or `model`",
            [Refusal("unknown_narrative_author", str(narrative_author), "narrative_author")],
        )
    if not isinstance(blocks, list) or not blocks:
        raise ArtifactRefused(
            "missing_blocks",
            "a Dossier is an ordered list of blocks",
            [Refusal("missing_blocks", "add at least one render block", "blocks")],
        )
    if len(blocks) > MAX_BLOCKS:
        raise ArtifactRefused(
            "too_many_blocks",
            f"a Dossier carries at most {MAX_BLOCKS} blocks",
            [Refusal("too_many_blocks", f"{len(blocks)} sent", "blocks")],
        )

    normalized: list[dict[str, Any]] = []
    render_ids: list[str] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("kind") not in _BLOCK_KINDS:
            raise ArtifactRefused(
                "unknown_block_kind",
                "a block is `render` or `narrative`",
                [Refusal("unknown_block_kind", str(block)[:80], f"blocks[{index}]")],
            )
        if block["kind"] == "render":
            render_id = _text(block.get("render_id"))
            if not render_id:
                raise ArtifactRefused(
                    "missing_render_id",
                    "a render block names the Render it shows",
                    [Refusal("missing_render_id", "render_id", f"blocks[{index}]")],
                )
            render_ids.append(render_id)
            normalized.append({"kind": "render", "render_id": render_id})
        else:
            text = _text(block.get("text"))
            if not text or len(text) > MAX_NARRATIVE_CHARS:
                raise ArtifactRefused(
                    "narrative_out_of_bounds",
                    f"a narrative block carries 1..{MAX_NARRATIVE_CHARS} characters",
                    [Refusal("narrative_out_of_bounds", str(len(text)), f"blocks[{index}]")],
                )
            author = block.get("authored_by") or narrative_author
            if author not in NARRATIVE_AUTHORS:
                raise ArtifactRefused(
                    "unknown_narrative_author",
                    "a narrative is written by `human` or `model`",
                    [Refusal("unknown_narrative_author", str(author), f"blocks[{index}]")],
                )
            normalized.append({"kind": "narrative", "text": text, "authored_by": author})

    if not render_ids:
        raise ArtifactRefused(
            "no_render_block",
            "a Dossier composes at least one Render",
            [Refusal("no_render_block", "add a render block", "blocks")],
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.renders WHERE id = ANY(%s) AND org_id = %s AND project_id = %s",
            (render_ids, org_id, project_id),
        )
        found = {str(row[0]) for row in cur.fetchall()}
    missing = sorted(set(render_ids) - found)
    if missing:
        raise ArtifactRefused(
            "render_not_found",
            "these blocks pin Renders this Project does not hold: " + ", ".join(missing),
            [Refusal("render_not_found", render_id, "blocks") for render_id in missing],
        )
    return normalized


def _append_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    dossier_id: str,
    label: str,
    description: str | None,
    blocks: list[dict[str, Any]],
    actor: str,
    version_number: int,
    predecessor: str | None,
) -> dict[str, Any]:
    document = {
        "contract_version": DOSSIER_CONTRACT_VERSION,
        "label": label,
        "description": description,
        "blocks": blocks,
    }
    content_hash = canonical_hash(document)
    version_id = f"dosv_{ULID()}"
    import json  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_dossier_versions
                (id, dossier_id, org_id, project_id, version_number, label, description,
                 blocks, content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING created_at
            """,
            (
                version_id,
                dossier_id,
                org_id,
                project_id,
                version_number,
                label,
                description,
                json.dumps(blocks),
                content_hash,
                predecessor,
                actor,
            ),
        )
        created_at = cur.fetchone()[0]
        cur.execute(
            "UPDATE app.analysis_dossiers "
            "SET current_version_id = %s, label = %s, description = %s, updated_at = NOW() "
            "WHERE id = %s AND org_id = %s AND project_id = %s",
            (version_id, label, description, dossier_id, org_id, project_id),
        )
    return {
        "dossier_id": dossier_id,
        "version_id": version_id,
        "version_number": version_number,
        "label": label,
        "description": description,
        "blocks": blocks,
        "content_hash": content_hash,
        "predecessor_version_id": predecessor,
        "created_at": created_at.isoformat() if created_at is not None else None,
    }


def create_dossier(
    conn,
    *,
    org_id: str,
    project_id: str,
    label: str,
    actor: str,
    blocks: Any,
    description: str | None = None,
    narrative_author: str = DEFAULT_NARRATIVE_AUTHOR,
) -> dict[str, Any]:
    """Create a Dossier head and its version 1, in the caller's transaction."""
    label = _text(label)
    if not label:
        raise ArtifactRefused(
            "missing_field",
            "a Dossier needs a label",
            [Refusal("missing_field", "label", "label")],
        )
    normalized = _validated_blocks(
        conn,
        org_id=org_id,
        project_id=project_id,
        blocks=blocks,
        narrative_author=narrative_author,
    )
    dossier_id = f"dos_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_dossiers
                (id, org_id, project_id, label, description, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (dossier_id, org_id, project_id, label, description, actor),
        )
    return _append_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        dossier_id=dossier_id,
        label=label,
        description=description,
        blocks=normalized,
        actor=actor,
        version_number=1,
        predecessor=None,
    )


def append_dossier_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    dossier_id: str,
    actor: str,
    blocks: Any,
    label: str | None = None,
    description: str | None = None,
    narrative_author: str = DEFAULT_NARRATIVE_AUTHOR,
) -> dict[str, Any]:
    """Succeed the current version -- a Dossier is never edited in place."""
    head = _head_row(conn, org_id=org_id, project_id=project_id, dossier_id=dossier_id)
    if head["archived_at"] is not None:
        raise ArtifactRefused(
            "dossier_archived",
            "an archived Dossier takes no new version",
            [Refusal("dossier_archived", dossier_id, "dossier_id")],
        )
    label = _text(label) or head["label"]
    if description is None:
        description = head["description"]
    normalized = _validated_blocks(
        conn,
        org_id=org_id,
        project_id=project_id,
        blocks=blocks,
        narrative_author=narrative_author,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 "
            "FROM app.analysis_dossier_versions WHERE dossier_id = %s",
            (dossier_id,),
        )
        version_number = int(cur.fetchone()[0])
    return _append_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        dossier_id=dossier_id,
        label=label,
        description=description,
        blocks=normalized,
        actor=actor,
        version_number=version_number,
        predecessor=head["current_version_id"],
    )


def _head_row(conn, *, org_id: str, project_id: str, dossier_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, label, description, current_version_id, archived_at,
                   created_by, created_at, updated_at
            FROM app.analysis_dossiers
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (dossier_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ArtifactNotFound(dossier_id)
    return {
        "id": row[0],
        "label": row[1],
        "description": row[2],
        "current_version_id": row[3],
        "archived_at": row[4],
        "created_by": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def list_dossiers(conn, *, org_id: str, project_id: str) -> list[dict[str, Any]]:
    """Every live Dossier head, newest movement first, with its version count."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.label, d.description, d.current_version_id, d.created_by,
                   d.created_at, d.updated_at,
                   v.version_number, v.blocks
            FROM app.analysis_dossiers d
            LEFT JOIN app.analysis_dossier_versions v ON v.id = d.current_version_id
            WHERE d.org_id = %s AND d.project_id = %s AND d.archived_at IS NULL
            ORDER BY d.updated_at DESC
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        blocks = row[8] or []
        out.append(
            {
                "id": row[0],
                "label": row[1],
                "description": row[2],
                "current_version_id": row[3],
                "created_by": row[4],
                "created_at": row[5].isoformat() if row[5] else None,
                "updated_at": row[6].isoformat() if row[6] else None,
                "current_version_number": row[7],
                "render_count": sum(1 for b in blocks if b.get("kind") == "render"),
                "narrative_count": sum(1 for b in blocks if b.get("kind") == "narrative"),
            }
        )
    return out


def get_dossier(conn, *, org_id: str, project_id: str, dossier_id: str) -> dict[str, Any]:
    """Head, every immutable version, and the current version RESOLVED.

    Resolution is what the share page (73-2) and the PDF (73-3) print: each
    render block joined with its Render's provenance -- the Result, the exact
    Visualization Spec version, the runtime build that drew it, and when. Read
    at answer time, never stored: a stored copy would be free to rot.
    """
    head = _head_row(conn, org_id=org_id, project_id=project_id, dossier_id=dossier_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version_number, label, description, blocks, content_hash,
                   predecessor_version_id, created_by, created_at
            FROM app.analysis_dossier_versions
            WHERE dossier_id = %s AND org_id = %s AND project_id = %s
            ORDER BY version_number DESC
            """,
            (dossier_id, org_id, project_id),
        )
        version_rows = cur.fetchall()
    versions = [
        {
            "id": row[0],
            "version_number": row[1],
            "label": row[2],
            "description": row[3],
            "blocks": row[4] or [],
            "content_hash": row[5],
            "predecessor_version_id": row[6],
            "created_by": row[7],
            "created_at": row[8].isoformat() if row[8] else None,
        }
        for row in version_rows
    ]
    current = next(
        (v for v in versions if v["id"] == head["current_version_id"]), None
    )
    resolved = (
        _resolve_blocks(conn, org_id=org_id, project_id=project_id, blocks=current["blocks"])
        if current
        else []
    )
    return {
        "id": head["id"],
        "label": head["label"],
        "description": head["description"],
        "current_version_id": head["current_version_id"],
        "archived_at": head["archived_at"].isoformat() if head["archived_at"] else None,
        "created_by": head["created_by"],
        "created_at": head["created_at"].isoformat() if head["created_at"] else None,
        "updated_at": head["updated_at"].isoformat() if head["updated_at"] else None,
        "versions": versions,
        "current_resolved_blocks": resolved,
    }


def _resolve_blocks(
    conn, *, org_id: str, project_id: str, blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    render_ids = [b["render_id"] for b in blocks if b.get("kind") == "render"]
    provenance: dict[str, dict[str, Any]] = {}
    if render_ids:
        with conn.cursor() as cur:
            # The walk is the RESULT's (D4 of 2026-09-02): read off
            # `app.query_results` through the Render's own result pin, never
            # re-declared by whoever composed the dossier. `ai_path` is the id,
            # or None when the Result recorded the absent literal.
            cur.execute(
                """
                SELECT r.id, r.result_id, r.visualization_spec_version_id, r.runtime_build_id,
                       r.renderer_build_id, r.created_at, q.ai_path_id
                FROM app.renders r
                LEFT JOIN app.query_results q
                       ON q.id = r.result_id AND q.org_id = r.org_id AND q.project_id = r.project_id
                WHERE r.id = ANY(%s) AND r.org_id = %s AND r.project_id = %s
                """,
                (render_ids, org_id, project_id),
            )
            for row in cur.fetchall():
                provenance[str(row[0])] = {
                    "result_id": row[1],
                    "visualization_spec_version_id": row[2],
                    "runtime_build_id": row[3],
                    "renderer_build_id": row[4],
                    "rendered_at": row[5].isoformat() if row[5] else None,
                    "ai_path": str(row[6]) if row[6] else None,
                }
    resolved: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("kind") == "render":
            entry = {"kind": "render", "render_id": block["render_id"]}
            pinned = provenance.get(block["render_id"])
            # A Render is append-only, so an unresolvable pin is a data fault
            # worth SAYING, never worth hiding behind an empty slot.
            entry["provenance"] = pinned
            entry["provenance_missing"] = pinned is None
            resolved.append(entry)
        else:
            entry = dict(block)
            # A block stored before 2026-09-02 carries no author; the console
            # was the only writer then, so the missing word is `human`.
            entry.setdefault("authored_by", DEFAULT_NARRATIVE_AUTHOR)
            resolved.append(entry)
    return resolved
