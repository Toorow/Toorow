"""Read-only external BigQuery registration (Story 12.7).

Register an EXISTING BigQuery table/view as a read-only Datastream WITHOUT taking
ownership -- toorow never becomes a second writer of the external object. This
module owns EXACTLY the external-BQ-specific concerns; the generic publication
path (Story 12.5) is reused unchanged and consumed generically (AD-2): there is NO
external-BQ branch in ``server/core``'s generic flow -- callers dispatch to this
module by ``source_kind == 'external_bq'`` and everything downstream (12.3 mapping,
12.4 projection, 12.5 candidate registry + publication) is source-agnostic.

The five concerns, each fail-closed and honest (AD-9 -- NULL is never coerced to 0):

  * NO-WRITE GUARANTEE (proven by construction + test). Every SQL this module can
    emit against the external object is gated through ``assert_read_only_sql`` which
    rejects ANY write/DDL/DML verb (INSERT/UPDATE/DELETE/MERGE/TRUNCATE/DROP/ALTER/
    CREATE/REPLACE/GRANT/REVOKE/LOAD/COPY/EXPORT/CALL). The probe emits ONLY the
    read-only introspection statements enumerated in ``READ_ONLY_PROBE_STATEMENTS``.
    A test asserts the whole module's emitted-SQL surface contains no write verb.

  * ACCESS / SCHEMA / LOCATION / BOUNDED SAMPLE probe. ``build_probe_plan`` returns
    the read-only statements a live probe would run (scoped read permission check,
    schema fetch, location/region fetch, bounded ``SELECT ... LIMIT`` sample). The
    LIVE execution against BigQuery is Phase B (no live BQ credentials exist); this
    module returns the PLAN + the pure verdict/evidence logic, and a caller wires
    the live client in Phase B.

  * VIRTUAL PULL COMMIT. ``build_virtual_pull_commit`` turns an observation
    (watermark + content hash + schema hash + row count + location) into a provenance
    bundle EQUIVALENT to a real connector pull (a synthetic ``pull_id``, the object
    coordinates, the evidence hashes). ``is_unchanged_noop`` makes a repeated
    observation of identical content an auditable no-op (no candidate minted).

  * SAFE-FAIL MATRIX. ``classify_observation`` maps absent / empty / moved /
    permission-revoked / region-incompatible / changed-after-preview to a specific
    ``verdict`` + repair action. NONE of these replace a published version -- the
    caller reads the verdict and, on any non-ok/non-unchanged verdict, leaves the
    prior published pointer intact.

  * SCHEDULER GUARD. ``is_external_bq_datastream`` / ``exclude_external_bq_dispatch``
    are the EXPLICIT invariant the scheduler consumes so it never dispatches a
    provider pull for an external_bq datastream (there is no provider to pull).

Reuse (Stories 12.3 / 12.4 / 12.5):
  * 12.3 mapping + 12.4 projection: consumed unchanged -- an external-BQ candidate is
    classified, mapped, and projected through the SAME contracts. This module does
    NOT re-implement them; ``mint_observation`` records the observation and hands the
    (plan_version_id, mapping_version_id, projection_plan) tuple straight to 12.5's
    ``create_execution`` (via the integration hook the caller supplies).
  * 12.5 candidate registry: ``mint_observation`` calls ``create_execution`` /
    ``advance_state`` and returns the seam so the caller can run 12.5 DQ gates +
    ``commit_publication``. This module never edits datastream_publication.py.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ulid import ULID

# ---------------------------------------------------------------------------
# NO-WRITE GUARANTEE -- the structural proof that this module cannot mutate the
# external object. Every SQL string this module emits is checked against this
# allow/deny list. The test suite asserts the whole emitted surface is read-only.
# ---------------------------------------------------------------------------

# Any of these tokens as a leading statement verb (or anywhere as a standalone
# word) marks a write/DDL/DML/permission-changing statement -> rejected. Kept
# deliberately broad (fail closed): an unknown verb that is not a proven read is
# also rejected by ``assert_read_only_sql``'s allow-list on the FIRST keyword.
WRITE_VERBS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "TRUNCATE",
        "DROP",
        "ALTER",
        "CREATE",
        "REPLACE",
        "GRANT",
        "REVOKE",
        "LOAD",
        "COPY",
        "EXPORT",
        "CALL",
        "UPSERT",
        "RENAME",
        "SET",
    }
)

# The ONLY leading verbs a read-only external probe may use. A statement whose
# first keyword is not here is rejected (fail closed) even if it contains no
# WRITE_VERB -- an allow-list is stronger than a deny-list.
READ_ONLY_LEADING_VERBS: frozenset[str] = frozenset({"SELECT", "WITH"})


class ExternalBQWriteAttempt(Exception):
    """A SQL statement that is not provably read-only was emitted (fail closed)."""

    def __init__(self, verb: str, statement: str) -> None:
        super().__init__(f"non_read_only_sql: leading verb {verb!r}")
        self.verb = verb
        # Do NOT store the full statement in the message (it may echo object names);
        # keep it on the attribute for a caller/log that wants it.
        self.statement = statement


_LEADING_WORD = re.compile(r"[A-Za-z_]+")
_WORD = re.compile(r"[A-Za-z_]+")


def _first_keyword(statement: str) -> str:
    """Return the first SQL keyword of a statement, upper-cased (``""`` if none)."""
    # Strip leading line comments and whitespace.
    lines = []
    for raw in statement.splitlines():
        stripped = raw.strip()
        if stripped.startswith("--") or not stripped:
            continue
        lines.append(stripped)
    body = " ".join(lines).lstrip("(").strip()
    match = _LEADING_WORD.match(body)
    return match.group(0).upper() if match else ""


def assert_read_only_sql(statement: str) -> str:
    """Assert *statement* is provably read-only; raise ``ExternalBQWriteAttempt`` else.

    Two-layer fail-closed check:
      1. The FIRST keyword must be in ``READ_ONLY_LEADING_VERBS`` (allow-list).
      2. No standalone ``WRITE_VERBS`` token may appear anywhere (deny-list) --
         catches a write hidden after a CTE (``WITH x AS (...) DELETE ...`` is not
         valid SQL but the deny-list is belt-and-braces).

    Returns the statement unchanged on success so it can be used inline:
    ``cur.execute(assert_read_only_sql(sql))``.
    """
    leading = _first_keyword(statement)
    if leading not in READ_ONLY_LEADING_VERBS:
        raise ExternalBQWriteAttempt(leading or "<empty>", statement)
    tokens = {t.upper() for t in _WORD.findall(statement)}
    hit = tokens & WRITE_VERBS
    if hit:
        raise ExternalBQWriteAttempt(sorted(hit)[0], statement)
    return statement


# ---------------------------------------------------------------------------
# Safe-fail matrix (closed set of verdicts + repair actions).
# ---------------------------------------------------------------------------

VERDICT_OK = "ok"
VERDICT_UNCHANGED_NOOP = "unchanged_noop"
VERDICT_ABSENT = "absent"
VERDICT_EMPTY = "empty"
VERDICT_MOVED = "moved"
VERDICT_PERMISSION_REVOKED = "permission_revoked"
VERDICT_REGION_INCOMPATIBLE = "region_incompatible"
VERDICT_CHANGED_AFTER_PREVIEW = "changed_after_preview"

# Verdicts that BLOCK publication (the published pointer is NOT replaced). ok and
# unchanged_noop are the two non-blocking outcomes; unchanged_noop mints nothing.
BLOCKING_VERDICTS: frozenset[str] = frozenset(
    {
        VERDICT_ABSENT,
        VERDICT_EMPTY,
        VERDICT_MOVED,
        VERDICT_PERMISSION_REVOKED,
        VERDICT_REGION_INCOMPATIBLE,
        VERDICT_CHANGED_AFTER_PREVIEW,
    }
)

# Repair action per non-ok verdict (code + operator-facing next step). Read by the
# caller/UI; the verdict itself is the machine contract.
_REPAIR_BY_VERDICT: dict[str, dict[str, Any]] = {
    VERDICT_ABSENT: {
        "code": "external_object_absent",
        "action": "verify_object_exists_or_reselect",
    },
    VERDICT_EMPTY: {
        "code": "external_object_empty",
        "action": "wait_for_upstream_data_or_confirm_empty",
    },
    VERDICT_MOVED: {
        "code": "external_object_moved",
        "action": "reselect_object_coordinates",
    },
    VERDICT_PERMISSION_REVOKED: {
        "code": "external_read_permission_revoked",
        "action": "regrant_read_access_to_reader_identity",
    },
    VERDICT_REGION_INCOMPATIBLE: {
        "code": "external_region_incompatible",
        "action": "align_object_location_with_project_region",
    },
    VERDICT_CHANGED_AFTER_PREVIEW: {
        "code": "external_object_changed_after_preview",
        "action": "re_preview_before_activation",
    },
}


class ObservationInputError(ValueError):
    """The observation input is structurally invalid (safe to reject as a draft)."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def object_ref(external_object: dict[str, Any]) -> str:
    """Return the fully-qualified ``project.dataset.object`` reference string.

    Pure -- validates the required coordinates. Never interpolates into SQL (the
    probe plan carries the reference as a parameter, not a formatted identifier).
    """
    try:
        project = str(external_object["project"]).strip()
        dataset = str(external_object["dataset"]).strip()
        obj = str(external_object["object"]).strip()
    except (KeyError, TypeError) as exc:
        raise ObservationInputError("external_object requires project/dataset/object") from exc
    if not project or not dataset or not obj:
        raise ObservationInputError("external_object coordinates must be non-empty")
    return f"{project}.{dataset}.{obj}"


