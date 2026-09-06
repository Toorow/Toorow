"""Backend-aware raw landing for connector modules: DuckDB, or BigQuery.

Until now nothing in the product could write raw rows to BigQuery. Every
connector's ``_insert_raw_rows`` raised ``unsupported db_mode`` for anything but
DuckDB, and ``mirror_sync``'s BigQuery branch logged "deferred (Phase B)" and
returned -- so ``TOOROW_DB_MODE=bigquery`` was a mode the code accepted and
silently did not honour.

BigQuery has TWO ingestion paths and they are not interchangeable:

* **load job** (``MODE_LOAD``) -- batched, NOT billed for ingestion, minutes of
  latency, effectively unlimited volume. The right default for a daily pull.
* **streaming** (``MODE_STREAM``) -- rows are queryable in seconds, billed per
  byte ingested. The right choice when a surface has to feel live.

Choosing one for the other is a real cost or a real staleness, so the mode is
explicit rather than inferred: ``TOOROW_BQ_WRITE_MODE`` sets the default and a
caller may override per landing.

TWO STREAMING TRANSPORTS, AND WHY BOTH EXIST
    ``MODE_STORAGE_WRITE`` is the BigQuery Storage Write API: rows go as protobuf
    over gRPC, which is what buys the cheaper per-GB rate and commits that are
    exactly-once rather than at-least-once. It is Google's recommended path and
    the default for new connectors.

    ``MODE_STREAM`` is the older ``tabledata.insertAll``: JSON over HTTPS,
    at-least-once, billed at a higher rate. Kept because it needs nothing beyond
    ``google-cloud-bigquery`` and stays available to a deployment that cannot
    install the Storage Write distribution or cannot reach BigQuery over gRPC.

    Selecting ``storage_write`` where the package is missing FAILS rather than
    quietly falling back to ``stream``: the two differ in price and in delivery
    guarantee, so a silent downgrade would change the bill and the semantics
    without anything saying so.

WHY APPEND-ONLY MAKES STREAMING SAFE HERE
    Streamed rows sit in a write-optimised buffer where they cannot be UPDATEd or
    DELETEd for a while after arrival. That would be a problem for a mutable
    table. The raw zone is append-only (AD-7) and the staging models supersede by
    ``pull_id`` with a QUALIFY, so a re-pull adds rows and the newest wins -- no
    mutation is ever needed, and the buffer restriction never binds.

AD-2: source-agnostic. No provider names, no imports from ``server/modules``.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

MODE_LOAD = "load"
MODE_STREAM = "stream"
MODE_STORAGE_WRITE = "storage_write"
_MODES = (MODE_LOAD, MODE_STREAM, MODE_STORAGE_WRITE)

# Table and column names come from module code, never from a request. Validated
# anyway: these names are interpolated into DDL, which no amount of "the caller
# is trusted" makes safe to leave unchecked.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# One small vocabulary, mapped to each backend, so a connector declares its raw
# columns once rather than maintaining two dialects of the same table.
_DUCKDB_TYPES = {
    "STRING": "VARCHAR",
    "FLOAT": "DOUBLE",
    "INTEGER": "BIGINT",
    "DATE": "VARCHAR",
    "TIMESTAMP": "VARCHAR",
    "BOOLEAN": "BOOLEAN",
    "JSON": "JSON",
}
_BIGQUERY_TYPES = {
    "STRING": "STRING",
    "FLOAT": "FLOAT64",
    "INTEGER": "INT64",
    "DATE": "STRING",
    "TIMESTAMP": "STRING",
    "BOOLEAN": "BOOL",
    "JSON": "JSON",
}


_BIGQUERY_TYPE_ALIASES = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
}


def _normalized_bigquery_type(value: str) -> str:
    upper = str(value).upper()
    return _BIGQUERY_TYPE_ALIASES.get(upper, upper)


class RawLandingError(RuntimeError):
    """A raw landing could not be completed. Carries the backend and the table."""


def default_write_mode() -> str:
    """The BigQuery write mode to use when a caller does not name one."""
    mode = (os.environ.get("TOOROW_BQ_WRITE_MODE") or MODE_LOAD).strip().lower()
    return mode if mode in _MODES else MODE_LOAD


def resolve_warehouse_project() -> str:
    """The GCP project the warehouse writes into. Never an ambient default.

    `bigquery.Client(project=None)` silently adopts whatever project the
    application-default credentials happen to name. On a developer machine that
    is routinely an unrelated project -- this was measured landing rows into one
    -- and the failure is invisible: the write succeeds, in the wrong place, and
    is found much later by someone looking for data that is not there.

    The codebase already disagreed with itself here: `warehouse.py` reads
    GCP_PROJECT and falls back to the ambient default, `warehouse_tenancy.py`
    reads GOOGLE_CLOUD_PROJECT and falls back to a hardcoded name. Both are
    accepted, in that order, and an unset pair REFUSES rather than guesses.
    """
    for variable in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        value = (os.environ.get(variable) or "").strip()
        if value:
            return value
    raise RawLandingError(
        "raw landing: no warehouse project configured -- set GCP_PROJECT. "
        "Refusing to inherit the ambient credential's default project."
    )


_CANDIDATE_EXECUTION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "toorow_candidate_execution", default=None
)

#: A PREVIEW MUST NOT LAND, AND NO CONNECTOR SHOULD HAVE TO KNOW THAT.
#:
#: The setup preview of step 4 used to require each module to accept `dry_run`
#: and return rows instead of landing them. Measured 2026-08-11: THREE modules of
#: thirty-nine had it, so thirty-six Connectors could not be previewed, and a
#: Datastream cannot be activated without a preview. That is the same shape as
#: the candidate isolation above, and it gets the same answer -- the SCOPE
#: carries it, not thirty-six signatures, each of which is an opportunity to
#: forget one and land a preview into the warehouse.
#:
#: Inside `preview_capture()` every raw write is diverted into the buffer and
#: nothing reaches DuckDB or BigQuery. `warehouse_write.open_raw_writer` honours
#: the same scope, which is what covers the modules that write through a DuckDB
#: connection of their own rather than through `land_raw_rows`.
_PREVIEW_CAPTURE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "toorow_preview_capture", default=None
)


@contextlib.contextmanager
def preview_capture():
    """Divert every raw write in this scope into a buffer. Nothing is landed.

    Yields the buffer: ``{"rows": [...], "tables": [...], "writers": [...]}``.
    """
    buffer: dict = {"rows": [], "tables": [], "writers": []}
    token = _PREVIEW_CAPTURE.set(buffer)
    try:
        yield buffer
    finally:
        _PREVIEW_CAPTURE.reset(token)


def active_preview_capture() -> dict | None:
    """The preview buffer in force here, if any."""
    return _PREVIEW_CAPTURE.get()


#: {execution_id: {relations reellement ecrites}}. Un dict de module et non un
#: ContextVar : l'ECRITURE a lieu dans le scope de l'execution, mais la LECTURE a
#: lieu apres sa sortie, quand le driver compose la provenance du candidat. Un
#: ContextVar serait deja reinitialise a ce moment-la. La cle est l'execution, donc
#: deux executions concurrentes ne se melangent pas.
_LANDED_RELATIONS: dict[str, set[str]] = {}
_LANDED_COLUMNS: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {}


@contextlib.contextmanager
def candidate_execution(execution_id: str):
    """Route every raw write inside this scope into *execution_id*'s isolation.

    Ambient on purpose, and this is the one place it is the right tool. A
    candidate has to be materialized by running the connector's REAL pull -- that
    is what makes it evidence rather than a mock -- but `pull()` has a fixed
    signature across 24 modules and no execution to pass. Threading one through
    would be twenty-four edits, each an opportunity to forget one module, and a
    module that forgot would publish its candidate straight into the marts.

    So the scope carries it, the way a transaction scope does: WHERE writes go is
    a property of the execution, not an argument each connector must remember.
    ContextVar rather than a global, so concurrent executions in the same worker
    never see each other's isolation.
    """
    token = _CANDIDATE_EXECUTION.set(execution_id)
    try:
        yield execution_id
    finally:
        _CANDIDATE_EXECUTION.reset(token)


def landed_candidate_relations(execution_id: str) -> list[str]:
    """Les relations dans lesquelles CETTE execution a reellement ecrit.

    Enregistre a l'atterrissage plutot que reconstruit par l'appelant : le nom
    isole est calcule ici (`candidate_table`), et le seul moyen de garantir que
    la reference publiee designe la relation qui existe est de publier celle qui
    a servi. Une reconstruction ailleurs peut deriver ; une observation, non.

    Vide quand l'execution n'a rien landed -- une fenetre sans donnee est un
    resultat, pas une panne, et l'appelant doit pouvoir le distinguer d'un echec.
    """
    return sorted(_LANDED_RELATIONS.get(execution_id, set()))


def unlanded_candidate_ref(execution_id: str) -> str:
    """The provenance reference of an execution that landed NO readable relation.

    THE ONLY PLACE THIS STRING IS COMPOSED (AI-308). Two activation drivers built
    it from two copies of the same comment -- connector pull and managed feed --
    which is how one line of the tracker could name one of them and be believed
    complete. A third driver now inherits the convention instead of inventing a
    third one.

    It carries the execution because provenance must still name the act: the
    activation guard asks exactly that (`datastream_activation.py:2064`). It is
    deliberately NOT an identifier, so that
    `query_execution.names_a_readable_relation` answers False and the read files
    an outcome naming the gesture -- rather than a name that looks queryable and
    is not.
    """
    if not execution_id:
        raise RawLandingError("an unlanded candidate reference requires an execution_id")
    return f"execution/{execution_id}/candidate/relation"


def landed_candidate_columns(execution_id: str, relation: str) -> list[tuple[str, str]]:
    """Return the exact logical columns supplied when *relation* was landed.

    This is the connector-pull equivalent of a managed import's immutable
    dispatch bundle. It is observed at the only seam that knows both the
    execution-isolated relation and its declared types; publication must not
    reverse-engineer those types from values or from a later warehouse query.
    """
    return list(_LANDED_COLUMNS.get(execution_id, {}).get(relation, ()))


def clear_candidate_landing(execution_id: str) -> None:
    """Release the bounded, process-local landing evidence for one execution.

    The activation driver reads this evidence after the pull scope closes, so
    cleanup cannot belong to :func:`candidate_execution`. Its safe owner is the
    activation worker, after completion and stage/phase evidence have consumed
    the driver's result, including on a terminal failure.
    """
    _LANDED_RELATIONS.pop(execution_id, None)
    _LANDED_COLUMNS.pop(execution_id, None)


def record_candidate_landing(
    execution_id: str,
    relation: str,
    columns: list[tuple[str, str]],
) -> None:
    """Record one execution-isolated relation at the shared writer seam."""
    if not execution_id:
        raise RawLandingError("candidate landing evidence requires an execution_id")
    _validate(relation, columns)
    frozen_columns = tuple((str(name), str(kind)) for name, kind in columns)
    existing_columns = _LANDED_COLUMNS.setdefault(execution_id, {}).get(relation)
    if existing_columns is not None and existing_columns != frozen_columns:
        raise RawLandingError(
            "raw landing candidate relation was reused with a different column contract: "
            f"recorded={existing_columns!r}, observed={frozen_columns!r}"
        )
    _LANDED_RELATIONS.setdefault(execution_id, set()).add(relation)
    _LANDED_COLUMNS[execution_id][relation] = frozen_columns


def active_candidate_execution() -> str | None:
    """The execution whose isolation is in force here, if any."""
    return _CANDIDATE_EXECUTION.get()


def promoted_relation(relation: str) -> str:
    """The SHARED relation an isolated candidate is promoted into, given either name.

    THE DEFECT THIS NAMES (2026-09-04, harness project). A mapping-change candidate
    lands in `<table>__cand_<execution_id>`; `promote_candidate` appends its rows into
    `<table>` and the isolated relation is gone. Publication still recorded the
    isolated name as the Output version's `relation_ref` -- a connector pull records
    the shared name -- so every query planned against a table BigQuery no longer
    had (404 `managed_feed_ds_…__cand_dse_01M1P084…`). A published candidate IS a
    promoted one; this is the one place that says so, for the writer at publish and
    for the reader at plan time, so the two cannot drift apart again.
    """
    text = str(relation or "")
    dataset, dot, bare = text.rpartition(".")
    head = bare.split("__cand_", 1)[0]
    return f"{dataset}{dot}{head}" if dot else head


def candidate_table(table: str, execution_id: str) -> str:
    """The isolated relation ONE candidate execution lands in.

    This is the whole of the execution isolation, and it is structural rather
    than a predicate. A Connector lands every pull into one shared raw table and
    all 137 staging models supersede on ``pull_id DESC``, so a candidate written
    there is live the moment it lands -- numbers published before anyone
    confirmed them. Tagging the rows would not help: a tag has to be honoured by
    every reader, and one model that forgets it publishes the candidate.

    A separate relation cannot be forgotten. Staging names the shared table, so a
    candidate is invisible to it BY CONSTRUCTION -- and no dbt model changes.
    Publication is then `promote_candidate`, which appends those rows into the
    shared table with their pull_id intact (AD-7), and rollback is
    `discard_candidate`, which drops the relation.
    """
    if not _IDENTIFIER.match(table or ""):
        raise RawLandingError(f"raw landing table {table!r} is not an identifier")
    if not execution_id:
        raise RawLandingError("candidate isolation requires an execution_id")
    # NOT a sanitiser. Replacing the illegal characters would map `exec-A` and
    # `exec_A` -- and `...` and `///` -- onto ONE relation, so two distinct
    # executions would land in the same place and the isolation this function
    # exists to provide would be silently gone. An id that is already a bare
    # identifier is used verbatim, because a readable relation name is worth
    # having when something has to be inspected; anything else is hashed, which
    # is injective for every input that matters.
    if re.fullmatch(r"[A-Za-z0-9_]+", execution_id):
        suffix = execution_id
    else:
        suffix = "h" + hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:16]
    marker = f"__cand_{suffix}"
    # IDEMPOTENT FOR THE SAME EXECUTION, and it has to be: two places isolate,
    # and neither can see the other. `land_rows` suffixes the target and RECORDS
    # it; `warehouse_write._rewrite` then reads the very statement that name
    # produced and suffixes it again. Traced 2026-08-16:
    #
    #   candidate_table('managed_feed_ds_X')            -> 'managed_feed_ds_X__cand_E'
    #   candidate_table('managed_feed_ds_X__cand_E')    -> '..._cand_E__cand_E'
    #
    # The rows landed in the DOUBLE name while the reported `landing.table` --
    # and therefore the published reference -- named the single one. A reference
    # that points at a relation nobody wrote is worse than an error: it reads as
    # a successful landing.
    #
    # Only the SAME suffix is a no-op. Isolating for a DIFFERENT execution still
    # suffixes, because that is a genuine second isolation and collapsing it
    # would put two executions in one relation -- the exact failure this function
    # exists to prevent.
    if table.endswith(marker):
        return table
    return f"{table}{marker}"


def _canonical_value(value, kind: str):
    """One value in the representation its DECLARED kind fixes, whatever the side.

    AI-321 (2026-08-29, G9 on production): the expected fingerprint is computed
    over the parser's typed rows and the observed one over what BigQuery returns
    for the same rows. Same 5 rows, same schema, two content fingerprints -- a
    `Decimal("110.00")` serialised by `default=str` is `"110.00"` on one side
    and `110.0` (a FLOAT64 come back as `float`) on the other; an INT64 comes
    back as `int` where the parser kept `"5"`. The first governed import that
    ever reached promotion was refused `candidate_evidence_diverged` on that
    alone. A fingerprint is a claim about the DATA, so each value is put in the
    shape its kind fixes before hashing: numbers as numbers, text as text, null
    as null. `default=str` stays for anything the kind does not cover.
    """
    if value is None:
        return None
    if kind == "FLOAT":
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)
    if kind == "INTEGER":
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return str(value)
    if kind == "BOOLEAN":
        if isinstance(value, str):
            return value.strip().lower() in {"true", "t", "1", "yes"}
        return bool(value)
    if kind in {"STRING", "DATE", "TIMESTAMP"}:
        return value if isinstance(value, str) else str(value)
    return value


def _canonical_rows_fingerprint(
    rows: list[dict],
    columns: list[tuple[str, str]],
) -> str:
    canonical_rows = sorted(
        json.dumps(
            [_canonical_value(row.get(name), kind) for name, kind in columns],
            separators=(",", ":"),
            default=str,
        )
        for row in rows
    )
    payload = {
        "columns": [{"name": name, "type": kind} for name, kind in columns],
        "rows": canonical_rows,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def candidate_rows_fingerprint(
    rows: list[dict],
    *,
    columns: list[tuple[str, str]],
) -> str:
    """Fingerprint the expected typed warehouse representation."""
    _validate("_candidate", columns)
    return _canonical_rows_fingerprint(rows, columns)


def candidate_schema_fingerprint(columns: list[tuple[str, str]]) -> str:
    # Declared logical schema, independent of parser metadata.
    _validate("_candidate", columns)
    return hashlib.sha256(
        json.dumps(
            [{"name": name, "type": kind} for name, kind in columns],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _duckdb_candidate_source(con, table: str, execution_id: str) -> tuple[str, bool]:
    """The candidate's qualified DuckDB name AS THE CATALOG HOLDS IT, and whether it exists.

    TWO isolation mechanisms exist on DuckDB, and which one applied depends on HOW
    the execution reached the writer:

      * through the ambient `candidate_execution` scope, `warehouse_write.
        _open_candidate_writer` creates a SCHEMA per execution AND the table keeps
        its `__cand_` suffix -- that is the path every real materialization takes;
      * through an explicit `execution_id=` argument to `land_raw_rows`, only the
        table name is suffixed, in whatever schema the connection is on.

    Promotion and inspection run AFTER the write and therefore outside the scope,
    so they cannot know which happened. They must not guess either: this OBSERVES
    the schema from `information_schema` rather than recomposing it. A
    reconstruction can drift the day the naming rule changes; an observation
    cannot -- the same reasoning `landed_candidate_relations` already applies.

    Naming the candidate bare is what the code did, and on the scope path it
    found nothing while `information_schema` -- schema-agnostic -- still reported
    the table as present. The miss surfaced as a bare `CatalogException` on the
    NEXT statement instead of as an absence, and `csv_excel_import` turned that
    into `dispatch_promotion_reconciliation_required`: a naming bug wearing the
    name of an infrastructure hiccup.
    """
    name = candidate_table(table, execution_id)
    row = con.execute(
        "SELECT table_schema FROM information_schema.tables WHERE table_name = ? "
        "ORDER BY table_schema LIMIT 1",
        [name],
    ).fetchone()
    if row is None:
        return f'"{name}"', False
    return f'"{row[0]}"."{name}"', True


def inspect_candidate(
    table: str,
    execution_id: str,
    *,
    columns: list[tuple[str, str]],
    project_id: str | None = None,
    backend: str | None = None,
) -> dict:
    """Read independent row/schema evidence from the materialized candidate."""
    source = candidate_table(table, execution_id)
    _validate(table, columns)
    names = [name for name, _ in columns]
    target = (backend or os.environ.get("TOOROW_DB_MODE") or "duckdb").strip().lower()

    if target == "duckdb":
        from core.warehouse_write import open_raw_writer  # noqa: PLC0415

        con = _duckdb_connection(open_raw_writer, project_id)
        try:
            qualified_source, _present = _duckdb_candidate_source(con, table, execution_id)
            actual = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [source],
            ).fetchall()
            if not actual:
                raise RawLandingError(f"raw landing candidate {source!r} is absent")
            expected_physical = [(name, _DUCKDB_TYPES[kind]) for name, kind in columns]
            actual_normalized = [(str(name), str(kind).upper()) for name, kind in actual]
            if actual_normalized != expected_physical:
                raise RawLandingError("raw landing candidate schema differs from contract")
            values = con.execute(
                # QUALIFIED with the schema the catalog reports, because the
                # candidate may be isolated by schema and this connection is not
                # inside its scope.
                f"SELECT {', '.join(names)} FROM {qualified_source}"  # noqa: S608
            ).fetchall()
        finally:
            con.close()
        rows = [dict(zip(names, value)) for value in values]
    elif target == "bigquery":
        from google.cloud import bigquery  # noqa: PLC0415

        from core import warehouse_tenancy  # noqa: PLC0415

        client = bigquery.Client(project=resolve_warehouse_project())
        dataset = warehouse_tenancy.bigquery_raw_dataset(project_id, conn=None)
        ref = f"{client.project}.{dataset}.{source}"
        found = client.get_table(ref)
        expected_physical = [(name, _BIGQUERY_TYPES[kind]) for name, kind in columns]
        actual_normalized = [
            (field.name, _normalized_bigquery_type(field.field_type)) for field in found.schema
        ]
        if actual_normalized != expected_physical:
            raise RawLandingError("raw landing candidate schema differs from contract")
        tick = chr(96)
        rows = [
            dict(value)
            for value in client.query(
                f"SELECT {', '.join(names)} FROM {tick}{ref}{tick}"  # noqa: S608
            ).result()
        ]
    else:
        raise RawLandingError(f"raw landing: unsupported backend {target!r}")

    return {
        "row_count": len(rows),
        "content_fingerprint": _canonical_rows_fingerprint(rows, columns),
        "schema_fingerprint": candidate_schema_fingerprint(columns),
        "relation": source,
        "backend": target,
    }


def promote_candidate(
    table: str,
    execution_id: str,
    *,
    columns: list[tuple[str, str]],
    project_id: str | None = None,
    backend: str | None = None,
    idempotency_column: str | None = None,
    idempotency_value: str | None = None,
    expected_rows: int | None = None,
    expected_content_fingerprint: str | None = None,
    expected_schema_fingerprint: str | None = None,
) -> dict:
    """Append a candidate atomically and make retries unable to duplicate rows."""
    source = candidate_table(table, execution_id)
    _validate(table, columns)
    names = ", ".join(name for name, _ in columns)
    column_names = {name for name, _ in columns}
    if (idempotency_column is None) != (idempotency_value is None):
        raise RawLandingError("promotion idempotency column and value must be paired")
    if idempotency_column is not None and idempotency_column not in column_names:
        raise RawLandingError("promotion idempotency column is not in the candidate")
    target = (backend or os.environ.get("TOOROW_DB_MODE") or "duckdb").strip().lower()

    if target == "duckdb":
        from core.warehouse_write import open_raw_writer  # noqa: PLC0415

        con = _duckdb_connection(open_raw_writer, project_id)
        replayed = False
        promoted = 0
        try:
            ddl = ", ".join(f"{name} {_DUCKDB_TYPES[kind]}" for name, kind in columns)
            con.execute("BEGIN TRANSACTION")
            con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({ddl})")  # noqa: S608
            actual_schema = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            expected_physical = [(name, _DUCKDB_TYPES[kind]) for name, kind in columns]
            normalized_schema = [(str(name), str(kind).upper()) for name, kind in actual_schema]
            if normalized_schema != expected_physical:
                raise RawLandingError("promotion target schema differs from contract")
            if (
                expected_schema_fingerprint is not None
                and candidate_schema_fingerprint(columns) != expected_schema_fingerprint
            ):
                raise RawLandingError("promotion target schema fingerprint diverges")

            # The existence check and the read are now ONE observation. They used
            # to disagree: `information_schema` said present, the bare-name read
            # said absent, and the branch taken was the one that then exploded.
            qualified_source, source_exists = _duckdb_candidate_source(con, table, execution_id)
            existing = 0
            if idempotency_column is not None:
                existing = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {idempotency_column} = ?",  # noqa: S608
                    [idempotency_value],
                ).fetchone()[0]
            if source_exists:
                moved = con.execute(
                    f"SELECT COUNT(*) FROM {qualified_source}"  # noqa: S608
                ).fetchone()[0]
                expected = moved if expected_rows is None else expected_rows
                if moved != expected:
                    raise RawLandingError("promotion candidate row count differs from evidence")
                if existing not in (0, expected):
                    raise RawLandingError("promotion target contains divergent execution rows")
                if existing == 0:
                    con.execute(
                        f"INSERT INTO {table} ({names}) "  # noqa: S608
                        f"SELECT {names} FROM {qualified_source}"
                    )
                replayed = existing == expected
                promoted = expected
            else:
                if expected_rows is None or existing != expected_rows:
                    raise RawLandingError("promotion candidate is absent and target is incomplete")
                replayed = True
                promoted = existing

            if expected_content_fingerprint is not None:
                if idempotency_column is None:
                    raise RawLandingError("content proof requires an idempotency predicate")
                values = con.execute(
                    f"SELECT {names} FROM {table} WHERE {idempotency_column} = ?",  # noqa: S608
                    [idempotency_value],
                ).fetchall()
                target_rows = [dict(zip([name for name, _ in columns], value)) for value in values]
                observed = _canonical_rows_fingerprint(target_rows, columns)
                if observed != expected_content_fingerprint:
                    raise RawLandingError("promotion target content fingerprint diverges")
            if source_exists:
                con.execute(f"DROP TABLE {qualified_source}")  # noqa: S608
            con.execute("COMMIT")
        except Exception:
            with contextlib.suppress(Exception):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        return {
            "promoted": promoted,
            "from": source,
            "into": table,
            "backend": "duckdb",
            "replayed": replayed,
            "content_fingerprint": expected_content_fingerprint,
            "schema_fingerprint": expected_schema_fingerprint,
        }

    if target == "bigquery":
        from google.api_core.exceptions import Conflict, NotFound  # noqa: PLC0415
        from google.cloud import bigquery  # noqa: PLC0415

        from core import warehouse_tenancy  # noqa: PLC0415

        if idempotency_column is None or idempotency_value is None:
            raise RawLandingError("BigQuery promotion requires an idempotency predicate")
        client = bigquery.Client(project=resolve_warehouse_project())
        dataset = warehouse_tenancy.bigquery_raw_dataset(project_id, conn=None)
        prefix = f"{client.project}.{dataset}"
        source_ref = f"{prefix}.{source}"
        target_ref = f"{prefix}.{table}"
        tick = chr(96)
        try:
            client.get_table(source_ref)
            source_exists = True
        except NotFound:
            source_exists = False

        if source_exists:
            client.query(
                f"CREATE TABLE IF NOT EXISTS {tick}{target_ref}{tick} LIKE {tick}{source_ref}{tick}"  # noqa: S608
            ).result()
            merge_job_id = (
                "toorow_promote_"
                + hashlib.sha256(f"{target_ref}:{idempotency_value}".encode("utf-8")).hexdigest()[
                    :32
                ]
            )
            values = ", ".join(f"S.{name}" for name, _ in columns)
            merge_sql = (
                f"MERGE {tick}{target_ref}{tick} T "
                f"USING {tick}{source_ref}{tick} S "
                f"ON T.{idempotency_column} = @execution_id "
                f"WHEN NOT MATCHED THEN INSERT ({names}) VALUES ({values})"
            )
            config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("execution_id", "STRING", idempotency_value)
                ]
            )
            try:
                merge_job = client.query(
                    merge_sql,
                    job_config=config,
                    job_id=merge_job_id,
                    # EXPLICITLY off, and `job_id_prefix` is not the alternative
                    # here. The deterministic job id IS the idempotency: a retry
                    # collides on it, the `Conflict` below adopts the first job,
                    # and the promotion cannot run twice. A prefix would mint a
                    # new id per attempt and lose exactly that. The client warns
                    # that this pairing will raise in a future release unless
                    # `job_retry` is disabled, so it is disabled.
                    job_retry=None,
                )
            except Conflict:
                merge_job = client.get_job(merge_job_id)
            merge_job.result()

        try:
            target_table = client.get_table(target_ref)
        except NotFound as exc:
            raise RawLandingError("promotion candidate is absent and target is incomplete") from exc
        expected_physical = [(name, _BIGQUERY_TYPES[kind]) for name, kind in columns]
        actual_physical = [
            (field.name, _normalized_bigquery_type(field.field_type))
            for field in target_table.schema
        ]
        if actual_physical != expected_physical:
            raise RawLandingError("promotion target schema differs from contract")
        if (
            expected_schema_fingerprint is not None
            and candidate_schema_fingerprint(columns) != expected_schema_fingerprint
        ):
            raise RawLandingError("promotion target schema fingerprint diverges")

        proof_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("execution_id", "STRING", idempotency_value)
            ]
        )
        target_rows = [
            dict(value)
            for value in client.query(
                f"SELECT {names} FROM {tick}{target_ref}{tick} "
                f"WHERE {idempotency_column} = @execution_id",  # noqa: S608
                job_config=proof_config,
            ).result()
        ]
        if expected_rows is not None and len(target_rows) != expected_rows:
            raise RawLandingError("promotion target row count differs from evidence")
        observed_fingerprint = _canonical_rows_fingerprint(target_rows, columns)
        if (
            expected_content_fingerprint is not None
            and observed_fingerprint != expected_content_fingerprint
        ):
            raise RawLandingError("promotion target content fingerprint diverges")
        if source_exists:
            client.delete_table(source_ref, not_found_ok=True)
        return {
            "promoted": len(target_rows),
            "from": source,
            "into": table,
            "backend": "bigquery",
            "replayed": not source_exists,
            "content_fingerprint": observed_fingerprint,
            "schema_fingerprint": candidate_schema_fingerprint(columns),
        }

    raise RawLandingError(f"raw landing: unsupported backend {target!r}")


def discard_candidate(
    table: str,
    execution_id: str,
    *,
    project_id: str | None = None,
    backend: str | None = None,
) -> dict:
    """Drop a candidate's isolated relation. Nothing it held was ever readable."""
    source = candidate_table(table, execution_id)
    target = (backend or os.environ.get("TOOROW_DB_MODE") or "duckdb").strip().lower()

    if target == "duckdb":
        from core.warehouse_write import open_raw_writer  # noqa: PLC0415

        con = _duckdb_connection(open_raw_writer, project_id)
        try:
            con.execute(f"DROP TABLE IF EXISTS {source}")  # noqa: S608
        finally:
            con.close()
    elif target == "bigquery":
        from google.cloud import bigquery  # noqa: PLC0415

        from core import warehouse_tenancy  # noqa: PLC0415

        client = bigquery.Client(project=resolve_warehouse_project())
        dataset = warehouse_tenancy.bigquery_raw_dataset(project_id, conn=None)
        client.delete_table(f"{client.project}.{dataset}.{source}", not_found_ok=True)
    else:
        raise RawLandingError(f"raw landing: unsupported backend {target!r}")
    return {"discarded": source, "backend": target}


