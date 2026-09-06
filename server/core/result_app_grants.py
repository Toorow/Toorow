"""Story 50.6 -- issuing and revoking the scoped Result handle.

WHY THIS IS NOT IN `core.result_slices`. The reader must be provably incapable of
writing: AC6 property 4 and AC7 rest on four SOURCE-LEVEL assertions, one of which
is that `result_slices.py` contains no `INSERT`, `UPDATE` or `DELETE` at all. A
module that both issues grants and reads slices cannot satisfy that, and an
assertion weakened to "no write against the payload table" is an assertion a
future edit walks straight through. So the write lives here, alone, and the
reader stays inspectable by grep.

WHAT A GRANT IS. A row in `app.result_app_grants` (migration 161) that NARROWS a
read the caller is already authorized to perform. It is not a credential:
`handle_id` is `rh_<ULID>` and decodes to nothing, every scope field lives in the
row, and `core.result_slices` resolves the CALLER's project access before this
row is ever loaded. Nothing here can widen an access decision -- there is no
column that grants, only columns that restrict.

ASCII-only, English copy, lazy `core.*` imports (no cycle with `core.main`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: Absolute lifetime of a grant (D5). A Result is permanent evidence; a handle is
#: a transport convenience. Giving a transport artifact the lifetime of an audit
#: record inverts both, so the handle expires long before the Result does.
HANDLE_TTL_SECONDS = 28800  # 8 hours

#: Idle lifetime (D5). A host session that stops reading stops holding a grant.
HANDLE_IDLE_SECONDS = 1800  # 30 minutes

HANDLE_PREFIX = "rh_"


class GrantRefused(Exception):
    """A grant could not be issued, with the reason a caller may safely see."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_handle_id() -> str:
    """Mint `rh_<ULID>` -- opaque, random, carrying no claim.

    Same helper family the repository already uses for every other identifier
    (`core/account_topology.py`, `core/admin_api.py`). The prefix is a reading
    aid so a handle is recognizable in a log; it is not a namespace a caller can
    exploit, because the 26 characters after it are random.
    """
    from ulid import ULID  # noqa: PLC0415

    return f"{HANDLE_PREFIX}{ULID()}"


def allowed_columns_from_schema(result_schema: Any) -> list[str]:
    """Derive the allowlist from the FROZEN Result schema, at issue time.

    The allowlist is the set of column names the Result itself declares. It is
    computed once, from evidence, and stored -- never recomputed at read time,
    where a wider schema could quietly widen an old grant.
    """
    fields = []
    if isinstance(result_schema, dict):
        fields = result_schema.get("fields") or []
    names: list[str] = []
    seen: set[str] = set()
    for field in fields:
        name = field.get("name") if isinstance(field, dict) else None
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def issue_handle(
    conn,
    *,
    org_id: str,
    project_id: str,
    result_id: str,
    identity: str,
    content_hash: str,
    result_schema: Any,
) -> dict[str, Any]:
    """Issue one grant over one immutable Result and return its public shape.

    The caller has ALREADY passed the project access decision (the data tool
    guards before it composes anything). This function does not re-derive
    authorization and must never be called as if it did -- issuing is the last
    step of an authorized read, not a gate of its own.
    """
    from core.result_slices import MAX_SLICE_COLUMNS  # noqa: PLC0415

    columns = allowed_columns_from_schema(result_schema)
    if not columns:
        # A grant with an empty allowlist is not a narrower grant, it is a broken
        # one. Refuse at issue rather than let a read discover it (the database
        # CHECK says the same thing; this says it with a code a caller can read).
        raise GrantRefused(
            "result_schema_has_no_fields",
            "The Result schema declares no fields, so no column can be granted.",
        )
    if len(columns) > MAX_SLICE_COLUMNS:
        raise GrantRefused(
            "result_schema_too_wide",
            f"The Result schema exceeds the {MAX_SLICE_COLUMNS}-column app-read bound.",
        )
    issued_at = _now()
    expires_at = issued_at + timedelta(seconds=HANDLE_TTL_SECONDS)
    idle_cutoff = issued_at - timedelta(seconds=HANDLE_IDLE_SECONDS)
    lock_key = "\x1f".join((org_id, project_id, result_id, identity, content_hash))
    with conn.cursor() as cur:
        # There is deliberately no new uniqueness migration in Story 65.3. A
        # transaction-scoped advisory lock serializes the exact grant identity,
        # then a live exact grant is reused instead of growing one row per render.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT handle_id, expires_at
            FROM app.result_app_grants
            WHERE org_id = %s AND project_id = %s AND result_id = %s
              AND content_hash = %s AND issued_to_identity = %s
              AND allowed_columns = %s
              AND revoked_at IS NULL AND expires_at > %s
              AND COALESCE(last_read_at, issued_at) >= %s
            ORDER BY issued_at DESC
            LIMIT 1
            """,
            (
                org_id,
                project_id,
                result_id,
                content_hash,
                identity,
                columns,
                issued_at,
                idle_cutoff,
            ),
        )
        existing = cur.fetchone()
    if existing is not None:
        existing_expires_at = existing[1]
        if hasattr(existing_expires_at, "astimezone"):
            existing_expires_at = existing_expires_at.astimezone(timezone.utc)
        return {
            "handle_id": existing[0],
            "allowed_columns": columns,
            "expires_at": existing_expires_at.isoformat(),
            "content_hash": content_hash,
        }

    handle_id = new_handle_id()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, issued_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                handle_id,
                org_id,
                project_id,
                result_id,
                content_hash,
                identity,
                columns,
                issued_at,
                expires_at,
            ),
        )
    return {
        "handle_id": handle_id,
        "allowed_columns": columns,
        "expires_at": expires_at.isoformat(),
        "content_hash": content_hash,
    }


def touch_handle(conn, *, handle_id: str) -> bool:
    """Record a read against a live grant and return whether it remained live.

    The ONLY other legitimate mutation besides revocation, and the migration 161
    trigger allows exactly these two columns to move. It is deliberately not in
    `core.result_slices`: a reader that can write is a reader whose read cannot
    be proved side-effect free.
    """
    now = _now()
    idle_cutoff = now - timedelta(seconds=HANDLE_IDLE_SECONDS)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.result_app_grants SET last_read_at = %s
            WHERE handle_id = %s AND revoked_at IS NULL AND expires_at > %s
              AND COALESCE(last_read_at, issued_at) >= %s
            """,
            (now, handle_id, now, idle_cutoff),
        )
        return cur.rowcount == 1