# ---------------------------------------------------------------------------
# Read-only probe plan. The LIVE execution is Phase B (no BQ credentials exist);
# this returns the read-only statements a probe would run, each proven read-only
# by construction, plus the parameters (object coordinates carried OUT of the SQL).
# ---------------------------------------------------------------------------


def build_probe_plan(
    external_object: dict[str, Any],
    *,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Return the read-only probe plan for an external object (no live call).

    The plan is four read-only steps:
      1. ``permission`` -- a scoped read permission + existence check (a bounded
         ``SELECT`` against the object; a permission error surfaces as
         ``permission_revoked``, a not-found as ``absent``/``moved``).
      2. ``schema``     -- INFORMATION_SCHEMA column fetch (read-only).
      3. ``location``   -- INFORMATION_SCHEMA / region fetch (read-only).
      4. ``sample``     -- a bounded ``SELECT * ... LIMIT n`` for profiling (12.3).

    EVERY statement is passed through ``assert_read_only_sql`` before it is placed
    in the plan, so a plan can NEVER carry a write. The object reference is carried
    as a PARAMETER (``@ref``-style placeholder) -- the SQL templates below use only
    INFORMATION_SCHEMA table names and a parameterized dataset/object filter, never
    a formatted external identifier (defence in depth vs. injection).

    NOTE (Phase B): BigQuery does not parameterize table identifiers in a bare
    ``SELECT * FROM project.dataset.object``; a live probe binds the object via the
    BigQuery client's fully-qualified TableReference (client.get_table / client.
    list_rows), NOT string interpolation. The ``sample`` template here is the
    read-only SHAPE; the live Phase-B probe uses the typed client API for the
    identifier and only the LIMIT is a literal.
    """
    ref = object_ref(external_object)
    dataset = str(external_object["dataset"]).strip()
    obj = str(external_object["object"]).strip()
    limit = max(1, min(int(sample_limit), 1000))

    # INFORMATION_SCHEMA reads: object/dataset carried as bind parameters, never
    # formatted into the SQL. These are proven read-only (leading SELECT).
    schema_sql = assert_read_only_sql(
        "SELECT column_name, data_type, is_nullable "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE table_schema = @dataset AND table_name = @object "
        "ORDER BY ordinal_position"
    )
    location_sql = assert_read_only_sql(
        "SELECT schema_name, location "
        "FROM INFORMATION_SCHEMA.SCHEMATA "
        "WHERE schema_name = @dataset"
    )
    # The permission/existence probe + bounded sample are read-only SHAPES; the
    # live Phase-B probe binds the object via the typed TableReference (see note).
    permission_sql = assert_read_only_sql(
        f"SELECT 1 FROM `{ref}` LIMIT 1"  # noqa: S608 -- shape only; live probe uses TableReference
    )
    sample_sql = assert_read_only_sql(
        f"SELECT * FROM `{ref}` LIMIT {limit}"  # noqa: S608 -- shape only; LIMIT is the only literal
    )

    return {
        "object_ref": ref,
        "read_only": True,
        "params": {"dataset": dataset, "object": obj},
        "steps": [
            {"name": "permission", "sql": permission_sql, "kind": "existence_permission"},
            {"name": "schema", "sql": schema_sql, "kind": "information_schema"},
            {"name": "location", "sql": location_sql, "kind": "information_schema"},
            {"name": "sample", "sql": sample_sql, "kind": "bounded_sample", "limit": limit},
        ],
        # Phase B: the live probe wires a BigQuery read-only client here. No live
        # BQ credentials exist in this environment; probe execution is Phase B.
        "phase_b": "live BigQuery read-only client execution deferred (no credentials)",
    }


# ---------------------------------------------------------------------------
# Observation classification (safe-fail matrix) -- pure.
# ---------------------------------------------------------------------------


def classify_observation(
    *,
    exists: bool,
    readable: bool,
    row_count: int | None,
    object_location: str | None,
    project_region: str | None,
    moved: bool = False,
    expected_content_hash: str | None = None,
    observed_content_hash: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Map a raw probe result to a ``(verdict, repair)`` pair (safe-fail matrix).

    Precedence (most-blocking first, fail closed):
      permission_revoked > absent > moved > region_incompatible >
      changed_after_preview > empty > ok

    ``repair`` is ``{}`` for ``ok``; the per-verdict repair action otherwise. NONE
    of the blocking verdicts replaces a published version (the caller enforces
    that by reading the verdict).
    """
    # Permission first: an unreadable object cannot be trusted for anything else.
    if not readable:
        return VERDICT_PERMISSION_REVOKED, dict(_REPAIR_BY_VERDICT[VERDICT_PERMISSION_REVOKED])
    if not exists:
        return VERDICT_ABSENT, dict(_REPAIR_BY_VERDICT[VERDICT_ABSENT])
    if moved:
        return VERDICT_MOVED, dict(_REPAIR_BY_VERDICT[VERDICT_MOVED])
    # Region: an object in an incompatible location cannot be joined to the project.
    if (
        object_location is not None
        and project_region is not None
        and object_location.strip().upper() != project_region.strip().upper()
    ):
        repair = dict(_REPAIR_BY_VERDICT[VERDICT_REGION_INCOMPATIBLE])
        repair["object_location"] = object_location
        repair["project_region"] = project_region
        return VERDICT_REGION_INCOMPATIBLE, repair
    # Changed-after-preview: a preview pinned an expected hash and it no longer holds.
    if (
        expected_content_hash is not None
        and observed_content_hash is not None
        and expected_content_hash != observed_content_hash
    ):
        return (
            VERDICT_CHANGED_AFTER_PREVIEW,
            dict(_REPAIR_BY_VERDICT[VERDICT_CHANGED_AFTER_PREVIEW]),
        )
    # Empty is blocked by default (mirrors 12.5 empty_candidate honesty).
    if row_count is not None and row_count == 0:
        return VERDICT_EMPTY, dict(_REPAIR_BY_VERDICT[VERDICT_EMPTY])
    return VERDICT_OK, {}