def _duckdb_connection(open_raw_writer, project_id):
    """A DuckDB connection on the org's raw schema (never the BigQuery writer)."""
    return open_raw_writer(os.environ.get("TOOROW_DUCKDB_PATH", ""), project_id=project_id)


def _validate(table: str, columns) -> None:
    if not _IDENTIFIER.match(table or ""):
        raise RawLandingError(f"raw landing table {table!r} is not an identifier")
    for name, kind in columns:
        if not _IDENTIFIER.match(name or ""):
            raise RawLandingError(f"raw landing column {name!r} is not an identifier")
        if kind not in _DUCKDB_TYPES:
            raise RawLandingError(f"raw landing type {kind!r} is not in the vocabulary")


def column_names(columns: list[tuple[str, str]]) -> list[str]:
    """The ordered column names of a raw-table declaration."""
    return [name for name, _ in columns]


def duckdb_ddl(table: str, columns: list[tuple[str, str]]) -> str:
    """The DuckDB ``CREATE TABLE`` for a declaration -- DERIVED, never hand-written.

    A module used to carry its raw table TWICE: a DDL string for the DuckDB path
    and a second `columns=` list for the BigQuery path. Measured 2026-08-17,
    seven of those pairs had drifted -- `google-ads` landed `metric_name` /
    `metric_value` / `currency` into BigQuery while its DDL and its dbt staging
    both read `metric` / `value_num` / `cost_source_currency`, so in the mode
    production actually runs (`TOOROW_DB_MODE=bigquery`) staging saw none of the
    columns that had landed.

    The repair is structural rather than a set of renames: there is now ONE
    declaration per raw table and both backends read it, so the two copies that
    drifted cannot both exist.
    """
    _validate(table, columns)
    body = ",\n    ".join(f"{name} {_DUCKDB_TYPES[kind]}" for name, kind in columns)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {body}\n)"


