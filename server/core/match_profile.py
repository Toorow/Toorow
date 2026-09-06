"""Story 66.3 -- what a proposed cross would actually do to the rows.

WHY THIS EXISTS. Story 66.2 says a cross is GOVERNED; that is a statement about
authority and it is silent about data. A governed relationship over two sources
that share four days out of ninety produces a legal, approved, catastrophically
wrong answer. So before anything executes, this module asks the warehouse one
combined question per side and one across them:

    how many rows, how many MATCHABLE distinct key tuples, how many null keys,
    how many keys appear more than once -- and how many keys are on BOTH sides

and turns the answers into the one number a person actually needs: **how much
would the row count multiply.**

EVERY FIGURE CARRIES HOW IT WAS OBTAINED. `exact` means a warehouse query
returned it. `estimated` means the read hit its row cap and the figure is a
lower bound. `unavailable` means we could not ask -- and `unavailable` is never
rendered as zero, never rendered as safe and never allowed to become a passing
state. That is the whole point of separating the three: "no unmatched rows" and
"nobody counted the unmatched rows" are opposite facts that look identical when
one of them is written as `0`.

WHAT IT REFUSES BEFORE IT ASKS. A key component with no physical binding on one
side, a Datastream with no published Output, and a column the relation does not
actually carry are refusals by name -- not empty profiles. A profile that came
back empty because the column was misspelled would read as "these sources share
nothing", which is a sentence about the business and not about the mistake.

IDENTIFIERS ARE ALLOWLISTED, VALUES ARE BOUND. The identifier guard is imported
from `query_execution` rather than retyped: two guards would be two answers to
"what may reach the warehouse as a name", and the weaker one would win the day
somebody edited it.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import time
from typing import Any, Callable, Mapping, Sequence

from core.query_execution import (
    _dataset_for,
    _safe_identifier,
    names_a_readable_relation,
)

logger = logging.getLogger(__name__)

#: A profile never scans beyond this. It is a bound on the ANSWER, not on the
#: table: the counts below are aggregates, so the cap applies to the number of
#: distinct key groups a duplicate/multiplication probe brings back.
MAX_KEY_GROUPS = 10_000

#: How the three evidence states are named everywhere in this epic.
EXACT, ESTIMATED, UNAVAILABLE = "exact", "estimated", "unavailable"
PROFILE_RECEIPT_SCHEMA_VERSION = "match-profile-receipt.v1"
MAX_PROFILE_RECEIPT_BYTES = 65_536
PROFILE_RECEIPT_MAX_AGE_SECONDS = 120
_FILTER_OPERATORS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


class ProfileRefused(ValueError):
    """The profile cannot be produced, and the reason names what to repair."""

    def __init__(self, code: str, message: str, *, missing_link: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.missing_link = missing_link


class ProfileReceiptInvalid(ValueError):
    """A supplied receipt cannot replace a fresh warehouse measurement."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _receipt_secret() -> bytes:
    """Derive a purpose-specific key from the configured server sidecar secret."""
    from core.analyze_feedback import feedback_context_secret  # noqa: PLC0415

    return hmac.digest(
        feedback_context_secret(), PROFILE_RECEIPT_SCHEMA_VERSION.encode(), "sha256"
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    try:
        raw = value.encode("ascii")
        decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except (UnicodeError, ValueError) as exc:
        raise ProfileReceiptInvalid("The matching profile receipt is not valid.") from exc
    if _b64encode(decoded) != value:
        raise ProfileReceiptInvalid("The matching profile receipt is not valid.")
    return decoded


def _relationship_block(
    conn,
    *,
    project_id: str,
    relationship_name: str | None,
    view_version_id: str | None,
) -> dict[str, str] | None:
    """The EXECUTOR's refusal for this published relationship, or `None`.

    The rule itself is `multi_source_plan.unsupported_relationship` -- imported,
    never restated, because a second copy of it is exactly how the match
    catalogue and the compiler came to disagree (repaired 2026-08-16).

    A relationship that cannot be read answers `None`, and that is deliberate:
    this function reports an executor OBJECTION, and "we could not check" is not
    one. The compiler remains the authority that refuses; nothing here lets
    anything run that it would stop.
    """
    if not relationship_name or not view_version_id:
        return None

    from core.multi_source_plan import unsupported_relationship  # noqa: PLC0415

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.cardinality_type, r.fan_out_policy, r.bridge_dataset
                  FROM app.semantic_view_version_relationships r
                  JOIN app.semantic_view_versions v ON v.id = r.view_version_id
                 WHERE v.project_id = %(project_id)s
                   AND v.status = 'published'
                   AND r.view_version_id = %(view_version_id)s
                   AND r.name = %(relationship_name)s
                 LIMIT 1
                """,
                {
                    "project_id": project_id,
                    "view_version_id": view_version_id,
                    "relationship_name": relationship_name,
                },
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("match_profile: relationship unreadable: %s: %s", type(exc).__name__, exc)
        return None

    if row is None:
        return None
    blocked = unsupported_relationship(
        {"cardinality": row[0], "fan_out_policy": row[1], "bridge_dataset": row[2]}
    )
    return None if blocked is None else {"code": blocked[0], "message": blocked[1]}


def _side_snapshot(side: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: side.get(field)
        for field in (
            "datastream_id",
            "mapping_version_id",
            "execution_id",
            "output_version_id",
            "output_id",
            "publication_log_id",
            "plan_version_id",
            "schema_hash",
        )
    }


def _canonical_window(filters: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    window = []
    for entry in filters or []:
        if not isinstance(entry, Mapping) or entry.get("stage") != "pre_aggregation":
            continue
        window.append(
            {
                "datastream_id": str(entry.get("datastream_id") or ""),
                "canonical_field_id": str(entry.get("canonical_field_id") or ""),
                "operator": str(entry.get("operator") or ""),
                "value": entry.get("value"),
            }
        )
    return sorted(window, key=lambda item: _canonical_json(item))


def _mint_profile_receipt(
    *,
    project_id: str,
    common_key_version_id: str,
    relationship_name: str,
    view_version_id: str,
    window: Sequence[Mapping[str, Any]] | None,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    profile: Mapping[str, Any],
    issued_at: int,
) -> str:
    document = {
        "schema_version": PROFILE_RECEIPT_SCHEMA_VERSION,
        "project_id": project_id,
        "common_key_version_id": common_key_version_id,
        "relationship_name": relationship_name,
        "view_version_id": view_version_id,
        "window": _canonical_window(window),
        "issued_at": issued_at,
        "left": _side_snapshot(left),
        "right": _side_snapshot(right),
        "profile": dict(profile),
    }
    body = _b64encode(_canonical_json(document))
    signature = _b64encode(hmac.digest(_receipt_secret(), body.encode(), "sha256"))
    token = f"{body}.{signature}"
    if len(token.encode()) > MAX_PROFILE_RECEIPT_BYTES:
        raise ProfileReceiptInvalid("The matching profile receipt is too large.")
    return token


def reuse_profile_receipt(
    conn,
    *,
    token: str,
    project_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
    common_key_version_id: str,
    relationship_name: str,
    view_version_id: str,
    window: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return signed evidence only while every published authority pin is unchanged."""
    if not isinstance(token, str) or not token:
        raise ProfileReceiptInvalid("The matching profile receipt is not valid.")
    try:
        token_bytes = token.encode("ascii")
    except UnicodeError as exc:
        raise ProfileReceiptInvalid("The matching profile receipt is not valid.") from exc
    if len(token_bytes) > MAX_PROFILE_RECEIPT_BYTES:
        raise ProfileReceiptInvalid("The matching profile receipt is not valid.")
    try:
        body, signature = token.split(".", 1)
        expected = hmac.digest(_receipt_secret(), body.encode(), "sha256")
        if not hmac.compare_digest(_b64decode(signature), expected):
            raise ProfileReceiptInvalid("The matching profile receipt is not valid.")
        document = json.loads(_b64decode(body))
    except ProfileReceiptInvalid:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProfileReceiptInvalid("The matching profile receipt is not valid.") from exc

    expected_root = {
        "schema_version": PROFILE_RECEIPT_SCHEMA_VERSION,
        "project_id": project_id,
        "common_key_version_id": common_key_version_id,
        "relationship_name": relationship_name,
        "view_version_id": view_version_id,
        "window": _canonical_window(window),
    }
    if not isinstance(document, dict) or any(
        document.get(key) != value for key, value in expected_root.items()
    ):
        raise ProfileReceiptInvalid("The matching profile receipt does not describe this edge.")
    if set(document) != {*expected_root, "issued_at", "left", "right", "profile"}:
        raise ProfileReceiptInvalid("The matching profile receipt is not valid.")
    issued_at = document.get("issued_at")
    now = int(time.time())
    if (
        not isinstance(issued_at, int)
        or issued_at > now + 5
        or now - issued_at > PROFILE_RECEIPT_MAX_AGE_SECONDS
    ):
        raise ProfileReceiptInvalid("The matching profile receipt has expired.")
    if (document.get("left") or {}).get("datastream_id") != left_datastream_id or (
        document.get("right") or {}
    ).get("datastream_id") != right_datastream_id:
        raise ProfileReceiptInvalid("The matching profile receipt does not describe this edge.")

    components = _key_components(
        conn, project_id=project_id, common_key_version_id=common_key_version_id
    )
    current_left = resolve_side(
        conn, project_id=project_id, datastream_id=left_datastream_id, components=components
    )
    current_right = resolve_side(
        conn, project_id=project_id, datastream_id=right_datastream_id, components=components
    )
    if document.get("left") != _side_snapshot(current_left) or document.get(
        "right"
    ) != _side_snapshot(current_right):
        raise ProfileReceiptInvalid("The matching profile receipt is stale.")
    profile = document.get("profile")
    if not isinstance(profile, dict):
        raise ProfileReceiptInvalid("The matching profile receipt is not valid.")
    # AC 9: compile issues ZERO duplicate profile jobs. The figures below were
    # measured when the receipt was minted, and saying so is the difference
    # between "this cost three jobs" and "this costs three jobs every time".
    # UNCONDITIONAL. Guarding on `isinstance(cost, dict)` made the label depend
    # on whether the receipt was minted before or after the cost block existed,
    # so a receipt from the other side of a deploy came back unlabelled -- and
    # unlabelled is exactly the reading this repairs. A receipt with no cost
    # block still says it was reused and that it issued nothing.
    cost = profile.get("cost")
    profile = {
        **profile,
        "cost": {
            **(cost if isinstance(cost, dict) else {}),
            "reused_from_receipt": True,
            "warehouse_jobs_issued_now": 0,
        },
    }
    return profile


def _runner() -> tuple[Callable[[str, list], tuple[list[dict], Any]], bool]:
    """The same two runners execution uses, chosen the same way.

    MEASURED, because AC 9 asks for three figures and only one of them was
    provable. `query_*_measured` returns the rows AND what the job cost; the
    unmeasured `_query_bigquery` threw the QueryJob away, so `total_bytes_billed`
    -- which lives on the job, never on the RowIterator -- was unreachable from
    here whatever the engine.
    """
    from core import warehouse  # noqa: PLC0415

    bigquery_mode = warehouse._db_mode() == "bigquery"
    return (
        warehouse.query_bigquery_measured if bigquery_mode else warehouse.query_duckdb_measured
    ), bigquery_mode


def _bind_positional(sql: str) -> str:
    """`?` -> `@p0..@pN`, because BigQuery does not take positional markers."""
    out: list[str] = []
    index = 0
    for char in sql:
        if char == "?":
            out.append(f"@p{index}")
            index += 1
        else:
            out.append(char)
    return "".join(out)


# ---------------------------------------------------------------------------
# Resolving one side
# ---------------------------------------------------------------------------


def resolve_side(
    conn, *, project_id: str, datastream_id: str, components: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Where this Datastream's rows live, and which column carries each component.

    The chain is the one `query_execution.resolve_physical_plan` walks for a
    single source -- mapping version -> published output version -> relation --
    and a break anywhere is reported BY NAME so a person reads which link is
    missing instead of "no data".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.name, d.current_mapping_version_id, m.mapping_payload,
                   d.current_published_execution_id
              FROM app.datastreams d
              LEFT JOIN app.datastream_mapping_versions m
                     ON m.id = d.current_mapping_version_id
             WHERE d.id = %s AND d.project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ProfileRefused(
            "datastream_not_found", "That source is not in this Project.",
        )
    name, mapping_version_id, payload, published_execution_id = row
    if not mapping_version_id:
        raise ProfileRefused(
            "no_published_mapping",
            f"{name} publishes no mapping, so nothing says which column carries which "
            "field. Publish its mapping, then profile the match.",
            missing_link="datastream_mapping_versions",
        )

    bound = {
        str((field.get("binding") or {}).get("mdm_target") or ""): str(field.get("field_id") or "")
        for field in (payload or {}).get("fields") or []
        if isinstance(field, dict)
        and isinstance(field.get("binding"), dict)
        and field["binding"].get("mdm_target")
    }
    columns: dict[str, str] = {}
    for component in components:
        field_id = str(component.get("canonical_field_id") or "")
        physical = bound.get(field_id)
        if not physical:
            raise ProfileRefused(
                "key_component_unmapped",
                f"{name} does not map any column to {component.get('canonical_name')}, so "
                "the two sources cannot be matched on it. Map that column, then come back.",
                missing_link="mapping_payload.fields",
            )
        columns[field_id] = physical

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dov.relation_ref, dov.execution_id, dov.created_at, dov.id,
                   dov.output_id, dov.publication_log_id, dov.plan_version_id, dov.schema_hash
              FROM app.datastream_output_versions dov
              JOIN app.datastream_outputs output
                ON output.id = dov.output_id
               AND output.project_id = dov.project_id
               AND output.datastream_id = dov.datastream_id
             WHERE dov.mapping_version_id = %s
               AND dov.datastream_id = %s
               AND dov.project_id = %s
               AND dov.execution_id = %s
               AND output.output_kind = 'full_grain'
             ORDER BY dov.created_at DESC, dov.id DESC
             LIMIT 1
            """,
            (mapping_version_id, datastream_id, project_id, published_execution_id),
        )
        output = cur.fetchone()
    if output is None or not output[0]:
        raise ProfileRefused(
            "no_published_output",
            f"{name} has never produced an output, so there are no rows to count yet. "
            "Run a collection, then profile the match.",
            missing_link="datastream_output_versions",
        )

    relation_ref = output[0]
    relation = (
        relation_ref
        if isinstance(relation_ref, str)
        else (relation_ref.get("relation") or relation_ref.get("name") or "")
    )
    # AI-308: named and READABLE are two checks. A run that landed nowhere
    # readable publishes the traceability path `execution/<id>/candidate/relation`
    # -- a name, and not one a question can reach. The refusal is the same, and
    # it is raised here rather than left to `_safe_identifier` further down, which
    # answers by raising a class nothing on this path catches.
    if not names_a_readable_relation(relation):
        raise ProfileRefused(
            "output_names_no_relation",
            f"{name}'s published run landed nowhere readable. Run it again; if it "
            "lands the same way, its Runs tab carries the failure.",
            missing_link="relation_ref",
        )
    return {
        "datastream_id": datastream_id,
        "name": name,
        "mapping_version_id": mapping_version_id,
        "execution_id": output[1],
        "output_version_id": output[3],
        "output_id": output[4],
        "publication_log_id": output[5],
        "plan_version_id": output[6],
        "schema_hash": output[7],
        "relation": relation,
        "dataset": _dataset_of(project_id, relation),
        "columns": columns,
    }


def _dataset_of(project_id: str, relation: str) -> str:
    """Where the relation lives, ASKED OF THE MODE THAT WILL READ IT.

    `query_execution._dataset_for` answers with the BigQuery dataset, which is
    right in BigQuery mode and names a schema DuckDB does not have. Measured
    2026-08-13: qualifying a DuckDB read with the BigQuery dataset answers
    `Catalog Error: schema "marts_proj_..." does not exist`, and the profiler
    would have reported `unavailable` for two relations that were sitting right
    there. Both modes go through their own naming point, and neither invents one.
    """
    if "." in relation:
        return ""
    from core import warehouse, warehouse_tenancy  # noqa: PLC0415

    if warehouse._db_mode() == "bigquery":
        return _dataset_for(project_id, relation)
    return warehouse_tenancy.mart_prefix(project_id).rstrip(".")


def qualified_relation(side: Mapping[str, Any]) -> str:
    raw = str(side["relation"])
    if "." in raw:
        return ".".join(_safe_identifier(part) for part in raw.split("."))
    dataset = str(side.get("dataset") or "")
    return (
        f"{_safe_identifier(dataset)}.{_safe_identifier(raw)}"
        if dataset
        else _safe_identifier(raw)
    )


# ---------------------------------------------------------------------------
# One combined question per side, and one across them
# ---------------------------------------------------------------------------


def _where(
    side: Mapping[str, Any], filters: Sequence[Mapping[str, Any]] | None
) -> tuple[list[str], list[Any]]:
    predicates: list[str] = []
    params: list[Any] = []
    for entry in _canonical_window(filters):
        if entry["datastream_id"] != side["datastream_id"]:
            continue
        column = side["columns"].get(entry["canonical_field_id"])
        operator = _FILTER_OPERATORS.get(entry["operator"])
        if not column or not operator:
            raise ProfileRefused(
                "profile_window_not_compilable",
                "The matching profile cannot apply one requested window field or operator.",
                missing_link="filters",
            )
        predicates.append(f"{_safe_identifier(column)} {operator} ?")
        params.append(entry["value"])
    return predicates, params


def side_sql(
    side: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    filters: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[str, list[Any]]:
    """All per-side figures in one warehouse job."""
    relation = qualified_relation(side)
    key_columns = [
        _safe_identifier(side["columns"][str(c.get("canonical_field_id") or "")])
        for c in components
    ]
    null_predicate = " OR ".join(f"{column} IS NULL" for column in key_columns)
    keys = ", ".join(key_columns)
    predicates, params = _where(side, filters)
    summary_where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    repeated_predicates = [*(f"{column} IS NOT NULL" for column in key_columns), *predicates]
    # `distinct_keys` COUNTS ONLY KEYS THAT COULD MATCH -- a tuple with a null
    # part is reported by `null_key_rows` and excluded here. Found by the story
    # 66.11 journey gate: counting it in both places made
    # `unmatched = distinct - matched` say "one source is missing a partner" about
    # a row that has no key at all. Two different problems, two different repairs,
    # so two different numbers.
    return (  # noqa: S608 - identifiers allowlisted above
        "WITH summary AS (SELECT COUNT(*) AS total_rows, "
        f"SUM(CASE WHEN {null_predicate} THEN 1 ELSE 0 END) AS null_key_rows, "
        f"COUNT(DISTINCT CASE WHEN {null_predicate} THEN NULL ELSE ({keys}) END) "
        f"AS distinct_keys FROM {relation}{summary_where}), "
        "repeated AS (SELECT rows_per_key FROM ("
        f"SELECT {keys}, COUNT(*) AS rows_per_key FROM {relation} "
        f"WHERE {' AND '.join(repeated_predicates)} "
        f"GROUP BY {keys} HAVING COUNT(*) > 1 LIMIT {MAX_KEY_GROUPS}) bounded) "
        "SELECT summary.total_rows, summary.null_key_rows, summary.distinct_keys, "
        "(SELECT COUNT(*) FROM repeated) AS duplicated_keys, "
        "COALESCE((SELECT MAX(rows_per_key) FROM repeated), 0) AS max_rows_per_key "
        "FROM summary"
    ), [*params, *params]


def matched_sql(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    filters: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[str, list[Any]]:
    """How many distinct key tuples exist on both sides.

    Distinct tuples and not rows: matching ROWS is what multiplies, and the
    multiplication is computed from the per-side repetition instead. Counting
    matched rows here would silently answer the fan-out question with the
    fan-out included.
    """
    left_keys = [
        _safe_identifier(left["columns"][str(c.get("canonical_field_id") or "")])
        for c in components
    ]
    right_keys = [
        _safe_identifier(right["columns"][str(c.get("canonical_field_id") or "")])
        for c in components
    ]
    on = " AND ".join(f"l.{a} = r.{b}" for a, b in zip(left_keys, right_keys))
    left_not_null = " AND ".join(f"{c} IS NOT NULL" for c in left_keys)
    right_not_null = " AND ".join(f"{c} IS NOT NULL" for c in right_keys)
    left_filters, left_params = _where(left, filters)
    right_filters, right_params = _where(right, filters)
    left_where = " AND ".join([left_not_null, *left_filters])
    right_where = " AND ".join([right_not_null, *right_filters])
    return (
        "SELECT COUNT(*) AS matched_keys FROM ("  # noqa: S608 - identifiers allowlisted
        f"SELECT DISTINCT {', '.join(left_keys)} FROM {qualified_relation(left)} "
        f"WHERE {left_where}) AS l "
        f"JOIN (SELECT DISTINCT {', '.join(right_keys)} FROM {qualified_relation(right)} "
        f"WHERE {right_where}) AS r ON {on}"
    ), [*left_params, *right_params]


def _first(rows: list[dict], *names: str) -> dict[str, Any]:
    if not rows:
        return {}
    row = rows[0]
    return {name: row.get(name) for name in names}


def _job_cost(jobs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What this profile cost the warehouse, and how each figure was obtained.

    NO FIGURE WITHOUT ITS STATE, the rule the rest of this module already keeps.
    Billed bytes are summed only when EVERY job reported them: a total over two
    of three jobs is smaller than the truth and reads exactly like the truth.
    A DuckDB profile is `not_applicable`, never 0 -- 0 bytes billed is a
    measurement, and a local file read produces none.
    """
    jobs = [dict(job) for job in jobs]
    states = {str(job.get("billed_bytes_state")) for job in jobs}
    if not jobs:
        billed, state = None, "unavailable"
    elif states == {"exact"}:
        billed, state = sum(int(job.get("billed_bytes") or 0) for job in jobs), "exact"
    elif states == {"not_applicable"}:
        billed, state = None, "not_applicable"
    else:
        billed, state = None, "unavailable"

    # THE SAME RULE, NOT A FOURTH STATE. This used to sum the two jobs of three
    # that answered and label the result `partial` -- a total over part of the
    # work, which is smaller than the truth and reads exactly like the truth.
    # That is the defect the billed-bytes branch above exists to prevent, and
    # the vocabulary the document ratifies has THREE states, not four. Nothing
    # is lost: every job keeps its own `elapsed_ms` in `jobs[]`, so a reader who
    # wants the detail has it without a misleading total above it.
    elapsed = [job.get("elapsed_ms") for job in jobs if job.get("elapsed_ms") is not None]
    complete = bool(jobs) and len(elapsed) == len(jobs)
    return {
        "warehouse_jobs_issued": len(jobs),
        "engine": jobs[0]["engine"] if jobs else None,
        "billed_bytes": billed,
        "billed_bytes_state": state,
        # Wall clock, and every engine has one -- so `exact` whenever every job
        # reached the engine, `unavailable` the moment one did not.
        "elapsed_ms": sum(int(value) for value in elapsed) if complete else None,
        "elapsed_ms_state": "exact" if complete else "unavailable",
        "jobs": jobs,
    }


def profile_match(
    conn,
    *,
    project_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
    common_key_version_id: str,
    relationship_name: str | None = None,
    view_version_id: str | None = None,
    window: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The evidence a person needs before running a cross. Reads only."""
    components = _key_components(
        conn, project_id=project_id, common_key_version_id=common_key_version_id
    )
    left = resolve_side(
        conn, project_id=project_id, datastream_id=left_datastream_id, components=components
    )
    right = resolve_side(
        conn, project_id=project_id, datastream_id=right_datastream_id, components=components
    )

    runner, bigquery_mode = _runner()
    # Every job this profile issued, in order, with what it cost. A job that
    # FAILED is recorded too: three jobs of which one was unreadable is not the
    # same measurement as two jobs, and the bound AC 9 states is on jobs issued.
    costs: list[dict[str, Any]] = []

    def _ask(sql: str, params: list[Any]) -> tuple[list[dict], bool]:
        try:
            rows, cost = runner(_bind_positional(sql) if bigquery_mode else sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("match_profile: warehouse_unreadable: %s: %s", type(exc).__name__, exc)
            costs.append(
                {
                    "engine": "bigquery" if bigquery_mode else "duckdb",
                    "elapsed_ms": None,
                    "billed_bytes": None,
                    "billed_bytes_state": "unavailable",
                    "cache_hit": None,
                    "ok": False,
                }
            )
            return [], False
        costs.append({**cost.as_dict(), "ok": True})
        return rows, True

    left_rows, left_params = side_sql(left, components, window)
    right_rows, right_params = side_sql(right, components, window)
    matched_rows, matched_params = matched_sql(left, right, components, window)
    left_rows, left_ok = _ask(left_rows, left_params)
    right_rows, right_ok = _ask(right_rows, right_params)
    matched_rows, matched_ok = _ask(matched_rows, matched_params)

    # An empty answer from a runner that did not raise is a real answer only when
    # the query was an aggregate -- and all five are. A missing row therefore means
    # the warehouse had nothing to say about the relation at all.
    left_state = EXACT if (left_ok and left_rows) else UNAVAILABLE
    right_state = EXACT if (right_ok and right_rows) else UNAVAILABLE
    matched_state = (
        EXACT
        if (matched_ok and matched_rows and left_state == EXACT and right_state == EXACT)
        else UNAVAILABLE
    )

    left_side = _side_figures(left, left_rows, left_state, left_ok)
    right_side = _side_figures(right, right_rows, right_state, right_ok)
    matched_keys = int((matched_rows[0] or {}).get("matched_keys") or 0) if matched_rows else None

    multiplication = _multiplication(left_side, right_side)
    profile = {
        "common_key_version_id": common_key_version_id,
        "components": [dict(component) for component in components],
        "left": left_side,
        "right": right_side,
        "matched": {
            "state": matched_state,
            "matched_keys": matched_keys if matched_state == EXACT else None,
            "left_unmatched_keys": _unmatched(left_side, matched_keys, matched_state),
            "right_unmatched_keys": _unmatched(right_side, matched_keys, matched_state),
        },
        "multiplication": multiplication,
        "execution_safety": _safety(left_side, right_side, multiplication),
        "bounds": {"max_key_groups": MAX_KEY_GROUPS, "warehouse_jobs": 3},
        # AC 9's three figures, emitted rather than left to a test to count.
        "cost": _job_cost(costs),
        "authority": {"left": _side_snapshot(left), "right": _side_snapshot(right)},
    }

    # THE SAME LEAK AS THE MATCH CATALOGUE, AND THE MORE COSTLY HALF OF IT.
    # `_safety` above is the MEASUREMENT half -- it reads rows and knows nothing
    # about what the executor supports. The console gates its Run control on this
    # very field (`profileCanRun`), so a `many_to_one` relationship carrying
    # `fan_out_policy = 'deduplicate'` measured clean, read `ready`, ENABLED the
    # button, and ended at `deduplication_not_supported` one click later. The
    # executor's own rule is asked here, imported and never restated.
    blocked = _relationship_block(
        conn,
        project_id=project_id,
        relationship_name=relationship_name,
        view_version_id=view_version_id,
    )
    profile["execution_blocked"] = blocked
    if blocked is not None:
        profile["execution_safety"] = "unsafe"

    if relationship_name and view_version_id:
        issued_at = int(time.time())
        profile["evidence"] = {
            "profiled_at": issued_at,
            "valid_until": issued_at + PROFILE_RECEIPT_MAX_AGE_SECONDS,
        }
        profile["profile_receipt"] = _mint_profile_receipt(
            project_id=project_id,
            common_key_version_id=common_key_version_id,
            relationship_name=relationship_name,
            view_version_id=view_version_id,
            window=window,
            left=left,
            right=right,
            profile=profile,
            issued_at=issued_at,
        )
    return profile


def _side_figures(
    side: Mapping[str, Any],
    rows: list[dict],
    state: str,
    duplicates_ok: bool,
) -> dict[str, Any]:
    figures = _first(rows, "total_rows", "null_key_rows", "distinct_keys")
    duplicates = _first(rows, "duplicated_keys", "max_rows_per_key")
    duplicated = int(duplicates.get("duplicated_keys") or 0) if rows else None
    # The duplicate probe is the only capped read: at the cap, what it returns is
    # a floor and the figure says `estimated` rather than pretending to be whole.
    duplicate_state = (
        UNAVAILABLE
        if not duplicates_ok
        else (ESTIMATED if (duplicated or 0) >= MAX_KEY_GROUPS else EXACT)
    )
    return {
        "datastream_id": side["datastream_id"],
        "name": side["name"],
        "relation": side["relation"],
        "mapping_version_id": side["mapping_version_id"],
        "state": state,
        "total_rows": int(figures.get("total_rows") or 0) if state == EXACT else None,
        "null_key_rows": int(figures.get("null_key_rows") or 0) if state == EXACT else None,
        "distinct_keys": int(figures.get("distinct_keys") or 0) if state == EXACT else None,
        "duplicate_state": duplicate_state,
        "duplicated_keys": duplicated if duplicate_state != UNAVAILABLE else None,
        "max_rows_per_key": (
            int(duplicates.get("max_rows_per_key") or 0)
            if duplicate_state != UNAVAILABLE and rows
            else None
        ),
    }


def _unmatched(side: Mapping[str, Any], matched_keys: int | None, state: str) -> int | None:
    if state != EXACT or matched_keys is None or side.get("distinct_keys") is None:
        return None
    return max(int(side["distinct_keys"]) - int(matched_keys), 0)


def _multiplication(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """How many rows one matched key can become. 1x1 is the only quiet answer."""
    left_max = left.get("max_rows_per_key")
    right_max = right.get("max_rows_per_key")
    if left["duplicate_state"] == UNAVAILABLE or right["duplicate_state"] == UNAVAILABLE:
        return {
            "state": UNAVAILABLE,
            "worst_case_rows_per_key": None,
            "explanation": (
                "How much this cross would multiply the rows could not be measured. "
                "Running it now would produce a number nobody can check."
            ),
        }
    left_factor = max(int(left_max or 1), 1)
    right_factor = max(int(right_max or 1), 1)
    factor = left_factor * right_factor
    if factor <= 1:
        explanation = "Each key appears once on both sides, so nothing multiplies."
    else:
        explanation = (
            f"One key can appear {left_factor} time(s) on {left['name']} and "
            f"{right_factor} time(s) on {right['name']}, so a matched key can become "
            f"{factor} rows. Every measure of both sources is counted that many times "
            "unless the plan aggregates before it merges."
        )
    state = (
        ESTIMATED
        if ESTIMATED in {left["duplicate_state"], right["duplicate_state"]}
        else EXACT
    )
    return {"state": state, "worst_case_rows_per_key": factor, "explanation": explanation}


def _safety(
    left: Mapping[str, Any], right: Mapping[str, Any], multiplication: Mapping[str, Any]
) -> str:
    """What the DATA says, independently of what Governance approved.

    An unmeasurable profile is `review_required` and never `ready`: an approval
    is not a measurement, and this function is the measurement half.
    """
    if left["state"] != EXACT or right["state"] != EXACT:
        return "review_required"
    if multiplication["state"] == UNAVAILABLE:
        return "review_required"
    factor = int(multiplication.get("worst_case_rows_per_key") or 1)
    if factor > 1 and (left.get("duplicated_keys") or 0) and (right.get("duplicated_keys") or 0):
        # Duplicated on BOTH sides is a many-to-many in the data, whatever the
        # relationship says it is. This is the case that silently multiplies
        # every measure, so it is the one refusal this module makes on evidence.
        return "unsafe"
    if factor > 1:
        return "review_required"
    return "ready"


def _key_components(
    conn, *, project_id: str, common_key_version_id: str
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT components FROM app.mdm_common_key_versions
             WHERE id = %s AND project_id = %s
            """,
            (common_key_version_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ProfileRefused(
            "common_key_version_not_found",
            "That common key version is not in this Project.",
        )
    components = row[0] or []
    if not components:
        raise ProfileRefused(
            "common_key_has_no_component",
            "That common key version declares no component, so there is nothing to match on.",
        )
    return [dict(component) for component in components]