def is_blocking_verdict(verdict: str) -> bool:
    """Predicate: does *verdict* block publication (published pointer untouched)?"""
    return verdict in BLOCKING_VERDICTS


# ---------------------------------------------------------------------------
# Virtual pull commit -- provenance equivalent to a real connector pull.
# ---------------------------------------------------------------------------


def _mint_observation_id() -> str:
    return f"ebqo_{ULID()}"


def _derive_virtual_pull_id(
    *, object_ref_str: str, content_hash: str, schema_hash: str, row_count: int
) -> str:
    # House style: real pulls are ``pull_<ULID>``; a virtual (external-BQ) pull is
    # explicitly marked ``vpull_<...>`` so provenance can tell it apart from a
    # provider pull while still being a first-class, replayable pull commit.
    #
    # DETERMINISTIC (not a fresh ULID): a replay of the SAME external observation
    # (same object + same content/schema/row evidence) MUST reproduce the SAME vpull
    # id, so the projection-plan snapshot handed to ``create_execution`` is
    # byte-identical on retry and the 12.5 idempotency fingerprint MATCHES -- an
    # unchanged retry returns the existing execution instead of a false
    # ``IdempotencyConflict`` (HIGH-1). A genuine content change yields new evidence
    # -> a new vpull id -> a new candidate (correct).
    seed = _canonical_json(
        {
            "object_ref": object_ref_str,
            "content_hash": content_hash,
            "schema_hash": schema_hash,
            "row_count": row_count,
        }
    )
    return f"vpull_{_sha256(seed)[:26]}"