def duckdb_insert(table: str, columns: list[tuple[str, str]]) -> str:
    """The DuckDB ``INSERT`` whose column order matches `duckdb_ddl`'s declaration.

    Positional, and that is the point: the value tuple a connector builds is the
    same tuple the BigQuery path zips into row dicts (`row_from_values`), so a
    column added in one place cannot be forgotten in the other.
    """
    _validate(table, columns)
    names = column_names(columns)
    placeholders = ", ".join("?" for _ in names)
    return f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})"


def row_from_values(columns: list[tuple[str, str]], values: tuple) -> dict:
    """Zip one positional DuckDB value tuple into the row dict BigQuery expects.

    The two backends then carry the SAME names in the SAME order by construction,
    which is what removes the opportunity for a hand-transcribed BigQuery row dict
    to rename a column on its way out.
    """
    names = column_names(columns)
    if len(values) != len(names):
        raise RawLandingError(
            f"raw landing: {len(values)} values for {len(names)} declared columns"
        )
    return dict(zip(names, values))


def land_raw_rows(
    table: str,
    rows: list[dict],
    *,
    columns: list[tuple[str, str]],
    project_id: str | None = None,
    mode: str | None = None,
    backend: str | None = None,
    execution_id: str | None = None,
) -> dict:
    """Append *rows* to the raw table *table*, on whichever backend is configured.

    Passing *execution_id* lands into that execution's ISOLATED relation instead
    of the shared table -- see `candidate_table`. Staging never names it, so a
    candidate cannot reach the marts before someone publishes it.

    Args:
        table: raw table name, unqualified. The dataset/schema is resolved from
            *project_id* -- a connector never composes a warehouse location.
        rows: complete row dicts. A key absent from *columns* is ignored; a
            column absent from a row lands NULL.
        columns: ordered ``(name, type)`` pairs; type from the vocabulary above.
        project_id: the platform project, used to resolve the org's raw zone.
        mode: ``MODE_LOAD`` or ``MODE_STREAM``. BigQuery only; ignored on DuckDB,
            which has no such distinction.
        backend: overrides ``TOOROW_DB_MODE`` (test seam).

    Returns:
        ``{"rows": int, "backend": str, "mode": str | None, "table": str}``

    Raises:
        RawLandingError on an unknown backend, a bad identifier, or a write that
        the backend rejected. Never silently drops rows: a partial streaming
        failure raises with the per-row errors attached.
    """
    _validate(table, columns)
    target = (backend or os.environ.get("TOOROW_DB_MODE") or "duckdb").strip().lower()
    execution_id = execution_id or active_candidate_execution()
    if execution_id:
        table = candidate_table(table, execution_id)
        record_candidate_landing(execution_id, table, columns)
    capture = active_preview_capture()
    if capture is not None:
        capture["rows"].extend(rows)
        if table not in capture["tables"]:
            capture["tables"].append(table)
        return {"rows": len(rows), "backend": "preview", "mode": None, "table": table}

    if not rows:
        return {"rows": 0, "backend": target, "mode": None, "table": table}

    if target == "duckdb":
        return _land_duckdb(table, rows, columns, project_id)
    if target == "bigquery":
        return _land_bigquery(table, rows, columns, project_id, mode or default_write_mode())
    raise RawLandingError(f"raw landing: unsupported backend {target!r}")


