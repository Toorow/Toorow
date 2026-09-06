"""Google BigQuery connector — replicate a table the customer already owns.

The point of this module, said plainly because it is easy to confuse with the one
next to it: this REPLICATES. It reads a table in someone's BigQuery and lands the
rows in toorow's raw zone, so everything downstream — staging, marts, mapping,
governance, the Datastream Workbench — works exactly as it does for an API
connector. `core/external_bq_registration.py` (Story 12.7) does the opposite: it
registers an existing table read-only, in place, with a virtual pull and no data
movement. Both are legitimate; they answer different questions. Register when the
warehouse is the customer's system of record; replicate when the data must join
everything else toorow holds.

AD-2: the module name is never hardcoded in core/.
AD-3: no token is stored or logged. BigQuery IS a Google source, so it reads through
      the person's Google consent like every other one: `_get_bq_client` mints a fresh
      token from `nango_client` -- which routes a `google_direct` connection to
      `token_service`, never to Nango -- and holds it for the length of one call.
      Without a connection it falls back to the deployment's own Application Default
      Credentials, which is the self-hosted case and not the product path.
AD-7: pull_id is minted by the caller, never here.
AD-12: the MCP tool reads the mart, never raw_*.
AI-03: ASCII-only stdout.

FIELD DISCOVERY IS DYNAMIC, and the manifest says so rather than inventing a static
list. BigQuery has no fixed field catalog: the fields are the columns of the table
the operator selects, returned by `describe_table` at setup and carried in the
preview. Declaring a static list would be inventing one.

READ-ONLY BY CONSTRUCTION. Every statement this module can emit is a SELECT built
here from an identifier this module itself validated; no caller-supplied SQL is ever
executed. `_assert_safe_identifier` rejects anything that is not a plain
project.dataset.table reference, which is what stops a "table name" from carrying a
second statement.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp_app = FastMCP("bigquery")

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")

# A BigQuery identifier part: letters, digits, underscores and dashes only. Anything
# else -- a space, a quote, a semicolon, a backtick -- is refused before it can reach
# a query string.
_IDENTIFIER_PART = re.compile(r"^[A-Za-z0-9_-]+$")

# The bounded window a setup preview reads. Small on purpose: a preview exists to
# show shape and cost, not to move data.
_PREVIEW_ROW_LIMIT = 100

# Discovery walks someone's real estate, which can be large. These bounds keep the
# picker responsive; a dataset past the table bound is marked `truncated` so the
# screen can say so rather than quietly showing a partial list.
_MAX_DISCOVERY_PROJECTS = 50
_MAX_DISCOVERY_DATASETS = 200
_MAX_DISCOVERY_TABLES = 500

# How deep into nested STRUCTs the field catalog walks. Two levels reach every
# dimension of a GCP billing export; deeper is unbounded schema, not more choice.
_MAX_SCHEMA_DEPTH = 2


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


_ERROR_MAP: dict | None = None


def _load_error_map() -> dict:
    global _ERROR_MAP
    if _ERROR_MAP is None:
        manifest = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
        _ERROR_MAP = manifest.get("error_map") or {}
    return _ERROR_MAP


# ---------------------------------------------------------------------------
# Identifier safety -- the whole no-injection guarantee lives here
# ---------------------------------------------------------------------------


def assert_safe_table_reference(table_ref: str) -> tuple[str, str, str]:
    """Split and validate `project.dataset.table`, or refuse.

    A table reference arrives from discovery or from an operator's selection, and it
    is interpolated into SQL -- so it is validated here rather than trusted. Three
    parts, each matching `[A-Za-z0-9_-]+`. No quotes, no whitespace, no semicolons,
    no backticks: a reference that could close an identifier and open a statement
    never becomes one.
    """
    if not isinstance(table_ref, str) or table_ref.count(".") != 2:
        raise ValueError(
            "BigQuery table reference must be exactly project.dataset.table"
        )
    project, dataset, table = table_ref.split(".")
    for part, label in ((project, "project"), (dataset, "dataset"), (table, "table")):
        if not _IDENTIFIER_PART.match(part):
            raise ValueError(
                f"BigQuery {label} identifier {part!r} contains characters that are "
                "not allowed in an identifier"
            )
    return project, dataset, table


def _error_payload(exc: Exception, status: int):
    """Rebuild the BigQuery REST error envelope the client exception came from.

    `google.api_core.exceptions.GoogleAPICallError` keeps the `errors` array of
    the REST body -- `[{"reason": "rateLimitExceeded", "message": ...}, ...]` --
    on the exception, and that `reason` is the ONLY token in a BigQuery error
    that is not a copy of the HTTP status. Passing `str(exc)` alone (what this
    module did until 2026-08-17) handed the classifier a plain string, from which
    `core.pull_errors._extract_provider_codes` can extract nothing: the manifest
    map was consulted on every failure and could never match a single key.

    Returns the envelope as a dict when the exception carries reasons, and the
    message string otherwise -- so an exception without `errors` is preserved
    exactly as before, evidence included.
    """
    errors = getattr(exc, "errors", None)
    reasons = [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
    if not reasons:
        return str(exc)
    return {"error": {"code": status, "message": str(exc), "errors": reasons}}


def _classify_bigquery_error(exc: Exception):
    """Map a google-cloud-bigquery exception onto the canonical error taxonomy.

    The client raises typed exceptions rather than returning an HTTP response, so
    there is nothing for `classify_http_error` to read. This maps the status it
    carries, rebuilds the provider envelope (`_error_payload`) so the manifest
    `error_map` has a `reason` to key on, and defers to the shared classifier for
    the taxonomy itself.
    """
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        status = 500

    # 429 NEVER reaches classify_http_error -- it raises ValueError on purpose,
    # so that a rate limit cannot silently degrade into a retryable
    # `unclassified` that bypasses the breaker. Every other module keeps that
    # contract by branching on the response status BEFORE classifying; this one
    # could not, because the BigQuery client raises a typed exception instead of
    # returning a response, and nothing here looked at 429 at all. The result was
    # that a `TooManyRequests` from BigQuery raised ValueError out of the pull:
    # the breaker never recorded the limit, the job never re-queued, and the
    # operator saw a crash where the product had a documented recovery path.
    if status == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after = getattr(exc, "retry_after", None)
        return RateLimitError(
            platform="bigquery",
            retry_after=int(retry_after) if isinstance(retry_after, (int, float)) else None,
        )

    return classify_http_error(status, _error_payload(exc, status), _load_error_map())


# ---------------------------------------------------------------------------
# Discovery -- what this credential can actually read
# ---------------------------------------------------------------------------


def _get_bq_client(connection_id: str | None = None):
    """A BigQuery client reading AS the person, when a consent is named.

    `nango_client.get_fresh_token` is the single token door of the product, and
    its name is historical: it routes on the connection's `auth_path`, so a
    `google_direct` row is served by `token_service` -- decrypt, refresh, return
    -- and never reaches Nango. Every Google connector calls it the same way.

    `connection_id=None` falls back to Application Default Credentials: the
    deployment reading its own project. That is the self-hosted case, not the
    path a customer takes.
    """
    from core import nango_client  # noqa: PLC0415
    from google.cloud import bigquery  # noqa: PLC0415
    from google.oauth2.credentials import Credentials  # noqa: PLC0415

    creds = None
    if connection_id:
        token = nango_client.get_fresh_token(connection_id, provider="bigquery")
        creds = Credentials(token)

    return bigquery.Client(project=os.environ.get("GCP_PROJECT") or None, credentials=creds)


def verify_account(connection_id: str, account_id: str) -> dict | None:
    """Can this credential read THAT table? Ask BigQuery, do not consult the walk.

    `discover_accounts` says it plainly: "a project the principal cannot list
    simply does not appear". A client who grants access to ONE DATASET does not
    make their project enumerable -- `projects.list` needs a project-level
    permission that a dataset grant does not carry -- so the exact table a person
    was given became unreachable, while `get_table` on it answers.

    That is the ordinary shape of an external warehouse: someone shares a table,
    not an estate. The reference is parsed by the same guard the pull uses
    (`assert_safe_table_reference`), so nothing here widens what may be named.

    Returns the table and its reference when BigQuery answers, None when it
    refuses. A transport failure propagates -- it is not a refusal of access.
    """
    from google.api_core import exceptions as bq_exceptions  # noqa: PLC0415

    try:
        project_id, dataset_id, table_id = assert_safe_table_reference(account_id)
    except Exception:  # noqa: BLE001 -- a malformed reference is not an access answer
        return None

    client = _get_bq_client(connection_id)
    try:
        table = client.get_table(f"{project_id}.{dataset_id}.{table_id}")
    except (bq_exceptions.NotFound, bq_exceptions.Forbidden):
        return None
    return {
        "id": f"{table.project}.{table.dataset_id}.{table.table_id}",
        "label": f"{table.dataset_id}.{table.table_id}",
    }


def discover_accounts(connection_id: str | None = None) -> list[dict]:
    """List, as a walkable tree, every table this credential can actually read.

    Someone connects the table THEY have, so the picker has to show their real
    BigQuery: project -> dataset -> table, browsable. Returns the generic
    hierarchy core's topology flow consumes (the shape google-analytics returns
    for account -> property), NOT a wrapper dict -- core wraps it itself, and a
    dict here flattens to zero selectable accounts.

    Only a LEAF carries an ``id``. A project and a dataset are groupings you
    navigate, not things you can pull from: core stores every ``id`` it finds as
    a selectable account, so giving one to a folder would offer a choice that can
    only fail later at `assert_safe_table_reference`.

    Never an environment variable: a project the principal cannot list simply
    does not appear. Enumeration is bounded -- a large estate is truncated with
    ``truncated: true`` on the node rather than walked forever.
    """
    client = _get_bq_client(connection_id)

    # `list_datasets()` alone covers only the client's DEFAULT project. One
    # credential commonly reaches several, and the tables the person wants may be
    # in none of them -- so the projects are enumerated first.
    try:
        project_ids = [project.project_id for project in client.list_projects()]
    except Exception:  # noqa: BLE001 -- listing projects may be denied; the default still works
        project_ids = []
    default_project = client.project
    if default_project and default_project not in project_ids:
        project_ids.insert(0, default_project)

    tree: list[dict] = []
    for project_id in sorted(project_ids)[:_MAX_DISCOVERY_PROJECTS]:
        datasets: list[dict] = []
        try:
            listed = list(client.list_datasets(project=project_id))
        except Exception:  # noqa: BLE001 -- one unreadable project must not hide the others
            logger.info("bigquery_discover: cannot list datasets in %s", project_id)
            continue
        for dataset in listed[:_MAX_DISCOVERY_DATASETS]:
            dataset_id = dataset.dataset_id
            try:
                listed_tables = list(
                    client.list_tables(
                        f"{project_id}.{dataset_id}", max_results=_MAX_DISCOVERY_TABLES + 1
                    )
                )
            except Exception:  # noqa: BLE001 -- same, per dataset
                logger.info("bigquery_discover: cannot list tables in %s", dataset_id)
                continue
            truncated = len(listed_tables) > _MAX_DISCOVERY_TABLES
            tables = [
                {
                    "id": f"{project_id}.{dataset_id}.{table.table_id}",
                    "label": table.table_id,
                    "kind": "table",
                    "table_type": table.table_type,
                }
                for table in listed_tables[:_MAX_DISCOVERY_TABLES]
            ]
            datasets.append(
                {
                    "label": dataset_id,
                    "kind": "dataset",
                    "truncated": truncated,
                    "children": tables,
                }
            )
        tree.append(
            {
                "label": project_id,
                "kind": "project",
                "children": datasets,
            }
        )
    return tree


def describe_table(table_ref: str, connection_id: str | None = None) -> dict:
    """Return the column schema, row count and byte size of one table.

    This is the field catalog for this connector: BigQuery has no fixed list of
    fields, the fields ARE the columns of the table the operator selected.
    """
    assert_safe_table_reference(table_ref)
    client = _get_bq_client(connection_id)
    table = client.get_table(table_ref)
    return {
        "table_ref": table_ref,
        "location": table.location,
        "num_rows": table.num_rows,
        "num_bytes": table.num_bytes,
        "schema": _flatten_schema(table.schema),
        # The choices the person makes after picking the table, each narrowed to
        # the columns that can actually serve. Offering every column for the date
        # is how someone picks a STRING id and gets an unexplained empty pull.
        # `entity_key` and `version` are the two a VERSIONED HISTORY table needs:
        # a Fivetran-style landing writes one row per entity PER SYNC BATCH, and
        # nothing but an operator's declaration can say which column identifies
        # the entity and which one orders the batches.
        "candidates": {
            role: [f["name"] for f in _flatten_schema(table.schema) if role in f["roles"]]
            for role in _SELECTOR_ROLES
        },
    }


def _flatten_schema(schema, prefix: str = "", depth: int = 0) -> list[dict]:
    """Flatten a table's columns, walking into STRUCTs, into selectable paths.

    A BigQuery table's useful dimensions are routinely nested -- in a GCP billing
    export EVERY one of them is (`service.description`, `sku.description`,
    `project.id`), so a selector that only offers top-level columns offers cost
    per day and nothing to break it down by.

    A RECORD is a folder: it is listed so the picker can show the grouping, but it
    is not selectable itself. A REPEATED field is an array, not a scalar, so
    neither it nor anything under it can be a breakdown or a value without an
    UNNEST this connector deliberately does not emit.
    """
    fields: list[dict] = []
    for field in schema:
        path = f"{prefix}{field.name}"
        mode = str(getattr(field, "mode", "") or "NULLABLE").upper()
        repeated = mode == "REPEATED"
        is_record = (field.field_type or "").upper() in {"RECORD", "STRUCT"}
        roles = [] if (repeated or is_record) else _column_roles(field.field_type)
        fields.append(
            {
                "field_id": path,
                "name": path,
                "type": field.field_type,
                "nullable": getattr(field, "is_nullable", True),
                "repeated": repeated,
                "roles": roles,
                # The BigQuery mode and the schema's own description, carried so a
                # reader can SAY what a column is rather than infer it. `REPEATED
                # RECORD` is a statement about the column; a screen that flattened
                # it away would offer an array as if it were a value.
                "mode": mode,
                "description": getattr(field, "description", None),
            }
        )
        if is_record and not repeated and depth < _MAX_SCHEMA_DEPTH:
            fields.extend(_flatten_schema(field.fields, f"{path}.", depth + 1))
    return fields


# A column's BigQuery type decides which selector roles it can fill. Kept as one
# map so the picker and the pull agree on what is offerable.
_DATE_TYPES = {"DATE", "DATETIME", "TIMESTAMP"}
_NUMERIC_TYPES = {"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"}
_LABEL_TYPES = {"STRING", "BOOL", "BOOLEAN"} | _DATE_TYPES

# A VERSION has to ORDER the sync batches, so it is an instant or a number. A
# STRING version sorts "v10" before "v9", which is an ordering nobody declared --
# so a STRING is refused rather than sorted on a rule the operator never chose.
_VERSION_TYPES = _DATE_TYPES | _NUMERIC_TYPES

# An ENTITY KEY has to IDENTIFY the row across batches. A FLOAT identifies
# nothing (it is a measurement), and a BOOL names two entities at most.
_ENTITY_KEY_TYPES = {"STRING", "INTEGER", "INT64", "NUMERIC", "BIGNUMERIC"} | _DATE_TYPES

# Every role the selector offers, in the order a person meets them: the window
# first, then what is measured and how it is split, then -- only for a versioned
# history table -- what identifies a row and what orders its versions.
_SELECTOR_ROLES = ("date", "value", "breakdown", "entity_key", "version")


def _column_roles(field_type: str) -> list[str]:
    """Which selector roles a column of this type can fill."""
    upper = (field_type or "").upper()
    roles: list[str] = []
    if upper in _DATE_TYPES:
        roles.append("date")
    if upper in _NUMERIC_TYPES:
        roles.append("value")
    if upper in _LABEL_TYPES:
        roles.append("breakdown")
    if upper in _ENTITY_KEY_TYPES:
        roles.append("entity_key")
    if upper in _VERSION_TYPES:
        roles.append("version")
    return roles


# How many objects one dataset's discovery walk can list, and how deep the field
# catalog goes. Exported because a reader OUTSIDE this module has to be able to
# say whether a listing is COMPLETE, and the honest answer depends on this exact
# number. `discover_accounts` marks a dataset `truncated` when it hits the bound,
# but only the LEAF is persisted as a Source Account, so the flag never survives
# the trip; core re-derives it from this value rather than restating a literal
# that would disagree the day the walk changes.
MAX_DISCOVERY_TABLES_PER_DATASET = _MAX_DISCOVERY_TABLES
MAX_SCHEMA_DEPTH = _MAX_SCHEMA_DEPTH


class BigQuerySetupReader:
    """Read-only table metadata for Datastream setup (Story 57.1).

    Satisfies `inbound.adapters.datastream_setup.BigQuerySetupClient`. It opens NO
    second way into BigQuery: `describe_table` already reads the schema,
    `_flatten_schema` already decides what a nested column is worth, and the
    dry-run above is already how this module prices a scan. This class only shapes
    what they return into what the adapter reads, and adds the one call the
    Protocol did not have -- the estimate.

    NOTHING here reads a row. `get_table` is a metadata call BigQuery does not
    bill, and the estimate is a job that is planned and thrown away.

    THE AUTHORIZATION IS BOUND AT CONSTRUCTION, per request, exactly like
    `SheetsSetupReader`. BigQuery is a Google source, so it reads through the
    person's Google consent -- not through the deployment's own credentials. A
    reader whose methods each took an optional `connection_id` would let one call
    of a walk read as the person and the next as the service account, which is
    two different answers to "what can I see" inside one screen.

    `connection_id` is `None` only where there is genuinely no consent to use:
    a deployment reading its own project with Application Default Credentials.
    """

    def __init__(self, connection_id: str | None = None) -> None:
        self._connection_id = connection_id

    def get_table_metadata(self, object_ref: str) -> dict:
        """Schema, location and watermark candidate for one table or view.

        DESCRIBING IS NOT OFFERING, and the first version of this method confused
        the two: it filtered on `roles`, so no RECORD and no REPEATED field was
        ever emitted. An operator whose dimensions are all nested -- which is
        every GCP billing export -- saw a table missing half its columns, and
        nothing said why. The `roles` filter belongs to COLUMN SELECTION, where a
        folder must not be pickable; a schema description states what is there.

        So everything the catalog walked is emitted, with its real BigQuery mode:
        a `REPEATED RECORD` reads as one. Nothing can select it, because
        `_normalized_field_universe` refuses container fields by the shared rule
        in `core.datastream_setup_observations.is_container_field`.
        """
        described = describe_table(object_ref, self._connection_id)
        catalog = list(described.get("schema") or [])
        fields = [
            {
                "name": entry["name"],
                "field_id": entry["field_id"],
                "type": entry.get("type"),
                "nullable": bool(entry.get("nullable", True)),
                "mode": entry.get("mode"),
                "description": entry.get("description"),
            }
            for entry in catalog
        ]
        # Described above, and NOT selectable: counted so the screen can mark the
        # rows rather than leave an operator to guess which ones it will refuse.
        unselectable = [entry for entry in catalog if not entry.get("roles")]
        date_candidates = list((described.get("candidates") or {}).get("date") or [])
        return {
            "fields": fields,
            "location": described.get("location"),
            # ONE candidate is an answer; two are a question nobody was asked.
            # Silently taking the first of two date columns is a decision made on
            # the operator's behalf that they would never see.
            "watermark": date_candidates[0] if len(date_candidates) == 1 else None,
            # NEVER a fabricated instant. Freshness is what a read observed, and
            # no read has happened: a value here would be invented, and `0` or
            # "now" would both read as evidence.
            "freshness": None,
            # Described but not selectable, said rather than dropped. A schema
            # deeper than the catalog walks is the one thing that really is
            # missing from the list, and it is stated too.
            "unselectable_field_count": len(unselectable),
            "schema_depth_truncated": _schema_depth_truncated(catalog),
            "schema_depth_limit": _MAX_SCHEMA_DEPTH,
        }

    def estimate_scan_bytes(self, object_ref: str) -> int | None:
        """Bytes a read of this object would scan, planned without executing it.

        The BigQuery **dry-run query job** is the API that answers this: it plans
        the statement, reads no byte, and is not billed. The statement it plans is
        the `sample` step of the read-only probe plan -- the exact instruction a
        read would run -- so the number is the read's, not an approximation of it.

        Returns `None` when the estimate cannot be had. Never `0`: a zero here
        would be read as "this read is free", which is a claim nobody measured.
        """
        from core.external_bq_registration import build_probe_plan  # noqa: PLC0415
        from google.cloud import bigquery  # noqa: PLC0415

        project, dataset, obj = assert_safe_table_reference(object_ref)
        plan = build_probe_plan(
            {"project": project, "dataset": dataset, "object": obj},
            sample_limit=_PREVIEW_ROW_LIMIT,
        )
        sample = next(step for step in plan["steps"] if step["name"] == "sample")
        client = _get_bq_client(self._connection_id)
        try:
            # `dry_run=True` is what makes this an estimate; `use_query_cache=False`
            # is what stops a cached plan from reporting zero bytes for a table
            # that would really be scanned. Neither the job nor its result is ever
            # fetched -- calling `result()` here would BE the scan.
            estimate = client.query(
                sample["sql"],
                job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
            )
        except Exception as exc:  # noqa: BLE001 -- an absent estimate is an answer
            logger.info("bigquery_setup: scan estimate unavailable for %s: %s", object_ref, exc)
            return None
        total = getattr(estimate, "total_bytes_processed", None)
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            return None
        return int(total)


def _schema_depth_truncated(catalog: list[dict]) -> bool:
    """Did the field catalog stop before the schema did?

    `_flatten_schema` walks two levels. A non-repeated RECORD with no descendant
    in the flattened list is a folder whose contents were never read -- either
    because the depth bound cut it, or because it is empty. Both cases mean the
    list on screen is not the whole object, and the screen has to say so.
    """
    for entry in catalog:
        is_record = str(entry.get("type") or "").upper() in {"RECORD", "STRUCT"}
        if not is_record or entry.get("repeated"):
            continue
        prefix = f"{entry['name']}."
        if not any(other["name"].startswith(prefix) for other in catalog):
            return True
    return False


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

_RAW_TABLE = "raw_bigquery_daily"

# The raw table declared ONCE, in the shared vocabulary, so DuckDB DDL and the
# BigQuery schema cannot drift apart into two dialects of the same table.
_RAW_COLUMNS = [
    ("date", "STRING"),
    ("metric", "STRING"),
    ("value", "FLOAT"),
    ("breakdown_dimension", "STRING"),
    ("breakdown_value", "STRING"),
    ("pull_id", "STRING"),
    ("loaded_at", "STRING"),
    ("project_id", "STRING"),
]


def _insert_raw_rows(
    rows: list[dict], pull_id: str, project_id: str, write_mode: str | None = None
) -> int:
    """Append canonical long-format rows to the raw zone, on either backend.

    Goes through `core.raw_landing` rather than opening DuckDB directly, so this
    connector lands on BigQuery too -- by batch load job or by streaming insert,
    which is `write_mode`. Passing None takes the deployment's default.
    """
    from core.raw_landing import land_raw_rows  # noqa: PLC0415 -- AD-2

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    # The manifest's declared method wins over the deployment default. A caller
    # may still override it explicitly; what it must not do is be ignored, which
    # is what a declaration nothing reads amounts to.
    resolved_mode = write_mode or _declared_write_method()
    landed = land_raw_rows(
        _RAW_TABLE,
        [
            {
                "date": str(row.get("date", "")),
                "metric": str(row.get("metric", "")),
                "value": float(row.get("value", 0.0) or 0.0),
                "breakdown_dimension": str(row.get("breakdown_dimension", "")),
                "breakdown_value": str(row.get("breakdown_value", "")),
                "pull_id": pull_id,
                "loaded_at": loaded_at,
                "project_id": project_id,
            }
            for row in rows
        ],
        columns=_RAW_COLUMNS,
        project_id=project_id,
        mode=resolved_mode,
    )
    return landed["rows"]


def _declared_write_method() -> str | None:
    """The `raw_landing.method` this connector declares, or None if it declares none."""
    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
    return (manifest.get("raw_landing") or {}).get("method")


# ---------------------------------------------------------------------------
# pull()
# ---------------------------------------------------------------------------


def _date_param_type(client, table_ref: str, date_column: str) -> str:
    """The query-parameter type matching the chosen date column's own type.

    A metadata read (tables.get), which BigQuery does not bill. Falls back to
    STRING only when the column cannot be resolved -- the pull then fails on a
    type mismatch it can name, rather than on a silently empty window.
    """
    try:
        table = client.get_table(table_ref)
        for field in _flatten_schema(table.schema):
            if field["name"] == date_column:
                upper = (field["type"] or "").upper()
                return upper if upper in _DATE_TYPES else "STRING"
    except Exception:  # noqa: BLE001 -- the query below reports the real failure
        logger.info("bigquery_pull: could not type date column %s", date_column)
    return "STRING"


def quote_column_path(path: str) -> str:
    """Quote a possibly-nested column path for SQL, validating every part.

    `service.description` becomes `` `service`.`description` `` -- each segment
    backticked separately, because backticking the whole thing names a single
    column that happens to contain a dot. Every segment must still be a bare
    identifier, so nesting widens what can be SELECTED without widening what can
    be INJECTED.
    """
    parts = (path or "").split(".")
    if not parts or any(not _IDENTIFIER_PART.match(part) for part in parts):
        raise ValueError(f"BigQuery column path {path!r} is not an identifier path")
    if len(parts) > _MAX_SCHEMA_DEPTH + 1:
        raise ValueError(f"BigQuery column path {path!r} is nested deeper than the catalog")
    return ".".join(f"`{part}`" for part in parts)


def resolve_column(row: dict, path: str):
    """Read a possibly-nested value out of a returned row."""
    value = row
    for part in (path or "").split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


# The keys a history-deduplication declaration carries, named once here and
# declared in the manifest so nothing reads a key the contract does not describe.
HISTORY_DEDUP_KEYS = ("entity_key_columns", "version_column", "label_columns")


def _declared_columns(value, what: str) -> list[str]:
    """One column name or a list of them, as a list. Anything else is refused."""
    if isinstance(value, str):
        names = [value]
    elif isinstance(value, (list, tuple)):
        names = list(value)
    else:
        raise ValueError(
            f"BigQuery history deduplication expects {what} to name a column, or a "
            f"list of columns; it received {type(value).__name__}"
        )
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"BigQuery history deduplication expects every {what} entry to be a "
                "column name; one entry is empty"
            )
    return [name.strip() for name in names]


def resolve_history_dedup(declaration, schema_fields: list[dict]) -> dict | None:
    """Read a history declaration against THIS table's schema, or refuse by name.

    Read here rather than in `build_select` because a refusal has to name a column
    the operator can recognise, and only the table's own schema can say whether a
    name is one of its columns. This runs before any statement is sent, so a
    mistyped column is a refusal and never an empty window nobody can explain.

    Returns None when nothing is declared -- a flat table is read exactly as it was
    before this existed.
    """
    if not declaration:
        return None
    if not isinstance(declaration, dict):
        raise ValueError(
            "BigQuery history deduplication expects a declaration naming "
            f"{', '.join(HISTORY_DEDUP_KEYS)}; it received "
            f"{type(declaration).__name__}"
        )
    unknown = sorted(set(declaration) - set(HISTORY_DEDUP_KEYS))
    if unknown:
        raise ValueError(
            f"BigQuery history deduplication does not declare {', '.join(unknown)}; "
            f"it declares {', '.join(HISTORY_DEDUP_KEYS)}"
        )

    entity_key_columns = _declared_columns(
        declaration.get("entity_key_columns"), "entity_key_columns"
    )
    version_column = _declared_columns(declaration.get("version_column"), "version_column")
    if len(version_column) != 1:
        raise ValueError(
            "BigQuery history deduplication orders on exactly ONE version column; "
            f"{len(version_column)} were declared"
        )
    label_columns = (
        _declared_columns(declaration.get("label_columns"), "label_columns")
        if declaration.get("label_columns")
        else []
    )

    roles_by_name = {field["name"]: set(field["roles"]) for field in schema_fields}

    def _require(name: str, role: str, geste: str) -> None:
        if name not in roles_by_name:
            raise ValueError(
                f"BigQuery history deduplication names {name!r}, which is not a "
                "column of this table -- pick one of the columns the table describes"
            )
        if role not in roles_by_name[name]:
            raise ValueError(
                f"BigQuery history deduplication cannot use {name!r} as its {role} "
                f"-- {geste}"
            )

    for name in entity_key_columns:
        _require(
            name,
            "entity_key",
            "pick a text, integer or date column that identifies the row across "
            "sync batches",
        )
    _require(
        version_column[0],
        "version",
        "pick a timestamp or numeric column that orders the sync batches; this one "
        "cannot be ordered",
    )
    for name in label_columns:
        _require(name, "breakdown", "pick a text, boolean or date column")
        # The last non-NULL value REPLACES the column in the projection, and a
        # star-replacement names a top-level column: a nested label would have to
        # rebuild its whole STRUCT, which is not a statement this module emits.
        if "." in name:
            raise ValueError(
                f"BigQuery history deduplication cannot carry the nested label "
                f"{name!r} -- pick a top-level column"
            )

    return {
        "entity_key_columns": entity_key_columns,
        "version_column": version_column[0],
        "label_columns": label_columns,
    }


def build_select(
    table_ref: str,
    date_column: str,
    date_from: str,
    date_to: str,
    limit: int | None = None,
    history_dedup: dict | None = None,
) -> str:
    """Build the only shape of statement this module ever emits.

    Exported so a test can assert on it: no caller SQL is executed, the identifier
    is validated, and the window is always bounded by the requested dates.

    TWO WINDOWS, NOT ONE, when a versioned history is declared -- and that is the
    whole point. `ROW_NUMBER() ... = 1` in the QUALIFY keeps the BODY of the most
    recent version of each entity. `LAST_VALUE(<label> IGNORE NULLS)` over the
    whole partition keeps each declared LABEL's most recent non-NULL value, which
    is a different row whenever a sync batch wrote the label NULL. A single window
    cannot do both: taking "the latest version" alone drops every label the last
    batch blanked, silently, and that shortcut is what this exists to refuse.

    `history_dedup=None` changes nothing: a flat table produces the statement it
    always produced, character for character.
    """
    assert_safe_table_reference(table_ref)
    date_sql = quote_column_path(date_column)
    projection = "*"
    qualify = ""
    if history_dedup:
        partition_sql = ", ".join(
            quote_column_path(name) for name in history_dedup["entity_key_columns"]
        )
        version_sql = quote_column_path(history_dedup["version_column"])
        labels = history_dedup.get("label_columns") or []
        if labels:
            replacements = ", ".join(
                f"LAST_VALUE({quote_column_path(name)} IGNORE NULLS) OVER "
                f"(PARTITION BY {partition_sql} ORDER BY {version_sql} "
                f"ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) "
                f"AS {quote_column_path(name)}"
                for name in labels
            )
            projection = f"* REPLACE ({replacements})"
        qualify = (
            f" QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition_sql} "
            f"ORDER BY {version_sql} DESC) = 1"
        )
    sql = (
        f"SELECT {projection} FROM `{table_ref}` "
        f"WHERE {date_sql} >= @date_from AND {date_sql} < @date_to"
        f"{qualify}"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql


def pull(
    connection_id: str | None = None,
    date_from: str = "",
    date_to: str = "",
    project_id: str = "",
    pull_id: str | None = None,
    table_ref: str = "",
    date_column: str = "date",
    value_column: str = "",
    metric_name: str = "",
    breakdown_column: str = "",
    history_dedup: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Read one BigQuery table over a bounded window and land it in raw.

    `dry_run=True` fetches the same rows through the same query and RETURNS them
    instead of landing them: `_insert_raw_rows` is not called, so nothing reaches
    raw, staging or the marts. That is what a setup preview needs, and it is why a
    preview does not require the execution-isolation work a CANDIDATE does.
    Landing stays the default.

    `history_dedup` is the operator's declaration that this table is a VERSIONED
    HISTORY -- which entity key its rows repeat on, which column orders the sync
    batches, and which labels a batch may have blanked. It is never guessed from a
    column name: absent, the table is read flat, exactly as before.

    AD-7: `pull_id` comes from the caller. In a dry run there is no pull, so there
    is no pull_id -- the result says `None` rather than inventing one.
    """
    from google.cloud import bigquery  # noqa: PLC0415

    if not table_ref:
        raise ValueError("BigQuery pull requires a table_ref (project.dataset.table)")
    if not dry_run and not pull_id:
        raise ValueError("BigQuery pull requires a caller-minted pull_id (AD-7)")

    client = _get_bq_client(connection_id)
    # READ BEFORE ASKING. The declaration is checked against the table's own schema
    # -- a metadata read BigQuery does not bill -- so a column that is not there is
    # named now, and never becomes a statement that runs and returns the wrong rows.
    resolved_dedup = None
    if history_dedup:
        try:
            described = client.get_table(table_ref)
        except Exception as exc:  # noqa: BLE001 -- re-raised as a typed connector error
            raise _classify_bigquery_error(exc) from exc
        resolved_dedup = resolve_history_dedup(history_dedup, _flatten_schema(described.schema))

    # Two statements, and the difference between them is the point. `priced_sql` is
    # the UNBOUNDED window -- what a real pull of this window will scan, which is the
    # number worth showing someone. `sql` is what this call actually executes: the
    # same statement, bounded to a sample when previewing.
    priced_sql = build_select(
        table_ref, date_column, date_from, date_to, history_dedup=resolved_dedup
    )
    sql = (
        build_select(
            table_ref,
            date_column,
            date_from,
            date_to,
            limit=_PREVIEW_ROW_LIMIT,
            history_dedup=resolved_dedup,
        )
        if dry_run
        else priced_sql
    )
    # The window parameter is typed to MATCH the column, never cast onto it. A
    # billing export's date column is a TIMESTAMP and the table is partitioned on
    # it; wrapping the column in a CAST to make a STRING comparison work would
    # discard the partition pruning and scan the whole export every pull.
    param_type = _date_param_type(client, table_ref, date_column)
    params = [
        bigquery.ScalarQueryParameter("date_from", param_type, date_from),
        bigquery.ScalarQueryParameter("date_to", param_type, date_to),
    ]
    try:
        # Priced FIRST, and priced separately. BigQuery's own dry-run job plans the
        # statement without scanning a byte and is not billed -- so the estimate is
        # known BEFORE the scan is paid for, which is what the quota note promises.
        # Reading `total_bytes_processed` off the executed job cannot do that: it is
        # the bill, arriving after it is due.
        estimate = client.query(
            priced_sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params, dry_run=True, use_query_cache=False
            ),
        )
        bytes_estimated = getattr(estimate, "total_bytes_processed", None)

        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
        api_rows = [dict(row) for row in job.result()]
        bytes_processed = getattr(job, "total_bytes_processed", None)
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed connector error
        raise _classify_bigquery_error(exc) from exc

    canonical_rows = transform(
        api_rows,
        date_column=date_column,
        value_column=value_column,
        metric_name=metric_name or table_ref.rsplit(".", 1)[-1],
        breakdown_column=breakdown_column,
    )

    if dry_run:
        logger.info(
            "bigquery_pull_dry_run: table=%s rows=%d (nothing landed)",
            table_ref,
            len(canonical_rows),
        )
        return {
            "pull_id": None,
            "dry_run": True,
            "row_count": len(canonical_rows),
            "rows": canonical_rows,
            "schema": sorted({key for row in canonical_rows for key in row}),
            # What the full window WOULD scan, versus what this bounded sample did.
            "bytes_estimated": bytes_estimated,
            "bytes_processed": bytes_processed,
            "table_ref": table_ref,
            "date_from": date_from,
            "date_to": date_to,
        }

    row_count = _insert_raw_rows(canonical_rows, pull_id, project_id)
    logger.info(
        "bigquery_pull_completed: pull_id=%s table=%s row_count=%d",
        pull_id,
        table_ref,
        row_count,
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "bytes_estimated": bytes_estimated,
        "bytes_processed": bytes_processed,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# transform()
# ---------------------------------------------------------------------------


def transform(
    raw_rows: list[dict],
    date_column: str = "date",
    value_column: str = "",
    metric_name: str = "value",
    breakdown_column: str = "",
) -> list[dict]:
    """Fold arbitrary table columns into the canonical long format.

    A BigQuery table has whatever columns it has, and the raw zone is long format
    (date, metric, value, breakdown). Which column carries the number is an operator
    decision, made once at setup and carried in the plan -- it is never guessed here.
    When `value_column` is empty EVERY numeric column becomes its own metric, which
    is the honest default for a table nobody has mapped yet.

    AD-4: values are stored raw. Nothing is summed, averaged or rounded here.
    """
    result: list[dict] = []
    for row in raw_rows:
        date_value = resolve_column(row, date_column)
        # [:10] holds the date grain: a TIMESTAMP column (every date column in a
        # billing export is one) would otherwise carry an hour into a warehouse
        # whose invariant is one row per DAY.
        date_text = "" if date_value is None else str(date_value)[:10]
        raw_breakdown = resolve_column(row, breakdown_column) if breakdown_column else None
        breakdown_value = "" if raw_breakdown is None else str(raw_breakdown)

        if value_column:
            columns = [(metric_name, resolve_column(row, value_column))]
        else:
            columns = [
                (name, value)
                for name, value in row.items()
                if name not in (date_column, breakdown_column)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ]

        for name, value in columns:
            if value is None:
                continue
            result.append(
                {
                    "date": date_text,
                    "metric": name,
                    "value": float(value),
                    "breakdown_dimension": breakdown_column or "",
                    "breakdown_value": breakdown_value,
                }
            )
    return result


# ---------------------------------------------------------------------------
# MCP tool -- reads the mart (AD-12)
# ---------------------------------------------------------------------------


@mcp_app.tool()
def get_bigquery_report(
    project_id: str = "default",
    report_profile: str = "table_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Replicated BigQuery table report — reads fact_daily_kpi, never raw_*.

    Parameters:
        project_id: Project identifier.
        report_profile: Report profile id from the manifest.
        date_from: Start date ISO-8601. Defaults to 90 days ago.
        date_to: End date ISO-8601. Defaults to yesterday.
    """
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    from core.warehouse import query_report  # noqa: PLC0415

    # AD-4: the shared reader routes additive metrics to fact_daily_kpi and
    # non-additive ones to their semantic view. A replicated table declares no
    # non-additive metric until someone maps one, so this asks for what it landed.
    rows = query_report(
        project_id=project_id,
        connector="bigquery",
        metrics=[],
        dimensions=["date", "metric"],
        start_date=date_from,
        end_date=date_to,
        include_prior_period=False,
    )
    metrics: dict[str, float] = {}
    for row in rows:
        metric = str(row.get("metric", ""))
        metrics[metric] = metrics.get(metric, 0.0) + float(row.get("value", 0.0) or 0.0)
    return {
        "schema_version": "1",
        "meta": {
            "freshness": rows[-1].get("date") if rows else None,
            "provenance": [{"source_system": "bigquery", "source_field": "replicated_table"}],
            "alerts": [] if rows else [
                {"level": "warning", "message": "No replicated rows in this window."}
            ],
        },
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": metrics,
        },
    }
