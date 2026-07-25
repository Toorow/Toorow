"""Managed-feed import ledger + generic writer seam (Story 12.8).

This module is the KEYSTONE that Stories 12.9 (CSV/Excel) and 12.10 (Google Sheets
sync) consume. It owns EXACTLY the managed-feed-specific state that the generic
12.5 candidate registry (``core.datastream_publication``) does NOT: the pre-write
IMMUTABLE import ledger, the dedicated project-scoped RAW landing allocation, the
unchanged-snapshot no-op, and the rejected-row storage + blocking threshold.

It is pure, source-agnostic orchestration (AD-2): the feed ``format``
(``csv`` / ``excel`` / ``google_sheets``) is an OPAQUE enum -- NO branch in this
module reads it to change behaviour; 12.9/12.10 supply already-parsed rows +
already-computed evidence and this module records/routes them generically. There is
NO import from ``server/modules/*`` and NO provider name.

AD-8: dbt is the ONLY writer of analytical marts. The managed-feed landing this
module allocates is a project-scoped RAW table in the ``org_<wslug>_raw`` namespace
(routed via ``core.warehouse_write.open_raw_writer``); it is NEVER a mart. The
ledger and rejected-rows tables are Postgres metadata only.

The lifecycle (one ``mfl_<ULID>`` ledger row per import ATTEMPT):

    open_import  -> ledger row 'opened' + isolated 042 candidate (BEFORE any row).
                    Idempotent: same key + same payload returns the existing result;
                    same key + DIFFERENT payload -> ImportPayloadConflict (409).
                    An unchanged content snapshot short-circuits to a no-op ledger
                    row (no candidate, freshness refreshed) -- unchanged_no_op().
    record_rows  -> after 12.9/12.10 write the raw landing rows, record the landing
                    relation + row_count + rejected rows; advance the ledger to
                    'written' and the candidate to 'validating'/'ready' via 12.5.
    evaluate_rejection_gate -> blocking threshold decision (project-scoped pref).

Publication itself stays 12.5's job: this module hands a 'ready' candidate to
``core.datastream_publication.run_dq_gates`` / ``commit_publication``. It records
the resulting 'published' outcome on the ledger for the freshness/download contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ulid import ULID

# psycopg is imported guardedly so the offline unit tests (which never touch
# Postgres) still import this module when psycopg is absent, and so the real
# unique-violation on the idempotency index maps to a deterministic conflict.
try:
    import psycopg
except ImportError:  # pragma: no cover - psycopg is a declared dependency.
    psycopg = None  # type: ignore[assignment]

if psycopg is not None:  # pragma: no branch - trivial binding.
    _UNIQUE_VIOLATION: type[BaseException] = psycopg.errors.UniqueViolation
else:  # pragma: no cover - psycopg is a declared dependency.

    class _NoMatch(Exception):
        pass

    _UNIQUE_VIOLATION = _NoMatch


# ---------------------------------------------------------------------------
# Closed vocabularies + governed defaults.
# ---------------------------------------------------------------------------

FORMAT_CSV = "csv"
FORMAT_EXCEL = "excel"
FORMAT_GOOGLE_SHEETS = "google_sheets"
VALID_FORMATS = frozenset({FORMAT_CSV, FORMAT_EXCEL, FORMAT_GOOGLE_SHEETS})

WRITE_MODE_REPLACE = "replace"
WRITE_MODE_APPEND = "append"
VALID_WRITE_MODES = frozenset({WRITE_MODE_REPLACE, WRITE_MODE_APPEND})

OUTCOME_OPENED = "opened"
OUTCOME_WRITTEN = "written"
OUTCOME_NOOP = "noop"
OUTCOME_REJECTED = "rejected"
OUTCOME_PUBLISHED = "published"
OUTCOME_FAILED = "failed"
TERMINAL_OUTCOMES = frozenset(
    {OUTCOME_NOOP, OUTCOME_REJECTED, OUTCOME_PUBLISHED, OUTCOME_FAILED}
)
# Outcomes a CALLER may set via ``mark_outcome``. ``noop`` is EXCLUDED (M1): it is
# an internal outcome minted only by ``_record_no_op`` (which never lands rows); a
# caller flipping a real written import to 'noop' would mislabel it as unchanged.
MARKABLE_TERMINAL_OUTCOMES = frozenset(
    {OUTCOME_REJECTED, OUTCOME_PUBLISHED, OUTCOME_FAILED}
)

# Documented default used ONLY when app.project_preferences supplies no override.
# Never a silent platform-wide hardcode: the gate result records threshold_source.
DEFAULT_MAX_REJECTED_ROW_PCT = 25.0

# Blocking rejection gate code (closed).
GATE_REJECTION_THRESHOLD_EXCEEDED = "rejection_threshold_exceeded"


class ManagedFeedError(Exception):
    """Base for managed-feed errors carrying a stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ImportPayloadConflict(ManagedFeedError):
    """The idempotency key was reused with a DIFFERENT payload (-> 409)."""

    def __init__(self) -> None:
        super().__init__("import_payload_conflict")