def content_fingerprint(sample_rows: list[dict[str, Any]] | None, row_count: int | None) -> str:
    """Deterministic content hash over a bounded sample + the row count.

    v1 honesty: this is a fingerprint over the OBSERVED bounded sample + row count,
    NOT a full-table byte hash (which would require scanning the whole external
    object every observation -- a cost the read-only registration must not impose).
    It is sufficient to detect content CHANGE for the unchanged-noop / changed-
    after-preview checks; full byte-identity against the object is not claimed
    (mirrors 12.5's content-hash honesty -- advisory provenance, not a proof).
    """
    # Order-insensitive at the ROW level: BigQuery does not guarantee row order for a
    # ``LIMIT`` sample without ``ORDER BY``, so two observations of genuinely identical
    # content can return the same rows in a different order. We canonicalize each row
    # (keys sorted) and then sort the multiset of canonical rows before hashing, so the
    # fingerprint depends on content, not on the sample's arrival order (HIGH-2).
    payload = {
        "row_count": row_count,
        "sample": sorted(
            _canonical_json({k: r[k] for k in sorted(r)}) for r in (sample_rows or [])
        ),
    }
    return _sha256(_canonical_json(payload))


def build_virtual_pull_commit(
    *,
    external_object: dict[str, Any],
    watermark: datetime | None,
    content_hash: str,
    schema_hash: str,
    row_count: int,
    object_location: str | None,
) -> dict[str, Any]:
    """Build the virtual pull commit -- provenance EQUIVALENT to a real pull.

    Returns a bundle carrying a synthetic ``pull_id`` (``vpull_<ULID>``), the object
    coordinates, the watermark, and the content/schema evidence hashes. This is the
    "equivalent provenance / replay evidence" the story requires: the same shape a
    connector pull records (pull_id + evidence), so downstream 12.3/12.4/12.5 treat
    an external-BQ candidate identically to a connector candidate.

    The ``pull_id`` is DERIVED deterministically from the object + content/schema/row
    evidence, so an unchanged retry reproduces the same commit (see
    ``_derive_virtual_pull_id``); an unchanged-content no-op mints nothing (the caller
    checks ``is_unchanged_noop`` first).
    """
    ref = object_ref(external_object)
    return {
        "pull_id": _derive_virtual_pull_id(
            object_ref_str=ref,
            content_hash=content_hash,
            schema_hash=schema_hash,
            row_count=row_count,
        ),
        "kind": "virtual_external_bq",
        "object_ref": ref,
        "writer_identity": str(external_object.get("writer_identity", "")),
        "watermark": watermark.astimezone(timezone.utc).isoformat() if watermark else None,
        "content_hash": content_hash,
        "schema_hash": schema_hash,
        "row_count": row_count,
        "object_location": object_location,
    }