def _land_duckdb(table, rows, columns, project_id) -> dict:
    from core.warehouse_write import open_raw_writer  # noqa: PLC0415

    names = column_names(columns)
    duckdb_path = os.environ.get("TOOROW_DUCKDB_PATH", "")

    con = open_raw_writer(duckdb_path, project_id=project_id)
    try:
        con.execute(duckdb_ddl(table, columns))
        con.executemany(
            duckdb_insert(table, columns),
            [tuple(row.get(name) for name in names) for row in rows],
        )
    finally:
        con.close()
    return {"rows": len(rows), "backend": "duckdb", "mode": None, "table": table}


# Protobuf field types for the Storage Write API, per column type. INT64/DOUBLE
# rather than the string forms: the descriptor IS the contract on the wire, and a
# number sent as text is accepted by BigQuery and then sorts wrong forever.
_PROTO_TYPES = {
    "STRING": "TYPE_STRING",
    "DATE": "TYPE_STRING",
    "TIMESTAMP": "TYPE_STRING",
    "FLOAT": "TYPE_DOUBLE",
    "INTEGER": "TYPE_INT64",
    "BOOLEAN": "TYPE_BOOL",
    # The Storage Write API has no JSON wire type: a JSON column is appended as the
    # JSON TEXT. The connectors already hold json.dumps() output, so nothing is
    # re-encoded here -- but the BigQuery column stays JSON, not STRING.
    "JSON": "TYPE_STRING",
}