class LedgerNotFound(ManagedFeedError):
    """The requested ledger row does not exist in the given project scope."""

    def __init__(self) -> None:
        super().__init__("managed_feed_ledger_not_found")


class InvalidFeedFormat(ManagedFeedError):
    """An unknown feed format / write mode was supplied (-> 422)."""

    def __init__(self, detail: str) -> None:
        super().__init__("invalid_feed_format", detail)


class LedgerTerminal(ManagedFeedError):
    """An attempt to advance a ledger row that is already terminal (-> 409)."""

    def __init__(self, outcome: str) -> None:
        super().__init__("managed_feed_ledger_terminal", f"outcome={outcome}")


class RejectionThresholdExceeded(ManagedFeedError):
    """The blocking rejection threshold blocks publication (-> 422)."""

    def __init__(self, issue: dict[str, Any]) -> None:
        super().__init__(GATE_REJECTION_THRESHOLD_EXCEEDED)
        self.issue = issue


class ImportInProgress(ManagedFeedError):
    """Another non-terminal import execution is already active for this datastream.

    Surfaced (-> 409) when the 12.5 candidate registry rejects a second concurrent
    execution (``ConcurrentExecutionActive``). This is a DOCUMENTED part of the public
    contract 12.9/12.10 consume -- they must map it to a 409, not let it 500.
    """

    def __init__(self) -> None:
        super().__init__("managed_feed_import_in_progress")


# ---------------------------------------------------------------------------
# Pure helpers (no DB) -- exercised by the offline unit tests.
# ---------------------------------------------------------------------------


def _mint_ledger_id() -> str:
    return f"mfl_{ULID()}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_idempotency_key(idempotency_key: str) -> str:
    """SHA-256 hex of the idempotency key (matches the 64-hex CHECK constraint)."""
    return _sha256(idempotency_key)


def payload_fingerprint(payload: dict[str, Any]) -> str:
    """Deterministic fingerprint of an import's IDENTITY payload.

    Used to detect idempotency-key reuse with a DIFFERENT payload (-> 409). Keys are
    sorted so the fingerprint is stable across dict ordering. The caller decides
    what constitutes the identity (source metadata + plan/mapping versions + write
    mode) -- the content_hash of the SNAPSHOT is a separate axis (see
    ``build_import_identity``).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical)


def build_import_identity(
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
    feed_format: str,
    write_mode: str,
    source_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the canonical identity payload whose fingerprint the ledger stores.

    The fingerprint intentionally EXCLUDES the snapshot ``content_hash`` (a re-run of
    the SAME configuration over a NEW file is the SAME identity with a NEW content
    hash -- that is a legitimate re-import, not an idempotency conflict). It INCLUDES
    the source_metadata IDENTITY (which spreadsheet/tab/range or which upload slot),
    the plan + mapping versions, the format, and the write mode.
    """
    return {
        "datastream_id": datastream_id,
        "project_id": project_id,
        "plan_version_id": plan_version_id,
        "mapping_version_id": mapping_version_id,
        "feed_format": feed_format,
        "write_mode": write_mode,
        "source_metadata": source_metadata,
    }


def resolve_rejection_threshold(
    preferences: dict[str, Any] | None,
) -> tuple[float, str]:
    """Resolve the governed max-rejected-row-percent for a project.

    Reads ``max_rejected_row_pct`` from the project-scoped preferences dict; falls
    back to the DOCUMENTED default when unset, recording ``threshold_source`` so the
    fallback is never silent. Returns ``(max_rejected_row_pct, threshold_source)``.
    """
    prefs = preferences or {}
    value = prefs.get("max_rejected_row_pct")
    if value is None:
        return DEFAULT_MAX_REJECTED_ROW_PCT, "documented_default"
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_REJECTED_ROW_PCT, "documented_default"
    if pct < 0 or pct > 100:
        return DEFAULT_MAX_REJECTED_ROW_PCT, "documented_default"
    return pct, "project_preference"


