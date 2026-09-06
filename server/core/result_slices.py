"""Story 50.6 -- bounded, allowlisted reads of a frozen Result, and the ONE `_meta` builder.

THE SENTENCE THIS MODULE HAS TO MAKE TRUE.
`docs/product-architecture/visualization-and-rendering.md` ("Small and large
Results"): *"Every read rechecks project/resource authorization and reads the
immutable Result store; the handle is neither a credential nor a warehouse query
escape hatch."*

A policy sentence cannot make that true. Four properties of this FILE do, and each
one is asserted by source inspection in `server/tests/core/test_result_slices.py`
so a future edit that removes it fails a test rather than a review:

  1. AUTHORIZATION FIRST, GRANT SECOND, AND INTERSECTED.
     Every entry point resolves the CALLER through
     `core.project_access.resolve_strict_resource_access(identity, conn,
     project_id=..., minimum_capability="view")` -- the same call
     `core.first_report_render_mcp._guard_project_view` makes, including its
     fail-closed `except`. Only after that decision is `allowed` with an `org_id`
     is the grant row loaded, and the grant then NARROWS what the decision already
     permitted. There is no path here in which a grant field widens a decision,
     and no path in which the grant is read first. A valid handle presented by a
     denied identity yields `not_found`.

  2. THE WAREHOUSE IS NOT IN THE IMPORT GRAPH.
     This module imports none of `core.warehouse`, `core.warehouse_write`,
     `core.cache_warehouse`, `core.bigquery_raw_writer`, `core.semantic_compiler`.
     `core.warehouse` is the single door to a warehouse on the whole Analyze path
     (`core/query_execution.py` imports it inside `run_execution`). A module that
     cannot import it cannot reach a warehouse, whatever its arguments say. It
     also neither imports nor redefines `build_sql` -- compiling a query is Story
     50.1's job and happens once, at execution; a slice read happens after the
     answer is frozen.

  3. ONE SELECT, NO WRITES.
     The only SQL in this file is a single fully parameterized
     `SELECT ... FROM app.query_result_payloads WHERE result_id = %s AND
     org_id = %s AND project_id = %s`. There is no `INSERT`, `UPDATE`, `DELETE`,
     no `%`-formatting and no f-string reaching a cursor. Issuing and revoking a
     grant live in `core.result_app_grants` precisely so that this remains
     checkable by grep. A slice read therefore cannot alter the evidence it reads
     even if a caller wanted it to (AC7).

  4. ONE `_meta` BUILDER.
     `build_result_meta` is the only function under `server/core` that writes the
     `_meta` key `toorow.result`. Everything it emits comes from three frozen
     sources -- `app.query_result_payloads.result_schema`, `.manifest`, and
     `.rows_chunk` projected through `allowed_columns` -- plus the opaque handle
     and two integers from `app.query_results`. It takes no connection reference,
     no token, no request context and no free-form dict, so there is no parameter
     through which a secret could arrive. The denylist tripwire in the integration
     suite exists to catch a mistake INSIDE this one builder; it is not the proof.
     The proof is that there is nowhere else to make the mistake.

A SLICE IS NOT A SEMANTIC CHANGE (AD-10). It returns rows already inside the
frozen Result. A request for a row the Result does not contain is a NEW execution,
not a wider slice, and this module has no way to perform one.

ASCII-only, English copy, lazy `core.*` imports (no cycle with `core.main`).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds (D3). Every one of them refuses; none of them silently reduces.
# ---------------------------------------------------------------------------

#: Hard ceiling on one slice read. A larger request is REFUSED, not clamped: a
#: caller that asked for 5000 and silently received 500 believes it has the
#: dataset.
MAX_SLICE_ROWS = 500

#: A slice may expose at most this many frozen Result columns.
MAX_SLICE_COLUMNS = 100

#: Results are frozen by `query_execution` at this storage bound. An offset
#: beyond it cannot name a row in a valid immutable Result.
MAX_SLICE_OFFSET = 1_000

#: Opaque cursors are small transport tokens, never arbitrary client storage.
MAX_CURSOR_BYTES = 512

#: Hard ceiling on one serialized slice response.
MAX_SLICE_BYTES = 262144  # 256 KiB

#: Above either of these, `_meta` carries NO rows -- a handle, the manifest and a
#: bounded initial projection instead.
SMALL_PROJECTION_ROWS = 200
SMALL_PROJECTION_BYTES = 65536  # 64 KiB

#: Ceiling on the whole `_meta["toorow.result"]` object.
MAX_RESULT_META_BYTES = 262144  # 256 KiB

#: Manifest reads cross the same app-only transport as row slices.
MAX_MANIFEST_BYTES = MAX_SLICE_BYTES

#: THE key. One builder writes it; nothing else under `server/core` may.
RESULT_META_KEY = "toorow.result"

_HANDLE_RE = re.compile(r"^rh_[0-9A-HJKMNP-TV-Z]{26}$")


class SliceRefused(Exception):
    """A bounded read was refused, with a code the caller may safely see.

    `not_found` is deliberately overloaded: absent, foreign, revoked, expired and
    denied all raise it from ONE call site with ONE message, so the envelope is
    byte-identical and existence is never disclosed (AC6 property 3, AC13).
    """

    def __init__(self, code: str, message: str, **detail: Any):
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.detail}


#: The single non-disclosure envelope. Every branch that must not distinguish
#: raises THIS, from one place, so there is no second message, second code or
#: early return for a reader to time or compare.
_NOT_FOUND_CODE = "not_found"
_NOT_FOUND_MESSAGE = "Not found"


def _not_found() -> SliceRefused:
    return SliceRefused(_NOT_FOUND_CODE, _NOT_FOUND_MESSAGE)


# ---------------------------------------------------------------------------
# Cursor (D4) -- opaque, and NOT a security control.
# ---------------------------------------------------------------------------


def encode_cursor(*, result_id: str, content_hash: str, offset: int) -> str:
    """Opaque base64url over the frozen Result identity plus an offset.

    Offsets are stable by construction here: the Result is immutable and its row
    order was frozen at terminalization, so offset paging needs no sort-key
    contract -- and this story has no authority to make an ordering promise
    (ordering belongs to the Query Spec). The `content_hash` inside is NOT a
    security control; it makes a cursor from a different Result fail loudly
    instead of paging the wrong data.
    """
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= MAX_SLICE_OFFSET
    ):
        raise SliceRefused("offset_out_of_bounds", "The offset is outside the frozen Result.")
    raw = json.dumps(
        {"result_id": result_id, "content_hash": content_hash, "offset": int(offset)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(encoded.encode("ascii")) > MAX_CURSOR_BYTES:
        raise SliceRefused("cursor_invalid", "The cursor is not readable.")
    return encoded


def decode_cursor(cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise SliceRefused("cursor_invalid", "The cursor is not readable.")
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise SliceRefused("cursor_invalid", "The cursor is not readable.") from exc
    offset = payload.get("offset") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= MAX_SLICE_OFFSET
    ):
        raise SliceRefused("cursor_invalid", "The cursor is not readable.")
    return payload


# ---------------------------------------------------------------------------
# Property 1 -- the caller first, the grant second, intersected.
# ---------------------------------------------------------------------------


def _require_project_view(conn, *, identity: str, project_id: str):
    """Resolve the CALLER's access, fail closed, disclose nothing on denial.

    This runs BEFORE any grant row is loaded and before any payload is touched,
    which is what makes the denial answer before any work is done -- the same
    discipline `core/query_specs_api.py` states for the REST surface, inherited
    structurally rather than restated.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        decision = resolve_strict_resource_access(
            identity, conn, project_id=project_id, minimum_capability="view"
        )
    except Exception as exc:  # noqa: BLE001 -- fail closed, never proceed unguarded.
        logger.error("result_slices: access guard failed: %s", type(exc).__name__)
        raise _not_found() from exc
    if not decision.allowed or not decision.org_id:
        raise _not_found()
    return decision