def _proto_descriptor(columns):
    """Build a protobuf message descriptor matching *columns*, at runtime.

    The Storage Write API sends rows as serialized protobuf, so it needs a
    descriptor -- and a raw table's shape is decided per connector, so no static
    .proto file could cover them. The descriptor is therefore generated from the
    same column list the load path uses, which is what keeps the two transports
    from drifting into different schemas for the same table.
    """
    from google.protobuf import descriptor_pb2  # noqa: PLC0415

    proto = descriptor_pb2.DescriptorProto()
    proto.name = "RawRow"
    for index, (name, kind) in enumerate(columns, start=1):
        field = proto.field.add()
        field.name = name
        field.number = index
        # OPTIONAL, so a row that omits a column lands NULL rather than a zero --
        # the same behaviour the load path has.
        field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        field.type = getattr(descriptor_pb2.FieldDescriptorProto, _PROTO_TYPES[kind])
    return proto


def _write_via_storage_api(gcp_project, dataset, table, columns, payload) -> None:
    """Append rows through the BigQuery Storage Write API (protobuf over gRPC).

    Uses the DEFAULT stream, whose appends are committed as they are accepted --
    no explicit commit, and no stream to finalize. That is the right shape for an
    append-only raw zone: there is no transaction here to roll back, only rows
    that are in or are not.
    """
    try:
        from google.cloud import bigquery_storage_v1  # noqa: PLC0415
        from google.cloud.bigquery_storage_v1 import types as bqs_types  # noqa: PLC0415
        from google.protobuf import (  # noqa: PLC0415, E501
            descriptor_pb2,
            descriptor_pool,
            message_factory,
        )
    except ImportError as exc:
        # Never downgrade to MODE_STREAM here: it is billed differently and only
        # at-least-once, so a silent fallback would change both the bill and the
        # guarantee with nothing saying so.
        raise RawLandingError(
            "raw landing: storage_write requires google-cloud-bigquery-storage, "
            "which is not installed. Refusing to fall back to the legacy "
            "streaming API, which has different pricing and delivery semantics."
        ) from exc

    descriptor = _proto_descriptor(columns)

    # A private pool: the same message name is regenerated for every distinct raw
    # table, and the default pool would reject the second one as a duplicate.
    pool = descriptor_pool.DescriptorPool()
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = f"raw_landing_{table}.proto"
    file_proto.syntax = "proto2"
    file_proto.message_type.add().CopyFrom(descriptor)
    pool.Add(file_proto)
    message_class = message_factory.GetMessageClass(pool.FindMessageTypeByName("RawRow"))

    proto_rows = bqs_types.ProtoRows()
    for row in payload:
        message = message_class()
        for name, _ in columns:
            value = row.get(name)
            if value is not None:
                setattr(message, name, value)
        proto_rows.serialized_rows.append(message.SerializeToString())

    client = bigquery_storage_v1.BigQueryWriteClient()
    parent = client.table_path(gcp_project, dataset, table)

    # These are proto-plus wrappers, not raw protobuf messages: they are built by
    # constructor, and have no CopyFrom to mutate an existing instance with.
    request = bqs_types.AppendRowsRequest(
        write_stream=f"{parent}/streams/_default",
        proto_rows=bqs_types.AppendRowsRequest.ProtoData(
            writer_schema=bqs_types.ProtoSchema(proto_descriptor=descriptor),
            rows=proto_rows,
        ),
    )

    response = next(iter(client.append_rows(iter([request]))))
    if getattr(response, "error", None) and response.error.code:
        raise RawLandingError(f"raw landing: storage write rejected the append: {response.error}")
    if getattr(response, "row_errors", None):
        raise RawLandingError(f"raw landing: storage write rejected rows: {response.row_errors}")