def evaluate_rejection_gate(
    *,
    accepted_row_count: int,
    rejected_row_count: int,
    preferences: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Pure decision: does the rejection ratio BLOCK publication?

    Returns a blocking ``{code, detail, repair}`` issue when the rejected fraction
    (rejected / (rejected + accepted)) EXCEEDS the governed threshold, else ``None``
    (allowed rejected rows remain downloadable -- they do not block, they are
    surfaced). Fails CLOSED on nonsensical negative counts.
    """
    if accepted_row_count < 0 or rejected_row_count < 0:
        return {
            "code": GATE_REJECTION_THRESHOLD_EXCEEDED,
            "detail": "negative row counts are invalid",
            "repair": {"reparse_source": True},
        }
    total = accepted_row_count + rejected_row_count
    if total == 0 or rejected_row_count == 0:
        return None
    max_pct, threshold_source = resolve_rejection_threshold(preferences)
    rejected_pct = rejected_row_count / total * 100.0
    if rejected_pct > max_pct:
        return {
            "code": GATE_REJECTION_THRESHOLD_EXCEEDED,
            "detail": (
                f"{rejected_row_count}/{total} rows rejected "
                f"({rejected_pct:.2f}%) exceeds threshold {max_pct:.2f}%"
            ),
            "repair": {
                "fix_source_or_mapping": {
                    "accepted_row_count": accepted_row_count,
                    "rejected_row_count": rejected_row_count,
                    "rejected_pct": round(rejected_pct, 2),
                    "max_rejected_row_pct": max_pct,
                },
                "threshold_source": threshold_source,
            },
        }
    return None


def allocate_landing_relation(
    *,
    datastream_id: str,
    project_id: str,
    raw_schema: str | None,
) -> str:
    """Compose the project-scoped RAW landing relation name for a managed feed.

    The RAW landing is owned ONLY by toorow (AD-8: never a mart). The schema is
    resolved by ``core.warehouse_tenancy`` (via ``open_raw_writer``) -- it is NEVER
    composed here from user input. When the org schema is unresolvable (flag OFF or
    degraded), the relation lands on the DuckDB default schema ``main`` (the same
    degradation contract as ``open_raw_writer``). The table name is
    ``managed_feed_<datastream_id>`` -- one landing per datastream, replaced whole on
    each ``replace`` import (append is deferred to 12.9's keyed-dedup contract).

    Returns ``schema.table`` (both quoted-safe identifiers derived from trusted
    inputs: the schema from warehouse_tenancy, the datastream id from a minted ULID).
    """
    schema = raw_schema or "main"
    # datastream_id is a minted ds_<ULID>; normalise the prefix separator so the
    # table name is a valid identifier (ULID body is already [0-9A-Z]).
    safe_ds = datastream_id.replace("-", "_")
    return f"{schema}.managed_feed_{safe_ds}"


def assert_managed_landing(relation: str | None) -> str | None:
    """Guard a caller-supplied landing relation to the ``managed_feed_*`` shape (L4).

    ``allocate_landing_relation`` composes a safe relation, but ``open_import`` /
    ``record_rows`` also accept a caller-supplied ``landing_relation``. Enforce that any
    such value targets a toorow-owned managed-feed landing (table name begins with
    ``managed_feed_``) so a caller can never point the writer at a mart or an external
    table (AD-8). ``None`` is allowed (not yet allocated). Raises ``ManagedFeedError``.
    """
    if relation is None:
        return None
    table = relation.rsplit(".", 1)[-1]
    if not table.startswith("managed_feed_"):
        raise ManagedFeedError(
            "invalid_landing_relation",
            "landing_relation must target a managed_feed_* raw landing (never a mart)",
        )
    return relation


def open_managed_landing(duckdb_path: str, project_id: str | None):
    """Open the project-scoped RAW writer for a managed feed (AD-8 routing).

    Thin pass-through to ``core.warehouse_write.open_raw_writer`` so 12.9/12.10 get
    the SAME org-schema routing every connector uses -- the managed-feed WRITER never
    composes a schema itself and never touches a mart. Kept as a seam here so the
    landing allocation and the writer live in the managed-feed module (AD-2: the
    generic core flow consumes this, it does not branch on feed format).
    """
    from core.warehouse_write import open_raw_writer  # noqa: PLC0415

    return open_raw_writer(duckdb_path, project_id=project_id)


# ---------------------------------------------------------------------------
# DB helpers (private).
# ---------------------------------------------------------------------------


def _row_to_ledger(cur, row) -> dict[str, Any]:
    cols = [desc[0] for desc in cur.description]
    record: dict[str, Any] = {}
    for col, val in zip(cols, row):
        if col in (
            "snapshot_observed_at",
            "created_at",
            "updated_at",
        ) and val is not None:
            record[col] = val.isoformat()
        elif col == "source_metadata" and val is not None:
            record[col] = val if isinstance(val, dict) else json.loads(val)
        else:
            record[col] = val
    return record


_LEDGER_COLUMNS = """
    id, datastream_id, project_id, execution_id, plan_version_id,
    mapping_version_id, feed_format, write_mode, idempotency_key_hash,
    payload_fingerprint, content_hash, source_metadata, landing_relation,
    import_contract_id, row_count, rejected_row_count, outcome, superseded_ledger_id,
    error_code, error_detail, snapshot_observed_at, created_at, updated_at
"""


def _fetch_ledger(conn, ledger_id: str, project_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_LEDGER_COLUMNS}
            FROM app.managed_feed_import_ledger
            WHERE id = %s AND project_id = %s
            """,
            (ledger_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_ledger(cur, row)


def _fetch_ledger_by_key(
    conn, datastream_id: str, key_hash: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_LEDGER_COLUMNS}
            FROM app.managed_feed_import_ledger
            WHERE datastream_id = %s AND idempotency_key_hash = %s
            """,
            (datastream_id, key_hash),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_ledger(cur, row)


def _find_published_snapshot(
    conn, datastream_id: str, project_id: str, content_hash: str
) -> dict[str, Any] | None:
    """Return the most-recent PUBLISHED ledger row matching this exact snapshot.

    This is the unchanged-no-op oracle: if a published import of this content_hash
    already exists, a re-import of the same snapshot is a no-op (no new rows).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_LEDGER_COLUMNS}
            FROM app.managed_feed_import_ledger
            WHERE datastream_id = %s AND project_id = %s
              AND content_hash = %s AND outcome = 'published'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (datastream_id, project_id, content_hash),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_ledger(cur, row)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def open_import(
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
    feed_format: str,
    projection_plan: dict[str, Any],
    actor: str,
    idempotency_key: str,
    source_metadata: dict[str, Any],
    content_hash: str | None,
    conn,
    write_mode: str = WRITE_MODE_REPLACE,
    landing_relation: str | None = None,
    import_contract_id: str | None = None,
) -> dict[str, Any]:
    """Open an import: mint the IMMUTABLE ledger row + an isolated 042 candidate.

    Recorded BEFORE any raw row is written (NFR14): a crash leaves an unpublished
    candidate and an 'opened' ledger row -- the prior published data is untouched.

    Determinism / idempotency:
      * SAME idempotency_key + SAME identity payload -> returns the EXISTING ledger
        row + its result (no duplicate ledger row, no duplicate candidate).
      * SAME idempotency_key + DIFFERENT identity payload -> ``ImportPayloadConflict``
        (-> 409).
      * When ``content_hash`` matches a prior PUBLISHED snapshot -> an unchanged
        NO-OP ledger row ('noop') is created, NO candidate, freshness refreshed.

    The caller owns the transaction (does NOT commit here) so the ledger row, the
    candidate, and the audit row commit together. Returns::

        {
          "ledger": <ledger row dict>,
          "execution": <042 execution dict | None>,   # None on a no-op
          "no_op": bool,
          "replay": bool,                              # True when idempotent-replay
        }

    ``projection_plan`` MUST be executable (the 12.4 admission ticket). ``feed_format``
    is validated against the closed enum but is otherwise OPAQUE (AD-2: no branch).

    ``import_contract_id`` is an OPTIONAL, OPAQUE FK to the versioned parsing contract
    (``app.csv_excel_import_contracts``, migration 078) that 12.9 threads through so the
    import is traceable to the exact config version used. It is NULL for feeds that carry
    no parsing contract (e.g. Google Sheets sync) -- this module does NOT branch on it
    (AD-2); it merely persists the caller-supplied id on the ledger row.
    """
    from core.audit import ACTION_DATASTREAM_CREATED, insert_audit_row  # noqa: PLC0415
    from core.datastream_publication import (  # noqa: PLC0415
        ConcurrentExecutionActive,
        IdempotencyConflict,
        create_execution,
    )

    if feed_format not in VALID_FORMATS:
        raise InvalidFeedFormat(f"unknown feed_format {feed_format!r}")
    if write_mode not in VALID_WRITE_MODES:
        raise InvalidFeedFormat(f"unknown write_mode {write_mode!r}")
    if content_hash is not None and not _is_hex64(content_hash):
        raise InvalidFeedFormat("content_hash must be 64-hex or None")
    landing_relation = assert_managed_landing(landing_relation)  # L4: never a mart

    key_hash = hash_idempotency_key(idempotency_key)
    identity = build_import_identity(
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
        feed_format=feed_format,
        write_mode=write_mode,
        source_metadata=source_metadata,
    )
    fingerprint = payload_fingerprint(identity)

    # 1) Idempotent replay: same key + same payload returns the existing result.
    existing = _fetch_ledger_by_key(conn, datastream_id, key_hash)
    if existing is not None:
        if existing["payload_fingerprint"] != fingerprint:
            raise ImportPayloadConflict()
        execution = None
        if existing.get("execution_id"):
            from core.datastream_publication import get_execution  # noqa: PLC0415

            try:
                execution = get_execution(existing["execution_id"], project_id, conn)
            except Exception:  # noqa: BLE001 - defensive; execution may be gone.
                execution = None
        return {
            "ledger": existing,
            "execution": execution,
            "no_op": existing["outcome"] == OUTCOME_NOOP,
            "replay": True,
        }

    # 2) Unchanged-snapshot no-op: a prior PUBLISHED import of this exact content
    #    exists -> record an honest no-op ledger row (freshness refreshed), NO
    #    candidate, NO duplicated rows.
    if content_hash is not None:
        prior = _find_published_snapshot(
            conn, datastream_id, project_id, content_hash
        )
        if prior is not None:
            return _record_no_op(
                datastream_id=datastream_id,
                project_id=project_id,
                plan_version_id=plan_version_id,
                mapping_version_id=mapping_version_id,
                feed_format=feed_format,
                write_mode=write_mode,
                key_hash=key_hash,
                fingerprint=fingerprint,
                content_hash=content_hash,
                source_metadata=source_metadata,
                superseded_ledger_id=prior["id"],
                actor=actor,
                conn=conn,
            )

    # 3) A real import: create the isolated 042 candidate FIRST (the admission
    #    ticket + the target of the ledger's execution_id FK), then the ledger row.
    try:
        execution = create_execution(
            datastream_id,
            project_id,
            plan_version_id,
            mapping_version_id,
            projection_plan,
            actor,
            # The candidate's idempotency scope is the import's -- so a retry of the
            # whole import reuses the SAME candidate (create_execution is idempotent).
            idempotency_key,
            conn,
        )
    except IdempotencyConflict as exc:
        # The candidate registry saw the same key with a different candidate payload:
        # surface it as an import payload conflict (consistent 409 contract).
        raise ImportPayloadConflict() from exc
    except ConcurrentExecutionActive as exc:
        # A different import is mid-flight for this datastream (non-terminal execution):
        # surface it as a DOCUMENTED 409 (H1) so 12.9/12.10 map it, not a raw 500.
        raise ImportInProgress() from exc

    ledger_id = _mint_ledger_id()
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO app.managed_feed_import_ledger
                    (id, datastream_id, project_id, execution_id, plan_version_id,
                     mapping_version_id, feed_format, write_mode, idempotency_key_hash,
                     payload_fingerprint, content_hash, source_metadata,
                     landing_relation, import_contract_id, outcome, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                        'opened', %s)
                """,
                (
                    ledger_id,
                    datastream_id,
                    project_id,
                    execution["id"],
                    plan_version_id,
                    mapping_version_id,
                    feed_format,
                    write_mode,
                    key_hash,
                    fingerprint,
                    content_hash,
                    json.dumps(source_metadata),
                    landing_relation,
                    import_contract_id,
                    actor,
                ),
            )
        except _UNIQUE_VIOLATION as exc:
            # A concurrent open won the idempotency race. Re-read the existing row
            # and resolve it as a replay (same payload) or a conflict.
            other = _fetch_ledger_by_key(conn, datastream_id, key_hash)
            if other is not None and other["payload_fingerprint"] == fingerprint:
                return {
                    "ledger": other,
                    "execution": None,
                    "no_op": other["outcome"] == OUTCOME_NOOP,
                    "replay": True,
                }
            raise ImportPayloadConflict() from exc

    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=ACTION_DATASTREAM_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "managed_feed_ledger_id": ledger_id,
            "datastream_id": datastream_id,
            "project_id": project_id,
            "execution_id": execution["id"],
            "feed_format": feed_format,
            "write_mode": write_mode,
            "outcome": OUTCOME_OPENED,
        },
    )

    ledger = _fetch_ledger(conn, ledger_id, project_id)
    if ledger is None:  # pragma: no cover - the insert just succeeded.
        raise LedgerNotFound()
    return {"ledger": ledger, "execution": execution, "no_op": False, "replay": False}


def _record_no_op(
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
    feed_format: str,
    write_mode: str,
    key_hash: str,
    fingerprint: str,
    content_hash: str,
    source_metadata: dict[str, Any],
    superseded_ledger_id: str,
    actor: str,
    conn,
) -> dict[str, Any]:
    """Insert an honest unchanged-snapshot no-op ledger row (no candidate)."""
    from core.audit import ACTION_DATASTREAM_RUN, insert_audit_row  # noqa: PLC0415

    ledger_id = _mint_ledger_id()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.managed_feed_import_ledger
                (id, datastream_id, project_id, execution_id, plan_version_id,
                 mapping_version_id, feed_format, write_mode, idempotency_key_hash,
                 payload_fingerprint, content_hash, source_metadata, row_count,
                 outcome, superseded_ledger_id, created_by)
            VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 0,
                    'noop', %s, %s)
            """,
            (
                ledger_id,
                datastream_id,
                project_id,
                plan_version_id,
                mapping_version_id,
                feed_format,
                write_mode,
                key_hash,
                fingerprint,
                content_hash,
                json.dumps(source_metadata),
                superseded_ledger_id,
                actor,
            ),
        )
    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=ACTION_DATASTREAM_RUN,
        provider_account="",
        connection_ref="",
        metadata={
            "managed_feed_ledger_id": ledger_id,
            "datastream_id": datastream_id,
            "project_id": project_id,
            "outcome": OUTCOME_NOOP,
            "superseded_ledger_id": superseded_ledger_id,
            "content_hash": content_hash,
        },
    )
    ledger = _fetch_ledger(conn, ledger_id, project_id)
    if ledger is None:  # pragma: no cover - the insert just succeeded.
        raise LedgerNotFound()
    return {"ledger": ledger, "execution": None, "no_op": True, "replay": False}