def resolve_handle(conn, *, handle_id: str, identity: str, project_id: str) -> dict[str, Any]:
    """Return a live grant for an authorized caller, or raise the ONE envelope.

    ORDER IS THE CONTRACT. The access decision is taken first; only then is the
    grant loaded, and the grant is INTERSECTED with the decision -- it may narrow
    the columns and it may expire, it can never authorize. A caller whose
    `resolve_strict_resource_access` returns `allowed=False` gets `not_found`
    even holding a perfectly valid handle.
    """
    from core.result_app_grants import grant_is_live, load_grant  # noqa: PLC0415

    decision = _require_project_view(conn, identity=identity, project_id=project_id)
    if not isinstance(handle_id, str) or _HANDLE_RE.fullmatch(handle_id) is None:
        raise _not_found()
    grant = load_grant(conn, handle_id=handle_id, project_id=project_id)
    # Absent, foreign, revoked, expired and org-mismatched converge here, on one
    # raise, with one message. There is no branch below that does work the others
    # skip and no second error code to compare.
    if (
        grant is None
        or grant["org_id"] != decision.org_id
        or grant["issued_to_identity"] != identity
        or not grant_is_live(grant)
    ):
        raise _not_found()
    grant["_decision_org_id"] = decision.org_id
    return grant