def _land_bigquery(table, rows, columns, project_id, mode) -> dict:
    from google.cloud import bigquery  # noqa: PLC0415

    from core import warehouse_tenancy  # noqa: PLC0415

    if mode not in _MODES:
        raise RawLandingError(f"raw landing: unknown BigQuery write mode {mode!r}")

    client = bigquery.Client(project=resolve_warehouse_project())
    dataset = warehouse_tenancy.bigquery_raw_dataset(project_id, conn=None)
    table_id = f"{client.project}.{dataset}.{table}"
    schema = [bigquery.SchemaField(name, _BIGQUERY_TYPES[kind]) for name, kind in columns]

    # Both modes need the table to exist: streaming has no schema to autodetect
    # from, and letting a load job create it would make the first pull decide the
    # types for every pull after it.
    # THE ZONE IS CREATED IN EU, AND IT WAS NOT (AI-314, 2026-08-24). This call
    # passed a dataset ID and no location, so BigQuery applied its own default --
    # US -- while `warehouse_tenancy._provision_bigquery_datasets` pins EU on
    # every other dataset ("decision actée spike §4 + archi §6") and the nightly's
    # dbt profile queries in EU. BigQuery cannot read across locations, so the
    # nightly answered `Not found: Dataset toorow:raw_proj_… was not found in
    # location EU` on the only staging model the live Project has, and no mart of
    # its connector could ever be built. Measured 2026-08-24 with a dry run
    # (0 bytes) and confirmed on the datasets themselves: the two `raw_proj_*`
    # zones are US, the six other datasets of the platform are EU.
    #
    # This fixes the NEXT zone. The two that exist stay where they are -- a
    # dataset cannot change location, only be copied -- and that is an operational
    # act, not a code change.
    _raw_dataset = bigquery.Dataset(f"{client.project}.{dataset}")
    _raw_dataset.location = warehouse_tenancy.BIGQUERY_LOCATION
    client.create_dataset(_raw_dataset, exists_ok=True)
    client.create_table(bigquery.Table(table_id, schema=schema), exists_ok=True)

    names = [name for name, _ in columns]
    payload = [{name: row.get(name) for name in names} for row in rows]

    if mode == MODE_STORAGE_WRITE:
        _write_via_storage_api(client.project, dataset, table, columns, payload)
    elif mode == MODE_STREAM:
        errors = client.insert_rows_json(table_id, payload)
        if errors:
            # insert_rows_json reports per-row failures instead of raising, so a
            # partial write looks exactly like a success to a caller that does not
            # read the return value.
            raise RawLandingError(f"raw landing: streaming insert rejected rows: {errors!r}")
    else:
        job = client.load_table_from_json(
            payload,
            table_id,
            job_config=bigquery.LoadJobConfig(
                schema=schema,
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            ),
        )
        job.result()
        if job.errors:
            raise RawLandingError(f"raw landing: load job failed: {job.errors!r}")

    logger.info("raw_landing: table=%s backend=bigquery mode=%s rows=%d", table, mode, len(rows))
    return {"rows": len(rows), "backend": "bigquery", "mode": mode, "table": table}