def record_rows(
    *,
    ledger_id: str,
    project_id: str,
    landing_relation: str,
    accepted_row_count: int,
    content_hash: str,
    rejected_rows: list[dict[str, Any]] | None,
    actor: str,
    conn,
) -> dict[str, Any]:
    """Record the landing outcome of an OPENED import: rows written + rejections.

    Called by 12.9/12.10 AFTER they have written the raw landing rows via
    ``open_managed_landing``. This:
      * advances the ledger row 'opened' -> 'written' with the landing relation,
        row_count, content_hash (write-once), and rejected_row_count;
      * INSERTs the per-row rejection detail (append-only, traceable to field + rule
        + row + execution) in the SAME transaction so the count and detail agree.

    It does NOT advance the 042 candidate's state or run the DQ gates -- that is the
    caller's next step (advance the execution validating->ready via 12.5, then
    ``run_dq_gates`` / ``commit_publication``). Returns the updated ledger row.

    The caller owns the transaction. Fails closed (``LedgerTerminal``) if the ledger
    row is already terminal.
    """
    if accepted_row_count < 0:
        raise ManagedFeedError("invalid_row_count", "accepted_row_count must be >= 0")
    if not _is_hex64(content_hash):
        raise InvalidFeedFormat("content_hash must be 64-hex")
    landing_relation = assert_managed_landing(landing_relation)  # L4: never a mart

    ledger = _fetch_ledger(conn, ledger_id, project_id)
    if ledger is None:
        raise LedgerNotFound()
    if ledger["outcome"] in TERMINAL_OUTCOMES:
        raise LedgerTerminal(ledger["outcome"])
    # M2: fail CLOSED on a content_hash change instead of silently swallowing it via
    # COALESCE. If open_import already pinned a content_hash (the no-op oracle relies
    # on it) and record_rows now carries a DIFFERENT one, the file changed between
    # open and write -- surface it, do not discard it.
    prior_hash = ledger.get("content_hash")
    if prior_hash is not None and prior_hash != content_hash:
        raise ManagedFeedError(
            "content_hash_mismatch",
            "record_rows content_hash differs from the hash pinned at open_import",
        )

    rejected = rejected_rows or []
    rejected_count = len(rejected)
    execution_id = ledger.get("execution_id")
    datastream_id = ledger["datastream_id"]

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.managed_feed_import_ledger
            SET outcome = 'written',
                landing_relation = %s,
                row_count = %s,
                rejected_row_count = %s,
                content_hash = COALESCE(content_hash, %s),
                updated_at = NOW()
            WHERE id = %s AND project_id = %s AND outcome = 'opened'
            """,
            (
                landing_relation,
                accepted_row_count,
                rejected_count,
                content_hash,
                ledger_id,
                project_id,
            ),
        )
        if cur.rowcount != 1:  # pragma: no cover - guarded above; defensive race.
            raise LedgerTerminal(ledger["outcome"])

        for rej in rejected:
            cur.execute(
                """
                INSERT INTO app.managed_feed_rejected_rows
                    (ledger_id, execution_id, datastream_id, project_id, row_number,
                     field_name, rule, reason, rejected_value)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ledger_id,
                    execution_id,
                    datastream_id,
                    project_id,
                    # M3: NULL (no ordinal) stays distinct from a real row 0; only
                    # coerce to int when the caller actually supplied an ordinal.
                    (int(rej["row_number"]) if rej.get("row_number") is not None else None),
                    rej.get("field_name"),
                    str(rej.get("rule") or "unspecified"),
                    str(rej.get("reason") or ""),
                    _truncate(rej.get("rejected_value")),
                ),
            )

    updated = _fetch_ledger(conn, ledger_id, project_id)
    if updated is None:  # pragma: no cover - just updated.
        raise LedgerNotFound()
    return updated