# ---------------------------------------------------------------------------
# Property 3 -- the one SELECT.
# ---------------------------------------------------------------------------


def _load_payload(conn, *, result_id: str, org_id: str, project_id: str) -> dict[str, Any]:
    """The ONLY SQL statement in this module. Fully parameterized, read-only."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content_hash, result_schema, manifest, rows_chunk
            FROM app.query_result_payloads
            WHERE result_id = %s AND org_id = %s AND project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise _not_found()
    return {
        "content_hash": row[0],
        "result_schema": row[1] or {},
        "manifest": row[2] or {},
        "rows_chunk": row[3] or [],
    }


def _verify_pin(grant: dict[str, Any], payload: dict[str, Any]) -> None:
    """Property 6 -- the content hash is pinned at issue and verified on EVERY read.

    Immutability makes a mismatch unreachable, which is the point: the assumption
    stops being assumed and starts being checked. `result_changed` is a distinct
    code on purpose -- it is not an existence signal (the caller already proved
    access to this Result), it is a corruption signal.
    """
    if payload["content_hash"] != grant["content_hash"]:
        raise SliceRefused(
            "result_changed",
            "The Result no longer matches the pinned content hash of this handle.",
        )


# ---------------------------------------------------------------------------
# Column allowlist -- narrow only, refuse loudly.
# ---------------------------------------------------------------------------


def _resolve_columns(requested: Any, allowed: list[str]) -> list[str]:
    """Intersect a request with the allowlist; refuse an unknown column.

    Silently dropping an unknown column would hand back a narrower table that
    looks complete -- the same failure the payload budget refuses rather than
    trims. The allowlist is never widened at read time; `allowed` came from the
    frozen schema at issue time.
    """
    if requested is None:
        resolved = list(allowed)
    elif isinstance(requested, str):
        resolved = [part.strip() for part in requested.split(",") if part.strip()]
    elif isinstance(requested, (list, tuple)):
        resolved = list(requested)
    else:
        raise SliceRefused("columns_invalid", "Columns must be a list of names.")
    if not resolved:
        resolved = list(allowed)
    if any(not isinstance(column, str) or not column for column in resolved):
        raise SliceRefused("columns_invalid", "Columns must be a list of names.")
    if len(resolved) > MAX_SLICE_COLUMNS:
        raise SliceRefused(
            "columns_over_bound",
            f"A slice may request at most {MAX_SLICE_COLUMNS} columns.",
            requested=len(resolved),
            bound=MAX_SLICE_COLUMNS,
        )
    if len(set(resolved)) != len(resolved):
        raise SliceRefused("columns_invalid", "Columns must not contain duplicates.")
    unknown = [c for c in resolved if c not in allowed]
    if unknown:
        raise SliceRefused(
            "column_not_in_result_schema",
            "A requested column is not part of this Result's frozen schema.",
            columns=sorted(unknown),
        )
    return resolved


def _schema_columns(result_schema: Any) -> list[str]:
    if not isinstance(result_schema, dict):
        return []
    fields = result_schema.get("fields")
    if not isinstance(fields, list):
        return []
    return [
        field["name"]
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("name"), str) and field["name"]
    ]


def _project_rows(
    rows: list[Any], columns: list[str], *, source_columns: list[str] | None = None
) -> list[dict[str, Any]]:
    source_columns = source_columns or columns
    source_indexes = {column: index for index, column in enumerate(source_columns)}
    if any(column not in source_indexes for column in columns):
        raise SliceRefused(
            "result_changed",
            "The Result schema no longer matches the pinned columns of this handle.",
        )
    projected: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            projected.append({c: row.get(c) for c in columns})
        elif isinstance(row, (list, tuple)):
            projected.append(
                {
                    column: (
                        row[source_indexes[column]]
                        if source_indexes[column] < len(row)
                        else None
                    )
                    for column in columns
                }
            )
        else:
            raise SliceRefused(
                "result_changed",
                "The frozen Result contains a row that no longer matches its schema.",
            )
    return projected


def _serialized_bytes(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")).encode("utf-8"))


def _safe_manifest(manifest: Any) -> dict[str, Any]:
    from core.render_app_payload import project_result_manifest  # noqa: PLC0415

    return project_result_manifest(manifest)


def _bounded_prefix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prefix: list[dict[str, Any]] = []
    for row in rows[:SMALL_PROJECTION_ROWS]:
        candidate = [*prefix, row]
        if _serialized_bytes(candidate) > SMALL_PROJECTION_BYTES:
            break
        prefix = candidate
    return prefix


# ---------------------------------------------------------------------------
# The two app-only reads.
# ---------------------------------------------------------------------------


def read_manifest(conn, *, handle_id: str, identity: str, project_id: str) -> dict[str, Any]:
    """Return the frozen schema and manifest for one Result, bounded by the grant."""
    grant = resolve_handle(conn, handle_id=handle_id, identity=identity, project_id=project_id)
    payload = _load_payload(
        conn,
        result_id=grant["result_id"],
        org_id=grant["org_id"],
        project_id=grant["project_id"],
    )
    _verify_pin(grant, payload)
    response = {
        "result_id": grant["result_id"],
        "content_hash": payload["content_hash"],
        "schema": payload["result_schema"],
        "manifest": _safe_manifest(payload["manifest"]),
        "allowed_columns": grant["allowed_columns"],
        "row_count": len(payload["rows_chunk"]),
        "expires_at": grant["expires_at"].isoformat() if grant.get("expires_at") else None,
    }
    measured = _serialized_bytes(response)
    if measured > MAX_MANIFEST_BYTES:
        raise SliceRefused(
            "manifest_over_byte_budget",
            f"The manifest is {measured} bytes, over the {MAX_MANIFEST_BYTES} byte budget.",
            measured=measured,
            budget=MAX_MANIFEST_BYTES,
        )
    return response


def read_slice(
    conn,
    *,
    handle_id: str,
    identity: str,
    project_id: str,
    columns: Any = None,
    cursor: str | None = None,
    offset: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return one bounded page of a frozen Result. Bounded on EVERY axis (AC7).

    `limit` is refused above `MAX_SLICE_ROWS` rather than clamped; the serialized
    page is refused above `MAX_SLICE_BYTES`; `cursor` and `offset` are mutually
    exclusive; an offset past the end returns an EMPTY page with `has_more=false`
    rather than an error, because an error there would leak the true row count to
    a caller probing for it.
    """
    if cursor is not None and offset is not None:
        raise SliceRefused(
            "cursor_and_offset_both_supplied",
            "Provide either a cursor or an offset, never both.",
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise SliceRefused("limit_invalid", "The limit must be an integer.")
    if limit < 1 or limit > MAX_SLICE_ROWS:
        raise SliceRefused(
            "limit_over_bound",
            f"The limit must be between 1 and {MAX_SLICE_ROWS}.",
            requested=limit,
            bound=MAX_SLICE_ROWS,
        )

    grant = resolve_handle(conn, handle_id=handle_id, identity=identity, project_id=project_id)
    payload = _load_payload(
        conn,
        result_id=grant["result_id"],
        org_id=grant["org_id"],
        project_id=grant["project_id"],
    )
    _verify_pin(grant, payload)

    if cursor is not None:
        decoded = decode_cursor(cursor)
        if decoded.get("result_id") != grant["result_id"] or decoded.get(
            "content_hash"
        ) != payload["content_hash"]:
            # Loud, not silent: a cursor minted against another Result must never
            # page this one.
            raise SliceRefused(
                "cursor_result_mismatch",
                "This cursor was issued for a different Result.",
            )
        start = decoded["offset"]
    else:
        if offset is None:
            start = 0
        elif isinstance(offset, bool) or not isinstance(offset, int):
            raise SliceRefused("offset_out_of_bounds", "The offset must be an integer.")
        else:
            start = offset
    if start < 0 or start > MAX_SLICE_OFFSET:
        raise SliceRefused("offset_out_of_bounds", "The offset is outside the frozen Result.")

    rows = payload["rows_chunk"]
    total = len(rows)
    resolved_columns = _resolve_columns(columns, grant["allowed_columns"])
    page = _project_rows(
        rows[start : start + limit],
        resolved_columns,
        source_columns=_schema_columns(payload["result_schema"]),
    )
    next_offset = start + len(page)
    has_more = next_offset < total
    response = {
        "result_id": grant["result_id"],
        "content_hash": payload["content_hash"],
        "columns": resolved_columns,
        "rows": page,
        "offset": start,
        "returned_rows": len(page),
        "total_rows": total,
        "has_more": has_more,
        "next_cursor": (
            encode_cursor(
                result_id=grant["result_id"],
                content_hash=payload["content_hash"],
                offset=next_offset,
            )
            if has_more
            else None
        ),
    }
    measured = _serialized_bytes(response)
    if measured > MAX_SLICE_BYTES:
        raise SliceRefused(
            "slice_over_byte_budget",
            f"The slice is {measured} bytes, over the {MAX_SLICE_BYTES} byte budget.",
            measured=measured,
            budget=MAX_SLICE_BYTES,
        )
    return response


# ---------------------------------------------------------------------------
# Property 4 -- the one `_meta` builder.
# ---------------------------------------------------------------------------


def build_result_meta(
    *,
    result_id: str,
    content_hash: str,
    result_schema: Any,
    manifest: Any,
    rows_chunk: list[Any],
    allowed_columns: list[str],
    row_count: int,
    truncated: bool,
    result_handle: str | None,
    ai_path_walk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build `_meta["toorow.result"]` -- the ONLY place that key is written.

    Note the signature: no connection, no token, no request, no `**kwargs`, no
    free-form dict. Every value that can leave this function came in through a
    named parameter carrying frozen Result evidence, an opaque handle or an
    integer. There is no parameter through which a credential, a connection
    string, a warehouse relation name or an internal person identifier could
    arrive, which is why "`_meta` hides no secret" is a property of the call
    signature rather than a promise in a comment.

    Small vs large is decided here, once, on the two declared thresholds:
    a projection is SMALL when it has at most `SMALL_PROJECTION_ROWS` rows AND
    serializes to at most `SMALL_PROJECTION_BYTES`. A small projection carries its
    rows inline plus the frozen schema. A large Result carries NO rows: the
    handle, the manifest and an initial bounded projection of at most
    `SMALL_PROJECTION_ROWS` rows.

    `ai_path_walk` is the Result's AI Path projected for the app channel (the
    owner's wire shape, `core.ai_paths.wire_step_projection`), or an honest
    degradation stub naming why it is not. It travels here -- never in
    `structuredContent` -- because a walk with its judged branches does not fit
    the 4 KiB model channel, and the widget is its only reader.
    """
    projected = _project_rows(
        list(rows_chunk or []),
        list(allowed_columns or []),
        source_columns=_schema_columns(result_schema),
    )
    inline_bytes = _serialized_bytes(projected)
    is_small = len(projected) <= SMALL_PROJECTION_ROWS and inline_bytes <= SMALL_PROJECTION_BYTES

    meta: dict[str, Any] = {
        "result_id": result_id,
        "content_hash": content_hash,
        "schema": result_schema if isinstance(result_schema, dict) else {},
        "allowed_columns": list(allowed_columns or []),
        "row_count": int(row_count),
        "truncated": bool(truncated),
        "projection_size": "small" if is_small else "large",
    }
    if is_small:
        meta["rows"] = projected
        meta["result_handle"] = result_handle
        meta["next_cursor"] = None
    else:
        if not isinstance(result_handle, str) or _HANDLE_RE.fullmatch(result_handle) is None:
            raise SliceRefused(
                "large_result_requires_handle",
                "This large Result has no live app-only read handle.",
            )
        initial = _bounded_prefix(projected)
        meta["result_handle"] = result_handle
        meta["manifest"] = _safe_manifest(manifest)
        meta["initial_projection"] = initial
        meta["next_cursor"] = (
            encode_cursor(
                result_id=result_id, content_hash=content_hash, offset=len(initial)
            )
            if len(projected) > len(initial)
            else None
        )
    if ai_path_walk is not None:
        # Inside the size check below, like everything else: the app channel is
        # bounded, and a walk that would break it degrades at the CALLER (which
        # knows the identity it can still state), not by omitting the check.
        meta["ai_path_walk"] = ai_path_walk
    return validate_result_meta({RESULT_META_KEY: meta})


def validate_result_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Validate the complete ToolResult metadata after every additive finalizer."""
    measured = _serialized_bytes(meta)
    if measured > MAX_RESULT_META_BYTES:
        # Even the app channel is bounded. `_meta` is not model-visible, but it is
        # not a dumping ground either -- it still crosses one host message.
        raise SliceRefused(
            "result_meta_over_byte_budget",
            f"Result _meta is {measured} bytes, over the {MAX_RESULT_META_BYTES} byte budget.",
            measured=measured,
            budget=MAX_RESULT_META_BYTES,
        )
    return meta