def is_unchanged_noop(
    *, prior_content_hash: str | None, prior_schema_hash: str | None,
    observed_content_hash: str, observed_schema_hash: str,
) -> bool:
    """True when the observed content+schema equal the last committed observation.

    A repeated observation of identical content+schema is an AUDITABLE no-op -- no
    virtual pull commit is minted, no candidate execution is created. The caller
    still records the observation row (verdict ``unchanged_noop``) so the no-op is
    auditable, but it does NOT touch the published pointer.
    """
    if prior_content_hash is None or prior_schema_hash is None:
        return False
    return prior_content_hash == observed_content_hash and prior_schema_hash == observed_schema_hash


# ---------------------------------------------------------------------------
# Scheduler guard -- EXPLICIT invariant (not an accidental side effect).
# ---------------------------------------------------------------------------


def is_external_bq_datastream(datastream_row: dict[str, Any]) -> bool:
    """Predicate: is this datastream an external_bq registration (no provider pull)?

    Reads ``source_kind`` (canonical) OR the ``external_dispatch_excluded`` flag
    (migration 076). Either being set marks the stream as one the scheduler must
    NEVER dispatch a provider pull for. This is the EXPLICIT invariant the scheduler
    consumes (``exclude_external_bq_dispatch``), so exclusion is intentional, not an
    accidental consequence of a NULL connection_ref_id.
    """
    return (
        datastream_row.get("source_kind") == "external_bq"
        or bool(datastream_row.get("external_dispatch_excluded"))
    )


