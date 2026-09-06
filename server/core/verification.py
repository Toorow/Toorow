"""toorow -- Post-pull populate verification (Story 3.5, AD-13).

Every successful pull is followed by a verification pass that compares the rows
actually landed in the raw table against the expected row count derived from the
module manifest's report profile.  Results are written to app.pull_verifications
and, when a measured count came back at ZERO, the connection health is set to
'populate_failed' (distinct from 'revoked' / 'stale').

AI-302 -- WHAT A VERDICT MAY AND MAY NOT SAY. 'populate_failed' is sticky and the
enqueue gate refuses every pull of an authorization that carries it, so ONE false
verdict closes every Datastream behind one credential. It happened twice in
production (2026-08-12, 2026-08-17), both times on a pull whose rows were readable
seconds before the verdict. Three rules follow, and each is enforced below:

  * a count that could not be TAKEN is ``None``, never 0, and files no verdict at
    all -- see ``_count_raw_rows``;
  * the relation counted is the one THIS pull's profile declares, and when nothing
    can name it the counter answers nothing rather than the neighbour's address --
    see ``_relation_for_pull``;
  * a red NAMES the pull that raised it (migration 276), and a verdict of 'ok'
    LIFTS it -- the symmetric write ``clear_connection_health_red`` was written for
    and which only account verification ever called;
  * ONLY 'empty' raises it. A 'partial' files its verdict and stops there: a
    window that landed rows is a quiet fact plus an alert, never a closed door
    (AI-101, 2026-08-17). Raising the sticky red on sparseness suspended every
    Datastream of an authorization on a 12% draw, which is the suspension the
    schedule decision explicitly refuses to implement.

Public API:
  compute_expected_rows(manifest, date_from, date_to, report_profile_id=None) -> int
  run_post_pull_verification(pull_id, connection_ref_id, date_from, date_to, manifest) -> None

Environment variables:
  VERIFICATION_PARTIAL_THRESHOLD  float in [0,1], default 0.5.
      A pull whose completeness_ratio is below this threshold (but > 0) receives
      verdict 'partial'.  A pull with zero actual rows always receives 'empty'.
  TOOROW_RAW_TABLE_NAME  name of the raw DuckDB table to count rows from.
      Defaults to the value set by the active module at startup via
      register_raw_table_name().  Required at P3-dev.
      # TODO(3.6): generalize -- each module declares its raw table name in the manifest
  TOOROW_DB_MODE  "duckdb" (default, local testing) | "bigquery" (production).
      AI-96 [3]: the row count follows this the same way land_raw_rows() does.
      It used to read DuckDB unconditionally, so in BigQuery mode -- where there
      is no DuckDB file -- actual_rows was 0 by construction and every pull got
      verdict 'empty'.

Dev notes:
  - compute_expected_rows() reads the report_profile the pull ACTUALLY ran (AI-55),
    then that profile's declared rows/day or its dimension cardinalities.
    When the manifest has no cardinality declarations (string-only dimension names),
    the fallback of 1 row/day applies.  This means any pull returning at least 1 row
    will receive verdict 'ok', which is the safe-conservative baseline.
    A future story can add 'cardinality' to the manifest dimensions for stricter checks.
  - All log strings are ASCII-only (AI-03).
  - This module never raises -- exceptions are caught and logged at WARNING.

AD-2: source-agnostic -- no module-specific strings anywhere in this file.
AD-13: populate verification is a mandatory architecture requirement.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import date
from math import prod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_PARTIAL_THRESHOLD = 0.5

# Raw table name registry (AD-2: source-agnostic -- no module-specific names here).
# Each module's connector.py registers its raw table name at startup via
# register_raw_table_name(). Overridable via TOOROW_RAW_TABLE_NAME env var.
# TODO(3.6): generalize to per-module raw table name declared in the manifest
# review-3-5 F-5: PER-PROVIDER registry -- a single global would be silently
# overwritten by the second module (cross-provider row counts). The legacy
# single-arg call keeps working as a fallback entry for callers that predate
# the provider-aware signature.
_raw_table_names: dict[str, str] = {}
_raw_table_fallback: str = ""


def register_raw_table_name(name: str, provider: str | None = None) -> None:
    """Register a module's raw table name (called at module load time).

    Args:
        name: raw table name (module-owned string; core stays source-agnostic).
        provider: the module's manifest name. When omitted (legacy call), the
            name is stored as the single-module fallback.
    """
    global _raw_table_fallback
    if provider:
        _raw_table_names[provider] = name
    else:
        _raw_table_fallback = name


def _get_raw_table_name(provider: str | None = None) -> str:
    """Resolve the raw table for *provider* (env override > registry > fallback)."""
    env = os.environ.get("TOOROW_RAW_TABLE_NAME", "")
    if env:
        return env
    if provider and provider in _raw_table_names:
        return _raw_table_names[provider]
    return _raw_table_fallback


def _partial_threshold() -> float:
    """Read VERIFICATION_PARTIAL_THRESHOLD env var (default 0.5)."""
    try:
        raw = os.environ.get("VERIFICATION_PARTIAL_THRESHOLD", str(_DEFAULT_PARTIAL_THRESHOLD))
        return float(raw)
    except (ValueError, TypeError):
        return _DEFAULT_PARTIAL_THRESHOLD


# ---------------------------------------------------------------------------
# Expected-row computation from manifest (AC2)
# ---------------------------------------------------------------------------


def compute_expected_rows(
    manifest: dict,
    date_from: str,
    date_to: str,
    report_profile_id: str | None = None,
) -> int:
    """Compute the expected row count for a pull window from the manifest profile.

    Algorithm:
      # AI-20: partial-verdict reachability
      # AI-55: the profile ACTUALLY pulled owns the expectation
      0. When *report_profile_id* names a profile, that profile answers. An id
         the manifest does not carry falls back to days -- a foreign profile's
         number is not this pull's expectation.
      1. Otherwise, find the first report_profiles entry with granularity=='day'
         (or any if granularity is absent).  Fall back to days if no profiles
         exist.
      2. If the chosen profile declares verification_expected_rows_per_day (int),
         return days * verification_expected_rows_per_day. This activates the
         partial-verdict path: a module with value=150 means a 7-day pull expects
         1050 rows; if only 30 land, completeness_ratio = 0.029 -> verdict 'partial'.
         If null or absent, fall through to the cardinality-product algorithm.
      3. Read 'dimensions' list from the profile; extract 'cardinality' per entry
         (default 1 when absent -- conservative: any rows are 'expected').
      4. Return days * product(cardinalities).

    If the manifest has no report_profiles or no dimensions, return days (1 row
    per day as the safe minimum expectation -- avoids false-positive 'empty'
    verdicts on sparse sources).

    Args:
        manifest:  The module manifest dict (from loader.LoadedModule.manifest).
        date_from: ISO-8601 date string (inclusive start of pull window).
        date_to:   ISO-8601 date string (inclusive end of pull window).
        report_profile_id: The `report_profiles[].id` this pull ran, when known.

    Returns:
        Expected row count (>= 1 when date window is valid).
    """
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
        days = (d_to - d_from).days + 1
        if days < 1:
            days = 1
    except (ValueError, TypeError):
        return 1

    profiles = manifest.get("report_profiles")
    if not profiles:
        return days

    # AI-55: THE PROFILE PULLED OWNS ITS EXPECTATION.
    # This read `profiles[0]` for every pull, so a Connector declaring several
    # profiles was always measured against its first one. GA4 declares six --
    # `standard_daily` 150 rows/day, `user_type_daily` 9, and four at 50 -- so a
    # `user_type_daily` pull that landed all 9 of its rows was scored 9/150 and
    # filed `partial`, and the four 50-row profiles were scored 50/150 and filed
    # `partial` too. A soft verdict, never a hard error, but a verdict that said
    # 'incomplete' about a complete pull.
    chosen = None
    if report_profile_id:
        chosen = next((p for p in profiles if p.get("id") == report_profile_id), None)
        if chosen is None:
            # The dispatcher named a profile this manifest does not carry. That
            # is a mismatch to fix elsewhere; borrowing another profile's number
            # here would file a verdict about a pull nobody described.
            return days
    if chosen is None:
        # No id given (older call sites): first day-granular profile, else first.
        for p in profiles:
            if p.get("granularity") == "day":
                chosen = p
                break
    if chosen is None:
        chosen = profiles[0]

    # AI-20: partial-verdict reachability — use manifest-declared rows_per_day when present.
    # verification_expected_rows_per_day must be an int (not null) to activate this path.
    rows_per_day = chosen.get("verification_expected_rows_per_day")
    if isinstance(rows_per_day, int):
        return days * rows_per_day
    # null or absent -> fall through to cardinality-product algorithm below

    dimensions = chosen.get("dimensions", [])
    if not dimensions:
        return days

    # Extract cardinalities; dimension entries may be strings (name only) or dicts
    cardinalities: list[int] = []
    for dim in dimensions:
        if isinstance(dim, dict):
            cardinalities.append(int(dim.get("cardinality", 1)))
        # String dimension names carry no cardinality information -- use 1
        else:
            cardinalities.append(1)

    if not cardinalities:
        return days

    return days * prod(cardinalities)


# ---------------------------------------------------------------------------
# Raw row count (AC4) -- DuckDB at P3-dev
# ---------------------------------------------------------------------------


def _declared_raw_relation_count(provider: str | None) -> int:
    """How many DISTINCT raw relations *provider* declares across its profiles.

    This is the measurement that says whether the per-module registry can stand
    in for the profile. The registry holds ONE table per module; for a connector
    that lands in several, that one name is a GUESS, and a guess that returns 0
    is not evidence of an empty pull -- it is evidence of having looked in the
    wrong place. Measured 2026-08-17: 9 of the 39 connectors declare more than
    one raw relation (google-analytics declares 8, youtube-analytics 2).

    Returns 0 for an unknown module, which is not "cannot count": a module the
    declarations do not carry has no second relation to be confused with, and its
    registered table is the only address there is.

    AND FOR THOSE NINE THE REGISTRY IS NOT EVEN THE DEFAULT RELATION -- it is
    whichever `register_raw_table_name` call ran LAST, because the dict is keyed by
    provider and each call overwrites the previous. Measured by importing the
    connectors: `google-analytics` -> `raw_ga4_catalog_daily`,
    `hubspot` -> `raw_hubspot_catalog_daily`,
    `google-business-profile` -> `raw_gbp_search_keyword_monthly`. Every fall-back
    count for a GA4 pull was therefore taken in the CATALOG table. That is the
    measurement behind refusing to guess, rather than a preference.
    """
    try:
        from core.stage_relation_resolver import (  # noqa: PLC0415
            RAW_RELATION_KEY,
            declared_profile_relations,
        )

        profiles = declared_profile_relations().get((provider or "").strip()) or {}
        return len({p.get(RAW_RELATION_KEY) for p in profiles.values() if p.get(RAW_RELATION_KEY)})
    except Exception as exc:  # noqa: BLE001 -- never raises (HG-1)
        logger.warning("verification: declared_relations_unreadable provider=%s: %s", provider, exc)
        return 0


def _relation_for_pull(
    pull_id: str, provider: str | None, report_profile_id: str | None
) -> str | None:
    """WHICH relation this pull landed in, or None when nothing can name it.

    THE PROFILE'S OWN RELATION FIRST. The registry holds ONE table per module,
    and a Connector whose profiles land in several -- youtube-analytics lands a
    daily table and a breakdown table -- had its breakdown pulls counted in the
    daily one. Zero rows of that pull_id were there, the verdict was `empty`,
    and `empty` raises the sticky red flag that refuses every later run. So a
    successful pull closed the door behind itself: measured 2026-08-12, on a
    pull that had just landed 112 rows, and again 2026-08-17 on 344.

    AND WHEN THE PROFILE CANNOT BE PLACED, THIS ANSWERS NOTHING RATHER THAN
    ANSWERING THE NEIGHBOUR'S ADDRESS. The 2026-08-17 repetition is exactly that
    fall-through: `facts_api.run_verification_for` passed no profile at all, the
    registry answered `raw_youtube_daily`, the 344 rows were in
    `raw_youtube_breakdown`, and the count was a truthful zero about the wrong
    table. The env override is trusted unconditionally -- it is an operator's
    explicit declaration, and it is how the local recipe addresses its fixture.
    """
    env = os.environ.get("TOOROW_RAW_TABLE_NAME", "")
    if env:
        return env

    if report_profile_id:
        try:
            from core.stage_relation_resolver import resolve_stage_relations  # noqa: PLC0415

            resolved = resolve_stage_relations(
                connector=provider, report_profile_id=report_profile_id
            )
        except Exception as exc:  # noqa: BLE001 -- verification never raises
            logger.warning("verification: profile_relation_unreadable: %s", exc)
            return None
        relation = str(resolved.get("collected_relation") or "")
        if relation:
            return relation
        # The profile was NAMED and could not be placed. Falling back to the
        # module's single registered table is what filed 344 landed rows as empty.
        if _declared_raw_relation_count(provider) > 1:
            logger.warning(
                "verification: relation_unresolved pull_id=%s provider=%s profile=%s reason=%s "
                "-- the connector lands in several relations, so no count is attempted",
                pull_id,
                provider,
                report_profile_id,
                resolved.get("reason"),
            )
            return None
    elif _declared_raw_relation_count(provider) > 1:
        logger.warning(
            "verification: relation_unresolved pull_id=%s provider=%s reason=no_report_profile "
            "-- the connector lands in several relations, so no count is attempted",
            pull_id,
            provider,
        )
        return None

    return _get_raw_table_name(provider) or None


def _count_raw_rows(
    pull_id: str,
    provider: str | None = None,
    project_id: str | None = None,
    report_profile_id: str | None = None,
) -> int | None:
    """Count rows in the raw table for the given pull_id, on the configured backend.

    The raw table name is read from TOOROW_RAW_TABLE_NAME env var or from
    the value registered by the active module via register_raw_table_name().
    *provider* is the MODULE name (AI-95): the registry is keyed by module, and
    an unknown key falls through to the global fallback -- i.e. ANOTHER
    connector's table, counted and served as this pull's verdict.

    AI-96 [3]: this used to read DuckDB unconditionally. In BigQuery mode there
    is no DuckDB file at all, so the missing-path branch returned 0 with a
    WARNING and ``actual_rows`` was 0 BY CONSTRUCTION in production -- every
    pull landed verdict 'empty', which closes the whole chain (no counted rows
    -> no ready candidate -> datastream_publication blocks -> never 'active' ->
    no nightly). The count now follows the same TOOROW_DB_MODE dispatch that
    ``raw_landing.land_raw_rows`` uses to WRITE, so the reader and the writer
    can no longer disagree about which warehouse holds the rows.

    DuckDB remains the LOCAL TESTING backend (directive 2026-07-30) and its path
    is unchanged -- "BigQuery in production" is not "DuckDB never again".

    A COUNTER THAT CANNOT COUNT IS NOT A ZERO, and this used to return 0 for both.
    Every branch that failed to READ -- no relation resolvable, no table
    registered, an unknown backend, an unreachable warehouse, a missing DuckDB
    file -- returned the same 0 as a genuinely empty pull, and 0 is what raises
    the sticky `populate_failed` that refuses every later run of the whole
    authorization (twice in production: 2026-08-12, 2026-08-17). So the two are
    now different values: an INT is a measurement, and ``None`` means "not
    measured". Only a query that actually ran may answer 0.

    Still never raises -- HG-1: verification is an annotation, not job control, so
    an unreachable warehouse must not turn into a failed pull.

    # TODO(3.6): per-module table name from manifest (multi-module support)
    """
    raw_table = _relation_for_pull(pull_id, provider, report_profile_id)
    if not raw_table:
        logger.warning(
            "verification: raw_table_name not configured -- cannot count rows pull_id=%s", pull_id
        )
        return None

    mode = (os.environ.get("TOOROW_DB_MODE") or "duckdb").strip().lower()
    if mode == "bigquery":
        return _count_raw_rows_bigquery(pull_id, raw_table, project_id)
    if mode != "duckdb":
        logger.warning(
            "verification: unknown TOOROW_DB_MODE=%s -- cannot count raw rows pull_id=%s",
            mode,
            pull_id,
        )
        return None
    return _count_raw_rows_duckdb(pull_id, raw_table, project_id)


def distinct_raw_values(
    pull_id: str,
    field_ids: "Sequence[str]",
    provider: str | None = None,
    project_id: str | None = None,
    report_profile_id: str | None = None,
) -> dict[str, dict[str, int]]:
    """What DISTINCT values this pull actually landed in *field_ids*, with counts.

    Lives here, next to ``_count_raw_rows``, and not in the module that needs it:
    addressing the raw table is already hard once -- module registry, the
    duckdb/bigquery dispatch that must follow ``land_raw_rows``, and the org-schema
    qualification with its identifier-folding symmetry. A second copy in another
    module would drift from the writer the day one of those changes, and a reader
    that disagrees with the writer is exactly the defect AI-96 [3] recorded here.

    Returns ``{field_id: {value: occurrence_count}}``, skipping fields the table
    does not carry. Empty on ANY error: like every read in this module it is
    best-effort and never raises -- a governance read must not turn into a failed
    pull (HG-1).

    AND IT ADDRESSES THE PROFILE'S RELATION, exactly like the counter (AI-302). It
    read `_get_raw_table_name(provider)` alone, which is ONE table per module: for
    the 9 connectors that land in several, a breakdown pull's tracked entities were
    looked for in the daily table. The failure mode is milder than the counter's --
    a field the table does not carry is skipped, so the result is a MISSED
    observation rather than a false one, and the competitors surface then says "no
    candidate observed" about a pull that observed plenty. Same defect, same fix,
    same call.
    """
    if not field_ids:
        return {}
    raw_table = _relation_for_pull(pull_id, provider, report_profile_id)
    if not raw_table:
        logger.warning(
            "verification: raw_table_name not configured -- cannot read values pull_id=%s",
            pull_id,
        )
        return {}

    mode = (os.environ.get("TOOROW_DB_MODE") or "duckdb").strip().lower()
    if mode == "bigquery":
        return _distinct_raw_values_bigquery(pull_id, raw_table, list(field_ids), project_id)
    if mode != "duckdb":
        logger.warning(
            "verification: unknown TOOROW_DB_MODE=%s -- cannot read values pull_id=%s",
            mode,
            pull_id,
        )
        return {}
    return _distinct_raw_values_duckdb(pull_id, raw_table, list(field_ids), project_id)


def _distinct_raw_values_duckdb(
    pull_id: str, raw_table: str, field_ids: list[str], project_id: str | None
) -> dict[str, dict[str, int]]:
    import duckdb  # noqa: PLC0415

    duckdb_path = os.environ.get("TOOROW_DUCKDB_PATH", "")
    if not duckdb_path or not os.path.exists(duckdb_path):
        return {}
    qualified_table = _qualified_raw_table(raw_table, project_id)
    out: dict[str, dict[str, int]] = {}
    try:
        con = duckdb.connect(duckdb_path, read_only=True)
        try:
            for field_id in field_ids:
                # The field id comes from the connector's tracked-entity DECLARATION
                # (manifest), never from a request. Quoted anyway so a declared name
                # can never be read as SQL.
                safe = '"' + str(field_id).replace('"', '""') + '"'
                try:
                    rows = con.execute(
                        f"SELECT {safe} AS v, COUNT(*) AS n FROM {qualified_table} "  # noqa: S608
                        f"WHERE pull_id = ? AND {safe} IS NOT NULL GROUP BY 1",
                        [pull_id],
                    ).fetchall()
                except Exception:  # noqa: BLE001 -- a field this table does not carry
                    continue
                values = {str(v): int(n) for v, n in rows if str(v).strip()}
                if values:
                    out[field_id] = values
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
        logger.warning("verification: distinct_values_failed pull_id=%s: %s", pull_id, exc)
        return {}
    return out


def _distinct_raw_values_bigquery(
    pull_id: str, raw_table: str, field_ids: list[str], project_id: str | None
) -> dict[str, dict[str, int]]:
    try:
        from google.cloud import bigquery  # noqa: PLC0415

        from core import warehouse_tenancy  # noqa: PLC0415

        schemas = (
            warehouse_tenancy.resolve_org_schemas(project_id=project_id) if project_id else None
        )
        if schemas is None:
            return {}
        client = bigquery.Client()
        out: dict[str, dict[str, int]] = {}
        for field_id in field_ids:
            safe = "`" + str(field_id).replace("`", "") + "`"
            try:
                job = client.query(
                    f"SELECT {safe} AS v, COUNT(*) AS n "  # noqa: S608
                    f"FROM `{client.project}.{schemas.raw}.{raw_table}` "
                    f"WHERE pull_id = @pull_id AND {safe} IS NOT NULL GROUP BY 1",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("pull_id", "STRING", pull_id)
                        ]
                    ),
                )
                values = {str(r["v"]): int(r["n"]) for r in job.result() if str(r["v"]).strip()}
            except Exception:  # noqa: BLE001 -- a field this table does not carry
                continue
            if values:
                out[field_id] = values
        return out
    except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
        logger.warning("verification: distinct_values_bq_failed pull_id=%s: %s", pull_id, exc)
        return {}


def _qualified_raw_table(raw_table: str, project_id: str | None) -> str:
    """The org-qualified DuckDB table name, or the bare name when unresolvable.

    Extracted from ``_count_raw_rows_duckdb`` so the counter and the value reader
    resolve the SAME identifier -- including its quote-both-sides folding symmetry.
    """
    try:
        from core import warehouse_tenancy  # noqa: PLC0415

        if warehouse_tenancy.org_schemas_enabled() and project_id:
            schemas = warehouse_tenancy.resolve_org_schemas(project_id=project_id)
            if schemas is not None:
                return f'"{schemas.raw}"."{raw_table}"'
    except Exception as exc:  # noqa: BLE001 -- degradation contract
        logger.warning(
            "verification: org schema resolution failed for project_id=%s: %s -- reading main",
            project_id,
            exc,
        )
    return raw_table


def _count_raw_rows_bigquery(pull_id: str, raw_table: str, project_id: str | None) -> int | None:
    """Count this pull's rows in the org's BigQuery raw dataset.

    Mirrors ``raw_landing.promote_candidate``'s BigQuery branch exactly: the
    same project resolver (which REFUSES to inherit the ambient credential's
    default project rather than counting in someone else's warehouse) and the
    same dataset resolver, so the count reads the dataset the write produced.

    The filter on ``pull_id`` is the point of the function: an unfiltered
    COUNT(*) would return the connector's whole history and make every pull
    'ok', including an empty one.
    """
    try:
        from google.cloud import bigquery  # noqa: PLC0415

        from core import warehouse_tenancy  # noqa: PLC0415
        from core.raw_landing import resolve_warehouse_project  # noqa: PLC0415

        client = bigquery.Client(project=resolve_warehouse_project())
        dataset = warehouse_tenancy.bigquery_raw_dataset(project_id, conn=None)
        sql = (
            f"SELECT COUNT(*) AS n FROM `{client.project}.{dataset}.{raw_table}` "  # noqa: S608
            "WHERE pull_id = @pull_id"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("pull_id", "STRING", pull_id)]
        )
        row = next(iter(client.query(sql, job_config=job_config).result()), None)
        # A COUNT(*) always returns a row. No row means the result set itself was
        # not readable, which is not a measurement of zero.
        return int(row["n"]) if row is not None else None
    except Exception as exc:  # noqa: BLE001 -- never raises (HG-1)
        logger.warning(
            "verification: count_raw_rows_bigquery_failed pull_id=%s table=%s: %s",
            pull_id,
            raw_table,
            exc,
        )
        return None


def _count_raw_rows_duckdb(pull_id: str, raw_table: str, project_id: str | None) -> int | None:
    """Count this pull's rows in the local DuckDB file (local testing backend).

    Story 24.3 (T3): when TOOROW_ORG_SCHEMAS=1 and *project_id* is given,
    reads from the org raw schema (``org_<wslug>_raw.raw_*``) to match where
    connectors wrote their rows; falls back to unqualified ``main.raw_*``
    when the org is unresolvable (same degradation contract as warehouse_write).
    """
    import duckdb  # noqa: PLC0415

    duckdb_path = os.environ.get("TOOROW_DUCKDB_PATH", "")
    if not duckdb_path or not os.path.exists(duckdb_path):
        logger.warning(
            "verification: duckdb_path not found -- cannot count raw rows pull_id=%s", pull_id
        )
        return None

    # Story 24.3: qualify the table name with the org raw schema when flag ON.
    qualified_table = raw_table  # default: unqualified (reads from main)
    try:
        from core import warehouse_tenancy  # noqa: PLC0415

        if warehouse_tenancy.org_schemas_enabled() and project_id:
            schemas = warehouse_tenancy.resolve_org_schemas(project_id=project_id)
            if schemas is not None:
                # DuckDB identifier folding symmetry: the write path uses SET
                # search_path + unquoted DDL, so DuckDB lowercases every table name
                # at creation time.  Quote BOTH schema AND table here so the reader
                # resolves the same lowercased identifier the writer produced --
                # prevents false-empty verdicts if a module ever uses a mixed-case
                # table name in its DDL (DuckDB folds it; quoting the read side
                # would send a different case).  All current raw_* names are already
                # pure lowercase snake_case, so the quotes are cosmetically neutral
                # but make the symmetry explicit and safe for future changes.
                qualified_table = f'"{schemas.raw}"."{raw_table}"'
    except Exception as exc:  # noqa: BLE001 -- degradation contract
        logger.warning(
            "verification: org schema resolution failed for project_id=%s: %s -- counting in main",
            project_id,
            exc,
        )

    try:
        con = duckdb.connect(duckdb_path, read_only=True)
        try:
            # Table name comes from the module registry / qualified path; not user input.
            row = con.execute(
                f"SELECT COUNT(*) FROM {qualified_table} WHERE pull_id = ?",  # noqa: S608
                [pull_id],
            ).fetchone()
            # A COUNT(*) always returns a row; no row is an unreadable result,
            # not a measured zero.
            return int(row[0]) if row else None
        finally:
            con.close()
    except Exception as exc:
        logger.warning("verification: count_raw_rows_failed pull_id=%s: %s", pull_id, exc)
        return None


# ---------------------------------------------------------------------------
# Connection health red state (AC5)
# ---------------------------------------------------------------------------


def _set_connection_health_red(connection_ref_id: str, verdict: str, pull_id: str) -> None:
    """Upsert connection_health to status='populate_failed' for connection_ref_id.

    Mirrors health_poller._upsert_health() pattern but deliberately does NOT
    update last_fetched_at -- the pull ran (data was attempted), it just landed
    no or too few rows.

    AND IT NAMES THE PULL THAT RAISED IT (migration 276). This flag closes every
    Datastream behind one credential and it is sticky, yet the row used to say
    only `populate_failed`: no pull, no verdict, no date of its own
    (`last_checked_at` is rewritten by every poll cycle). Twice -- 2026-08-12 and
    2026-08-17 -- the only way from "this authorization is red" back to the
    collection that made it red was a hand correlation against the pull ledger.
    Three columns end that, and the enqueue refusal below quotes them.

    Never raises -- logs at WARNING on exception (AI-03 ASCII-only strings).
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    now = datetime.now(tz=timezone.utc)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.connection_health
                        (connection_ref_id, status, last_checked_at,
                         populate_failed_pull_id, populate_failed_verdict,
                         populate_failed_at)
                    VALUES (%(id)s, 'populate_failed', %(now)s,
                            %(pull_id)s, %(verdict)s, %(now)s)
                    ON CONFLICT (connection_ref_id) DO UPDATE
                        SET status          = EXCLUDED.status,
                            last_checked_at = EXCLUDED.last_checked_at,
                            -- The MOST RECENT red wins. An older pull_id left in
                            -- place would send the operator to a collection that
                            -- is no longer the one closing the door.
                            populate_failed_pull_id = EXCLUDED.populate_failed_pull_id,
                            populate_failed_verdict = EXCLUDED.populate_failed_verdict,
                            populate_failed_at      = EXCLUDED.populate_failed_at
                    """,
                    {
                        "id": connection_ref_id,
                        "now": now,
                        "pull_id": pull_id,
                        "verdict": verdict,
                    },
                )
            conn.commit()
        logger.warning(
            "verification: populate_failed: conn=%s verdict=%s pull_id=%s",
            connection_ref_id,
            verdict,
            pull_id,
        )
    except Exception as exc:
        logger.warning("verification: set_health_red_failed conn=%s: %s", connection_ref_id, exc)


def clear_connection_health_red(connection_ref_id: str, *, evidence: str) -> None:
    """A proven read clears the data red flag. THE SYMMETRIC WRITE THAT DID NOT EXIST.

    `_set_connection_health_red` above raises the flag, the poller is forbidden
    from lowering it ("populate_failed is STICKY ... cleared by verification
    writing 'ok' after a successful verified pull"), and nothing wrote that 'ok'.
    So a red flag was permanent: an authorization that once landed no rows could
    never be used again, whatever it proved afterwards. Measured in production
    2026-08-12 -- an authorization whose account had just been verified against
    the provider still refused every run with `access_denied`, and every Fleet row
    read `populate failed`.

    `populate_failed` and `provider_denied` are cleared (AI-341: a verified
    read is exactly the proof that the provider serves this connection's data
    again), and `revoked` is left alone: a dead authorization is not repaired
    by a read that predates its death.

    The three `populate_failed_*` columns of migration 276 go with the status they
    describe: a label naming a pull, left behind on a row that is no longer red,
    would send the next reader to a collection that is not the problem. They are
    nulled in the SAME `CASE`, so the label and the status can never disagree.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.connection_health
                        (connection_ref_id, status, last_checked_at)
                    VALUES (%(id)s, 'ok', %(now)s)
                    ON CONFLICT (connection_ref_id) DO UPDATE
                        -- AI-341: a verified read lifts BOTH pull-raised reds.
                        -- `provider_denied` exists precisely so that this write
                        -- can prove the provider serves data again; `revoked`
                        -- stays, as before -- a dead authorization is not
                        -- repaired by a read that predates its death.
                        SET status = CASE
                                WHEN app.connection_health.status
                                         IN ('populate_failed', 'provider_denied')
                                THEN 'ok'
                                ELSE app.connection_health.status
                            END,
                            last_checked_at = EXCLUDED.last_checked_at,
                            populate_failed_pull_id = CASE
                                WHEN app.connection_health.status = 'populate_failed'
                                THEN NULL
                                ELSE app.connection_health.populate_failed_pull_id
                            END,
                            populate_failed_verdict = CASE
                                WHEN app.connection_health.status = 'populate_failed'
                                THEN NULL
                                ELSE app.connection_health.populate_failed_verdict
                            END,
                            populate_failed_at = CASE
                                WHEN app.connection_health.status = 'populate_failed'
                                THEN NULL
                                ELSE app.connection_health.populate_failed_at
                            END,
                            provider_denied_pull_id = CASE
                                WHEN app.connection_health.status = 'provider_denied'
                                THEN NULL
                                ELSE app.connection_health.provider_denied_pull_id
                            END,
                            provider_denied_at = CASE
                                WHEN app.connection_health.status = 'provider_denied'
                                THEN NULL
                                ELSE app.connection_health.provider_denied_at
                            END
                    """,
                    {"id": connection_ref_id, "now": datetime.now(tz=timezone.utc)},
                )
            conn.commit()
        logger.info(
            "verification: health_red_cleared conn=%s evidence=%s", connection_ref_id, evidence
        )
    except Exception as exc:  # noqa: BLE001 -- the verification stands on its own
        logger.warning("verification: clear_health_red_failed conn=%s: %s", connection_ref_id, exc)


# ---------------------------------------------------------------------------
# Main verification entry point (AC3, AC4)
# ---------------------------------------------------------------------------


def run_post_pull_verification(
    pull_id: str,
    connection_ref_id: str,
    date_from: str,
    date_to: str,
    manifest: dict,
    provider: str | None = None,
    rejected_rows: int = 0,
    project_id: str | None = None,
    report_profile_id: str | None = None,
) -> None:
    """Run post-pull populate verification and write result to app.pull_verifications.

    Called from queue._execute_job() after a successful pull (job state == 'done').
    This function NEVER raises -- any failure is logged at WARNING and the job
    state is left unchanged (HG-1: verification is an annotation, not job control).

    Story 24.3 (T3): accepts *project_id* (default None for backward compat) and
    threads it to ``_count_raw_rows`` so the count targets the same schema the
    connector wrote to (``org_<wslug>_raw`` when flag ON, ``main`` otherwise).

    Verdict logic:
      - the count could not be taken            -> NO verdict is filed at all
      - actual_rows == 0                            -> 'empty'
      - completeness_ratio < VERIFICATION_PARTIAL_THRESHOLD -> 'partial'
      - otherwise                                   -> 'ok'

    When expected_rows == 0, set completeness_ratio = 1.0, verdict = 'ok'
    (zero-expectation pull cannot be 'empty'; avoids ZeroDivisionError).

    When verdict == 'empty' -- AND ONLY THEN -- connection health is set to
    'populate_failed' via _set_connection_health_red() (HG-2: distinct from
    'auth_expired' / 'revoked'). A verdict of 'ok' CLEARS that red -- the
    symmetric write `clear_connection_health_red` was written for and which only
    account verification called, so a red raised by a bad pull could not be
    lifted by a good one however many landed afterwards.

    'partial' FILES ITS VERDICT AND RAISES NOTHING. The red is sticky and the
    enqueue gate reads it, so raising it on a sparse window suspended every
    Datastream of an authorization over a pull that had landed rows -- the
    suspension "Decided -- what a verdict does to a schedule" (AI-101) refuses.
    A partial does not lift the red either: it proves the window renders
    something, not that the collection is healthy.

    AND A PULL WHOSE ROWS COULD NOT BE COUNTED GETS NO VERDICT. `empty` is not the
    absence of a measurement, it is a measurement of nothing -- and it raises a
    sticky flag that refuses every later run of the whole authorization. Filing it
    on a count that never ran is how 344 readable rows closed nine Datastreams
    (2026-08-17), and 112 before them (2026-08-12). There is nothing to record in
    `app.pull_verifications`, whose every column is a measurement, so the pull is
    left unverified and SAID at WARNING -- the instrument that let AI-301 be
    traced back through five days of silence in seconds.

    All log strings are ASCII-only (AI-03).
    """
    from ulid import ULID  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    try:
        # 1. Count actual rows in raw table (24.3: pass project_id for schema routing)
        actual_rows = _count_raw_rows(
            pull_id, provider, project_id=project_id, report_profile_id=report_profile_id
        )
        if actual_rows is None:
            logger.warning(
                "verification: unverified pull_id=%s conn=%s module=%s profile=%s -- the rows "
                "could not be counted, so no verdict is filed and no health red is raised",
                pull_id,
                connection_ref_id,
                provider,
                report_profile_id,
            )
            return

        # 2. Compute expected rows from manifest
        expected_rows = compute_expected_rows(
            manifest, date_from, date_to, report_profile_id=report_profile_id
        )

        # 3. Completeness ratio + verdict (handle zero-division)
        partial_threshold = _partial_threshold()
        if expected_rows == 0:
            completeness_ratio = 1.0
            verdict = "ok"
        elif actual_rows == 0:
            completeness_ratio = 0.0
            verdict = "empty"
        else:
            completeness_ratio = round(actual_rows / expected_rows, 4)
            if completeness_ratio < partial_threshold:
                verdict = "partial"
            else:
                verdict = "ok"

        # 4. Mint ver_ ULID
        ver_id = f"ver_{ULID()}"

        # 5. Insert into app.pull_verifications
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.pull_verifications
                        (id, pull_id, connection_ref_id, expected_rows, actual_rows,
                         completeness_ratio, verdict, rejected_rows)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ver_id,
                        pull_id,
                        connection_ref_id,
                        expected_rows,
                        actual_rows,
                        completeness_ratio,
                        verdict,
                        int(rejected_rows or 0),
                    ),
                )
            conn.commit()

        # 6. Log result
        logger.info(
            "verification: pull_id=%s verdict=%s actual=%d expected=%d ratio=%.4f",
            pull_id,
            verdict,
            actual_rows,
            expected_rows,
            completeness_ratio,
        )

        # 7. Set connection health red on `empty` ONLY -- and LIFT it on `ok`.
        #
        # A PARTIAL NEVER CLOSES THE DOOR (AI-101 + AI-302, 2026-08-17).
        # `populate_failed` is sticky and the enqueue gate admits only `ok` or
        # `stale`, so raising it on `partial` suspended every Datastream behind
        # one credential over a pull THAT HAD LANDED ROWS -- a 12% draw shut the
        # whole authorization. That is precisely the suspension the ratified
        # decision refuses: "Retrieval is never stopped by emptiness", and a
        # sparse window is even less of a reason than an empty one. The `partial`
        # verdict is still filed in `app.pull_verifications` above (HG-1: a
        # verification is an ANNOTATION, never job control), so it stays readable
        # in the day axis, the streaks and the re-collections. It is a quiet fact
        # plus an alert; it is not a closed door.
        #
        # And only `ok` LIFTS. A `partial` proves the window renders SOMETHING,
        # not that the collection is healthy, and the ratified rule reads "A
        # verified pull lifts the red" of the `ok` verdict alone. Letting a stream
        # limping at 12% clear a red raised by a genuinely empty window would
        # re-open the door on the strength of the very evidence that is too thin
        # to raise it -- so a partial neither raises nor lifts.
        #
        # The lift itself is the symmetric write `clear_connection_health_red`
        # describes as its own reason to exist ("cleared by verification writing
        # 'ok' after a successful verified pull"): it was wired to account
        # verification only, so a red raised by one bad window survived every
        # good window after it.
        if verdict == "empty":
            _set_connection_health_red(connection_ref_id, verdict, pull_id)
        elif verdict == "ok" and connection_ref_id:
            clear_connection_health_red(connection_ref_id, evidence=f"verified_pull:{pull_id}")

    except Exception as exc:
        logger.warning(
            "verification: run_post_pull_verification_failed pull_id=%s: %s", pull_id, exc
        )