def evaluate_rejection_gate_for_ledger(
    ledger_id: str,
    project_id: str,
    conn,
) -> dict[str, Any] | None:
    """Run the blocking rejection-threshold gate for a WRITTEN ledger row.

    Reads the ledger's row_count (accepted) + rejected_row_count + project-scoped
    ``max_rejected_row_pct`` preference. Returns a blocking ``{code, detail, repair}``
    issue when the threshold is breached, else ``None`` (allowed rejected rows do NOT
    block; they stay downloadable). Pure decision delegated to
    ``evaluate_rejection_gate``.
    """
    ledger = _fetch_ledger(conn, ledger_id, project_id)
    if ledger is None:
        raise LedgerNotFound()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT max_rejected_row_pct
            FROM app.project_preferences
            WHERE project_id = %s
            """,
            (project_id,),
        )
        pref_row = cur.fetchone()
    preferences = (
        {"max_rejected_row_pct": pref_row[0]} if pref_row is not None else {}
    )
    return evaluate_rejection_gate(
        accepted_row_count=int(ledger.get("row_count") or 0),
        rejected_row_count=int(ledger.get("rejected_row_count") or 0),
        preferences=preferences,
    )


def mark_outcome(
    ledger_id: str,
    project_id: str,
    outcome: str,
    actor: str,
    conn,
    *,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> dict[str, Any]:
    """Advance a WRITTEN/OPENED ledger row to a TERMINAL outcome.

    ``published`` is recorded after 12.5's ``commit_publication`` swaps the pointer
    (the ledger mirrors the execution's published state for the freshness/download
    contract). ``rejected`` records a blocking rejection-threshold breach.
    ``failed`` records an aborted import. Terminal outcomes are frozen by the DB
    trigger; re-marking an already-terminal row raises ``LedgerTerminal``.

    The caller owns the transaction.
    """
    from core.audit import (  # noqa: PLC0415
        ACTION_DATASTREAM_PUBLICATION_FAILED,
        ACTION_DATASTREAM_PUBLISHED,
        insert_audit_row,
    )

    if outcome not in MARKABLE_TERMINAL_OUTCOMES:
        # 'noop' is not caller-markable (M1); anything non-terminal is invalid.
        raise ManagedFeedError("invalid_outcome", f"{outcome} is not caller-markable")

    ledger = _fetch_ledger(conn, ledger_id, project_id)
    if ledger is None:
        raise LedgerNotFound()
    if ledger["outcome"] in TERMINAL_OUTCOMES:
        raise LedgerTerminal(ledger["outcome"])

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.managed_feed_import_ledger
            SET outcome = %s,
                error_code = %s,
                error_detail = %s,
                updated_at = NOW()
            WHERE id = %s AND project_id = %s
              AND outcome NOT IN ('noop', 'rejected', 'published', 'failed')
            """,
            (outcome, error_code, error_detail, ledger_id, project_id),
        )
        if cur.rowcount != 1:  # pragma: no cover - guarded; defensive race.
            raise LedgerTerminal(ledger["outcome"])

    action = (
        ACTION_DATASTREAM_PUBLISHED
        if outcome == OUTCOME_PUBLISHED
        else ACTION_DATASTREAM_PUBLICATION_FAILED
    )
    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=action,
        provider_account="",
        connection_ref="",
        metadata={
            "managed_feed_ledger_id": ledger_id,
            "datastream_id": ledger["datastream_id"],
            "project_id": project_id,
            "outcome": outcome,
            "error_code": error_code,
        },
    )
    updated = _fetch_ledger(conn, ledger_id, project_id)
    if updated is None:  # pragma: no cover - just updated.
        raise LedgerNotFound()
    return updated