def exclude_external_bq_dispatch(
    datastream_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only the datastreams the scheduler MAY dispatch a provider pull for.

    Filters OUT every external_bq datastream. The scheduler calls this on the rows
    it fetched (belt-and-braces alongside the SQL ``external_dispatch_excluded =
    FALSE`` guard in migration 076) so an external_bq stream can never reach
    ``enqueue_pull``. Source-agnostic: it decides purely on ``is_external_bq_data
    stream`` -- no provider name.
    """
    return [row for row in datastream_rows if not is_external_bq_datastream(row)]


# ---------------------------------------------------------------------------
# DB seam -- record an observation (append-only ledger) and, when it mints a
# virtual pull commit, hand the 12.5 candidate registry seam back to the caller.
# ---------------------------------------------------------------------------


def _last_committed_observation(conn, datastream_id: str, project_id: str) -> dict[str, Any] | None:
    """Return the most recent ``ok`` observation for a datastream (or None)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content_hash, schema_hash, watermark, row_count
            FROM app.external_bq_observations
            WHERE datastream_id = %s AND project_id = %s AND verdict = 'ok'
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "content_hash": row[0],
        "schema_hash": row[1],
        "watermark": row[2],
        "row_count": row[3],
    }


def record_observation(
    *,
    datastream_id: str,
    project_id: str,
    external_object: dict[str, Any],
    verdict: str,
    actor: str,
    conn,
    plan_version_id: str | None = None,
    watermark: datetime | None = None,
    content_hash: str | None = None,
    schema_hash: str | None = None,
    row_count: int | None = None,
    object_location: str | None = None,
    expected_content_hash: str | None = None,
    repair: dict[str, Any] | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """Insert one append-only observation row into ``app.external_bq_observations``.

    The caller owns the transaction (does NOT commit) so the observation can be
    composed atomically with the 12.5 ``create_execution`` it may mint. Writes an
    audit row (``ACTION_EXTERNAL_BQ_OBSERVED`` for ok/unchanged, ``_OBSERVATION_
    FAILED`` for a blocking verdict). Returns the inserted row as a dict.

    AD-9: content_hash/schema_hash/row_count are written verbatim (honest NULLs on a
    blocking verdict); never coerced to 0.
    """
    from core.audit import (  # noqa: PLC0415
        ACTION_EXTERNAL_BQ_OBSERVATION_FAILED,
        ACTION_EXTERNAL_BQ_OBSERVED,
        insert_audit_row,
    )

    ref_parts = external_object
    observation_id = _mint_observation_id()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.external_bq_observations
                (id, datastream_id, project_id, plan_version_id,
                 object_project, object_dataset, object_name, writer_identity,
                 watermark, content_hash, schema_hash, row_count, object_location,
                 verdict, expected_content_hash, repair, execution_id, observed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s)
            RETURNING id, datastream_id, project_id, verdict, execution_id, observed_at
            """,
            (
                observation_id,
                datastream_id,
                project_id,
                plan_version_id,
                str(ref_parts["project"]),
                str(ref_parts["dataset"]),
                str(ref_parts["object"]),
                str(ref_parts.get("writer_identity", "")),
                watermark,
                content_hash,
                schema_hash,
                row_count,
                object_location,
                verdict,
                expected_content_hash,
                json.dumps(repair or {}),
                execution_id,
                actor,
            ),
        )
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        record = dict(zip(cols, row))
        if record.get("observed_at") is not None:
            record["observed_at"] = record["observed_at"].isoformat()

    action = (
        ACTION_EXTERNAL_BQ_OBSERVED
        if verdict in (VERDICT_OK, VERDICT_UNCHANGED_NOOP)
        else ACTION_EXTERNAL_BQ_OBSERVATION_FAILED
    )
    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=action,
        provider_account="",
        connection_ref="",
        metadata={
            "observation_id": observation_id,
            "datastream_id": datastream_id,
            "project_id": project_id,
            "object_ref": object_ref(external_object),
            "verdict": verdict,
            "execution_id": execution_id,
            "row_count": row_count,
        },
    )
    return record


def observe_and_register(
    *,
    datastream_id: str,
    project_id: str,
    external_object: dict[str, Any],
    plan_version_id: str,
    mapping_version_id: str,
    projection_plan: dict[str, Any],
    probe_result: dict[str, Any],
    actor: str,
    idempotency_key: str,
    conn,
    project_region: str | None = None,
    expected_content_hash: str | None = None,
) -> dict[str, Any]:
    """Observe an external object and, on a fresh ``ok`` verdict, mint a candidate.

    ``probe_result`` is the OUTCOME of a (Phase-B live / test-injected) read-only
    probe: ``{exists, readable, row_count, schema_hash, object_location,
    sample_rows}``. This function is source-agnostic downstream: it classifies the
    observation (safe-fail matrix), and on a fresh ``ok`` verdict it builds the
    virtual pull commit and hands the (plan/mapping/projection) tuple to the 12.5
    ``create_execution`` -- the SAME candidate registry a connector pull uses. It
    NEVER edits datastream_publication.py; it calls its public seam.

    Returns ``{observation, verdict, execution?, virtual_pull_commit?}``:
      * verdict == 'unchanged_noop' -> observation recorded, NO execution minted.
      * verdict in BLOCKING_VERDICTS -> observation recorded (failed audit), NO
        execution minted, published pointer untouched.
      * verdict == 'ok' -> observation recorded + a 12.5 execution created in the
        candidate registry (state 'created'); the caller advances it and runs the
        12.5 DQ gates + commit_publication.

    The caller owns the transaction (does NOT commit here) so the observation + the
    minted execution land atomically.
    """
    from core.datastream_publication import create_execution  # noqa: PLC0415

    exists = bool(probe_result.get("exists", False))
    readable = bool(probe_result.get("readable", False))
    row_count = probe_result.get("row_count")
    schema_hash = probe_result.get("schema_hash")
    object_location = probe_result.get("object_location")
    sample_rows = probe_result.get("sample_rows")
    watermark = probe_result.get("watermark")

    observed_content_hash = content_fingerprint(sample_rows, row_count)

    verdict, repair = classify_observation(
        exists=exists,
        readable=readable,
        row_count=row_count,
        object_location=object_location,
        project_region=project_region,
        moved=bool(probe_result.get("moved", False)),
        expected_content_hash=expected_content_hash,
        observed_content_hash=observed_content_hash,
    )

    # Unchanged-content no-op: only meaningful when the object is otherwise ok.
    if verdict == VERDICT_OK:
        prior = _last_committed_observation(conn, datastream_id, project_id)
        if prior is not None and is_unchanged_noop(
            prior_content_hash=prior.get("content_hash"),
            prior_schema_hash=prior.get("schema_hash"),
            observed_content_hash=observed_content_hash,
            observed_schema_hash=str(schema_hash or ""),
        ):
            observation = record_observation(
                datastream_id=datastream_id,
                project_id=project_id,
                external_object=external_object,
                verdict=VERDICT_UNCHANGED_NOOP,
                actor=actor,
                conn=conn,
                plan_version_id=plan_version_id,
                watermark=watermark,
                content_hash=observed_content_hash,
                schema_hash=schema_hash,
                row_count=row_count,
                object_location=object_location,
                repair={},
            )
            return {"observation": observation, "verdict": VERDICT_UNCHANGED_NOOP}

    if verdict != VERDICT_OK:
        observation = record_observation(
            datastream_id=datastream_id,
            project_id=project_id,
            external_object=external_object,
            verdict=verdict,
            actor=actor,
            conn=conn,
            plan_version_id=plan_version_id,
            watermark=watermark,
            content_hash=observed_content_hash if exists and readable else None,
            schema_hash=schema_hash if exists and readable else None,
            row_count=row_count,
            object_location=object_location,
            expected_content_hash=expected_content_hash,
            repair=repair,
        )
        return {"observation": observation, "verdict": verdict}

    # Fresh, changed, ok content -> mint a virtual pull commit + a 12.5 candidate.
    virtual_pull_commit = build_virtual_pull_commit(
        external_object=external_object,
        watermark=watermark,
        content_hash=observed_content_hash,
        schema_hash=str(schema_hash or ""),
        row_count=int(row_count or 0),
        object_location=object_location,
    )

    # The virtual pull commit is provenance carried INTO the projection plan snapshot
    # so the 12.5 execution records where the external candidate came from -- the
    # SAME provenance seam a connector pull uses (pull_id).
    plan_with_provenance = dict(projection_plan)
    plan_with_provenance["virtual_pull_commit"] = virtual_pull_commit

    execution = create_execution(
        datastream_id,
        project_id,
        plan_version_id,
        mapping_version_id,
        plan_with_provenance,
        actor,
        idempotency_key,
        conn,
    )

    observation = record_observation(
        datastream_id=datastream_id,
        project_id=project_id,
        external_object=external_object,
        verdict=VERDICT_OK,
        actor=actor,
        conn=conn,
        plan_version_id=plan_version_id,
        watermark=watermark,
        content_hash=observed_content_hash,
        schema_hash=schema_hash,
        row_count=row_count,
        object_location=object_location,
        expected_content_hash=expected_content_hash,
        repair={},
        execution_id=execution["id"],
    )

    return {
        "observation": observation,
        "verdict": VERDICT_OK,
        "execution": execution,
        "virtual_pull_commit": virtual_pull_commit,
    }