def revoke_handle(conn, *, handle_id: str, project_id: str) -> bool:
    """Revoke a grant. Returns False when there was nothing to revoke.

    Scoped by `project_id` so a revocation cannot reach across Projects, and
    idempotent-by-refusal: migration 161 refuses to reinstate a revoked grant, so
    a second call changes nothing rather than moving the timestamp forward.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.result_app_grants SET revoked_at = %s
            WHERE handle_id = %s AND project_id = %s AND revoked_at IS NULL
            """,
            (_now(), handle_id, project_id),
        )
        return cur.rowcount > 0


def load_grant(conn, *, handle_id: str, project_id: str) -> dict[str, Any] | None:
    """Load one grant row, or None.

    ONE lookup, whatever the outcome. Absent, foreign, revoked and expired all
    execute exactly this statement and diverge only on the row it returns, so no
    branch does database work the others skip -- which is how the non-disclosure
    guarantee of AC6 property 3 is structural rather than promised. Note what is
    NOT claimed: constant time. Structural uniformity is what this delivers; a
    measured constant-time guarantee is not asserted, because nothing here
    measures one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT handle_id, org_id, project_id, result_id, content_hash,
                   issued_to_identity, allowed_columns, issued_at, last_read_at,
                   expires_at, revoked_at
            FROM app.result_app_grants
            WHERE handle_id = %s AND project_id = %s
            """,
            (handle_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "handle_id": row[0],
        "org_id": row[1],
        "project_id": row[2],
        "result_id": row[3],
        "content_hash": row[4],
        "issued_to_identity": row[5],
        "allowed_columns": list(row[6] or ()),
        "issued_at": row[7],
        "last_read_at": row[8],
        "expires_at": row[9],
        "revoked_at": row[10],
    }


def grant_is_live(grant: dict[str, Any], *, now: datetime | None = None) -> bool:
    """True only for a grant that is neither revoked, expired nor idle-expired.

    Expiry is an ADDITIONAL bound, never the primary defence: the primary defence
    is that `core.result_slices` re-resolves the caller's project access on every
    read, before this row is even loaded.
    """
    now = now or _now()
    if grant.get("revoked_at") is not None:
        return False
    expires_at = grant.get("expires_at")
    if expires_at is not None and now >= expires_at:
        return False
    last_read = grant.get("last_read_at") or grant.get("issued_at")
    if last_read is not None and now - last_read > timedelta(seconds=HANDLE_IDLE_SECONDS):
        return False
    return True