def get_ledger(ledger_id: str, project_id: str, conn) -> dict[str, Any]:
    """Read a single ledger row, project-scoped (Viewer-accessible)."""
    ledger = _fetch_ledger(conn, ledger_id, project_id)
    if ledger is None:
        raise LedgerNotFound()
    return ledger


def list_ledger(
    datastream_id: str,
    project_id: str,
    conn,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read the import ledger newest-first, project-scoped (Viewer-accessible)."""
    limit = max(1, min(int(limit), 200))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_LEDGER_COLUMNS}
            FROM app.managed_feed_import_ledger
            WHERE datastream_id = %s AND project_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (datastream_id, project_id, limit),
        )
        rows = [_row_to_ledger(cur, row) for row in cur.fetchall()]
    return rows


def get_rejected_rows(
    ledger_id: str,
    project_id: str,
    conn,
    *,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Read the rejected rows for a ledger entry (downloadable, traceable).

    Each row carries field + rule + row_number + execution_id + ledger_id so the
    download is traceable to exactly WHY a row was rejected (Story 12.8 AC4). Bounded
    + paginated so a huge rejection set is downloadable in pages.
    """
    limit = max(1, min(int(limit), 10000))
    offset = max(0, int(offset))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ledger_id, execution_id, datastream_id, project_id,
                   row_number, field_name, rule, reason, rejected_value, created_at
            FROM app.managed_feed_rejected_rows
            WHERE ledger_id = %s AND project_id = %s
            ORDER BY row_number ASC, id ASC
            LIMIT %s OFFSET %s
            """,
            (ledger_id, project_id, limit, offset),
        )
        cols = [desc[0] for desc in cur.description]
        rows: list[dict[str, Any]] = []
        for row in cur.fetchall():
            record: dict[str, Any] = {}
            for col, val in zip(cols, row):
                if col == "created_at" and val is not None:
                    record[col] = val.isoformat()
                else:
                    record[col] = val
            rows.append(record)
    return rows


# ---------------------------------------------------------------------------
# Small pure utilities.
# ---------------------------------------------------------------------------


def _is_hex64(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _truncate(value: Any, *, limit: int = 512) -> str | None:
    """Coerce a rejected value to a bounded string (never leak an unbounded blob)."""
    if value is None:
        return None
    text = str(value)
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text
