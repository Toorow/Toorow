"""toorow -- cleanup rules: a stored PATTERN, compiled to the warehouse's SQL.

A **Cleanup rule** is a name, a collected field, a regular expression and what to
do with the rows that match it. It is what removes the `_TEST_` campaigns from a
reading, or strips a UTM prefix off a placement name.

THE WORD. It is not called a *filter*, and that is a measurement rather than a
preference: `core/query_specs.py:57-58` holds `filter` for a READ filter over a
Semantic View, `core/datastream_intents.py:539` holds it for a COLLECTION filter
pushed to the provider, and `docs/product-architecture/glossary.md` was silent on
both. A third `filter` would have been a third object under one word. The entry
`Cleanup rule` in the glossary names the two senses already taken beside it.

APPLIED AT READ, NEVER AT COLLECTION. `122_derived_columns.sql:23-30` already
decided this for its own object and wrote why: "APPLIED AT READ, NOT AT
INGESTION -- changing a regex costs nothing, no refetch, no 16-month backfill".
A row a cleanup rule removes was still collected and is still in the raw zone,
which is the append-only evidence of AD-7. It also makes the effect COUNTABLE: a
row a provider filtered out is never returned and can never be counted, while a
row this removes is a row we hold.

THE ENGINE IS THE WAREHOUSE'S, NOT PYTHON'S `re`. BigQuery's `REGEXP_CONTAINS`
and DuckDB's `regexp_matches` are both RE2, which is linear by construction: the
catastrophic-backtracking question does not need a process kill to be bounded
here, it does not arise. That is also why the RE2 subset is enforced below --
lookaround and backreferences are refused, because RE2 does not implement them
and a rule accepted here that the engine rejects at read is a rule that fails on
a client's screen instead of in this function.

TWO DIALECTS FROM ONE STORED PATTERN. The fixture chain of this repository is
DuckDB and production is BigQuery, and the repository already polices the mixture
(`tests/core/test_fee_tax_geo_bridge.py:1027` refuses `regexp_full_match` where
BigQuery wants `REGEXP_CONTAINS`). The row stores the pattern; this module emits
both dialects from it, and `tests/conformance/test_cleanup_rule_dialects.py`
proves the same stored pattern compiles in both.

AND THE PATTERN NEVER ENTERS THE SQL STRING. It travels as a bound parameter,
like every other client value on this read path (`warehouse._build_query`:
"User-supplied values NEVER enter the SQL string"). Only identifiers and the
function name are formatted in.

The refusals are raised as :class:`ExpressionError`, declared below. It came from
`core.derived_columns`, whose vocabulary this story reused rather than inventing
a second error family for the same kind of refusal -- how two screens end up
speaking two languages about one failure. That module was the dormant engine of
a path story 60.6 deprecated, and it is now removed (AI-252): the class and
`PreviewResult` were the whole of what any caller still used, so they live here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from core.metric_semantics import _mint_id, _write_semantics_audit

logger = logging.getLogger(__name__)

ID_PREFIX = "crule_"
ENTITY_CLEANUP_RULE = "cleanup_rule"

#: The bound migration 122:69-72 carried and migration 125:89-93 dropped with the
#: column. It is stated here AND in the CHECK of migration 240, because a bound
#: that lives only in Python is a bound a second writer forgets.
MAX_PATTERN_LENGTH = 500

#: The relation cleanup rules read. It is the mart, not a raw table: AD-12 states
#: "reads marts only -- never raw_* tables", and this rule applies at READ.
MART_RELATION = "fact_daily_kpi"

#: How many days the measured effect covers. `epic-60:99-100` asks for "how many
#: rows it removes over the last 30 days" and this is that window.
EFFECT_WINDOW_DAYS = 30

#: The preview bound of `derived_columns.py:280-281`, for the same reason: a
#: preview is evidence, and unbounded evidence is a scan.
MIN_PREVIEW_ROWS = 1
MAX_PREVIEW_ROWS = 200

RULE_KINDS = ("exclude_row", "keep_row", "strip_match")
DIALECTS = ("bigquery", "duckdb")

#: A field name is a plain identifier. Same discipline as
#: `derived_columns.py:235-238`: an identifier that could break out of its
#: quoting is impossible by construction rather than by escaping.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

#: What RE2 does not implement. Accepting one of these would store a rule the
#: warehouse refuses at read time -- a failure on a client's screen instead of in
#: this function.
_NOT_IN_RE2: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\(\?=|\(\?!"),
        "lookahead is not part of RE2, which is the engine both warehouses use",
    ),
    (
        re.compile(r"\(\?<=|\(\?<!"),
        "lookbehind is not part of RE2, which is the engine both warehouses use",
    ),
    (
        re.compile(r"\\[1-9]"),
        "a backreference is not part of RE2, which is the engine both warehouses use",
    ),
)


class ExpressionError(ValueError):
    """The expression cannot be stored or compiled safely.

    AI-252: this class and `PreviewResult` below used to live in
    `core.derived_columns`, a 426-line module with no other caller, no dbt model
    invoking its macro, a table at zero rows and a BigQuery-only dialect --
    deprecated by story 60.6 and removed here. These two were the whole of what
    survived it, so they move to the one file that uses them rather than keeping
    a module alive to hold them.

    They are NOT re-exported from a compatibility shim: a second expression path
    beside `semantic_expressions` (60.2) is exactly what 60.6 removed, and a
    lingering import site is how one comes back.
    """


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """What a preview call answers.

    `error` is set instead of rows when the warehouse refused the expression --
    never both, and never a partial render presented as a successful one.
    """

    rows: tuple[dict, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class CleanupRuleNotFound(ExpressionError):
    """The rule does not exist inside the guarded Project."""


class CleanupRuleConflict(ExpressionError):
    """A unique constraint refused the write (two rules, one name, one reach)."""


class CleanupRuleImpactUnavailable(ExpressionError):
    """The reach of the rule could not be counted. Fail closed -- never a zero."""


# ---------------------------------------------------------------------------
# PURE half: validation, plain language, and the two dialects.
# ---------------------------------------------------------------------------


def validate_pattern(pattern: str) -> str:
    """Refuse a pattern before it is stored, with a sentence naming what is wrong.

    Raises:
        ExpressionError: empty, over :data:`MAX_PATTERN_LENGTH`, multi-line,
            syntactically invalid, or using a construct RE2 does not implement.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise ExpressionError("the pattern is empty")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ExpressionError(f"the pattern exceeds {MAX_PATTERN_LENGTH} characters")
    if "\n" in pattern or "\r" in pattern:
        raise ExpressionError("the pattern must be a single line")

    for forbidden, reason in _NOT_IN_RE2:
        if forbidden.search(pattern):
            raise ExpressionError(f"not allowed in a cleanup rule: {reason}")

    # Syntax only. Python's `re` is a SUPERSET of RE2, so this catches a broken
    # expression and never approves one on RE2's behalf -- the constructs the two
    # disagree about are the ones refused just above, and the warehouse's own dry
    # run is the authority that follows.
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ExpressionError(f"the pattern is not a valid regular expression: {exc}") from exc
    return pattern


def validate_kind(rule_kind: str) -> str:
    if rule_kind not in RULE_KINDS:
        raise ExpressionError(f"unknown cleanup rule kind: {rule_kind!r}. One of {RULE_KINDS}")
    return rule_kind


def validate_field(source_field: str) -> str:
    if not isinstance(source_field, str) or not _IDENTIFIER.fullmatch(source_field.strip()):
        raise ExpressionError(
            "the field must start with a letter or underscore and contain only "
            "letters, digits and underscores"
        )
    return source_field.strip()


def describe(rule_kind: str, source_field: str, pattern: str) -> str:
    """The condition in words, for a person who does not read regular expressions.

    It is the sentence the screen shows beside the rule. It says what SURVIVES,
    because that is the question a person asks of a cleanup rule.
    """
    validate_kind(rule_kind)
    if rule_kind == "exclude_row":
        return f"Keeps a row only when {source_field} does not match `{pattern}`."
    if rule_kind == "keep_row":
        return f"Keeps a row only when {source_field} matches `{pattern}`."
    return f"Removes from {source_field} every part that matches `{pattern}`."


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """One rule, in one dialect, with the parameters its SQL expects.

    `sql` never contains the pattern: it carries a placeholder, and `params` is
    what the driver binds to it.
    """

    dialect: str
    #: TRUE for a row whose field matches the pattern.
    match_sql: str
    #: TRUE for a row the rule KEEPS. `None` for a rule that rewrites a field
    #: instead of dropping a row.
    keep_sql: str | None
    #: TRUE for a row the rule CHANGES -- removed, or rewritten. This is what the
    #: measured effect counts.
    affected_sql: str
    #: The projected value of the field after the rule. `None` for a rule that
    #: does not rewrite it.
    projection_sql: str | None
    #: What `match_sql` (and therefore `keep_sql` and `affected_sql`) binds.
    match_params: tuple[str, ...]
    #: What `projection_sql` binds. It is a SECOND occurrence of the same pattern
    #: and not a duplicate to be deduplicated: DuckDB counts its placeholders, so
    #: a statement carrying both fragments must bind the pattern twice or the
    #: driver refuses it. A real DuckDB caught exactly that
    #: (`tests/conformance/test_cleanup_rule_dialects.py`).
    projection_params: tuple[str, ...] = ()

    @property
    def params(self) -> tuple[str, ...]:
        """Every placeholder this rule emitted, in the order it emitted them."""
        return (*self.match_params, *self.projection_params)


def quote_identifier(dialect: str, identifier: str) -> str:
    """How each warehouse quotes a NAME. One owner for the two spellings.

    Public since story 70.1: `schema_split_compiler` emits a second family of
    two-dialect SQL, and a second private copy of these two lines is how one
    module ends up quoting with backticks where the other quotes with double
    quotes -- the exact mixture `test_cleanup_rule_dialects.py` exists to refuse.
    """
    if dialect not in DIALECTS:
        raise ExpressionError(f"unknown dialect: {dialect!r}. One of {DIALECTS}")
    return f"`{identifier}`" if dialect == "bigquery" else f'"{identifier}"'


def placeholder(dialect: str, index: int) -> str:
    """BigQuery names its parameters, DuckDB counts them -- the convention of
    `warehouse._build_query`, which is what will execute this."""
    if dialect not in DIALECTS:
        raise ExpressionError(f"unknown dialect: {dialect!r}. One of {DIALECTS}")
    return f"@p{index}" if dialect == "bigquery" else "?"


def _quote(dialect: str, identifier: str) -> str:
    return quote_identifier(dialect, identifier)


def _placeholder(dialect: str, index: int) -> str:
    return placeholder(dialect, index)


def compile_rule(
    *,
    rule_kind: str,
    source_field: str,
    pattern: str,
    dialect: str,
    value_column: str = "breakdown_value",
    first_param: int = 0,
) -> CompiledRule:
    """Emit the SQL of one rule for one dialect, pattern bound not inlined.

    `first_param` is where this fragment's placeholders start, so a caller that
    already bound a project and a window keeps one continuous numbering for
    BigQuery.
    """
    if dialect not in DIALECTS:
        raise ExpressionError(f"unknown dialect: {dialect!r}. One of {DIALECTS}")
    validate_kind(rule_kind)
    validate_pattern(pattern)
    column = _quote(dialect, validate_field(value_column))
    # Validated, not formatted in: the field is the `breakdown_dimension` VALUE
    # the higher-level builders bind in their WHERE clause, so it never becomes an
    # identifier here. It is still refused early, because a rule naming a field
    # this module would refuse later is a rule that fails after the dry run.
    validate_field(source_field)

    pattern_ref = _placeholder(dialect, first_param)
    # The projection binds its OWN placeholder rather than reusing the match's:
    # BigQuery would accept the same name twice, DuckDB counts and would refuse.
    projection_ref = _placeholder(dialect, first_param + 1)
    if dialect == "bigquery":
        match_sql = f"REGEXP_CONTAINS({column}, {pattern_ref})"
        projection = (
            f"REGEXP_REPLACE({column}, {projection_ref}, '')"
            if rule_kind == "strip_match"
            else None
        )
    else:
        match_sql = f"regexp_matches({column}, {pattern_ref})"
        projection = (
            f"regexp_replace({column}, {projection_ref}, '', 'g')"
            if rule_kind == "strip_match"
            else None
        )

    if rule_kind == "exclude_row":
        keep_sql: str | None = f"NOT ({match_sql})"
        affected_sql = match_sql
    elif rule_kind == "keep_row":
        keep_sql = match_sql
        affected_sql = f"NOT ({match_sql})"
    else:
        keep_sql = None
        affected_sql = match_sql

    return CompiledRule(
        dialect=dialect,
        match_sql=match_sql,
        keep_sql=keep_sql,
        affected_sql=affected_sql,
        projection_sql=projection,
        match_params=(pattern,),
        projection_params=(pattern,) if projection is not None else (),
    )


def compile_strip_expression(
    *, pattern: str, dialect: str, value_sql: str, first_param: int
) -> tuple[str, tuple[str, ...]]:
    """One `strip_match` rewrite over an arbitrary value EXPRESSION, one dialect.

    `compile_rule` quotes its ``value_column`` as an identifier, which is right
    for a rule standing alone but cannot COMPOSE: a second strip rule must
    rewrite the output of the first, and an output is an expression, not an
    identifier. This is the same two-dialect emission as
    :attr:`CompiledRule.projection_sql` with the value slot handed to the caller
    raw -- it lives HERE so the dialect vocabulary keeps one owner
    (`tests/conformance/test_cleanup_rule_dialects.py` polices both emissions).

    ``value_sql`` is SQL the caller already emitted (never client input); the
    pattern still travels as the bound parameter this function returns.
    """
    if dialect not in DIALECTS:
        raise ExpressionError(f"unknown dialect: {dialect!r}. One of {DIALECTS}")
    validate_pattern(pattern)
    pattern_ref = _placeholder(dialect, first_param)
    if dialect == "bigquery":
        return f"REGEXP_REPLACE({value_sql}, {pattern_ref}, '')", (pattern,)
    return f"regexp_replace({value_sql}, {pattern_ref}, '', 'g')", (pattern,)


def _relation(mart_prefix: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.]*", mart_prefix or ""):
        raise ExpressionError("invalid mart prefix")
    return f"{mart_prefix}{MART_RELATION}"


def build_dry_run_sql(
    *, rule_kind: str, source_field: str, pattern: str, dialect: str, mart_prefix: str
) -> tuple[str, list[str]]:
    """The SELECT submitted to the warehouse BEFORE the rule is stored.

    Same shape and same motive as `derived_columns.build_dry_run_sql:227-239`:
    `WHERE FALSE` reads no row and is billed nothing, and the engine that will
    run the expression is the one that approves it -- dialect, function
    signatures and types all answered by the thing that decides them.
    """
    compiled = compile_rule(
        rule_kind=rule_kind, source_field=source_field, pattern=pattern, dialect=dialect
    )
    return (
        f"SELECT {compiled.affected_sql} AS affected FROM {_relation(mart_prefix)} WHERE FALSE",
        # Only the match fragment is in this statement, so only its parameter is
        # bound: a positional driver counts what it sees, not what was compiled.
        list(compiled.match_params),
    )


def build_preview_sql(
    *,
    rule_kind: str,
    source_field: str,
    pattern: str,
    dialect: str,
    mart_prefix: str,
    project_id: str,
    limit: int = 20,
) -> tuple[str, list[str]]:
    """Real rows, beside what the rule does to them.

    The source value is carried through on purpose, for the reason
    `derived_columns.build_preview_sql:269-274` gives: a result shown alone is
    unreadable, and a wrong pattern is obvious next to the string it judged.
    """
    if not MIN_PREVIEW_ROWS <= limit <= MAX_PREVIEW_ROWS:
        raise ExpressionError(
            f"the preview is limited to between {MIN_PREVIEW_ROWS} and "
            f"{MAX_PREVIEW_ROWS} rows"
        )
    # The rule's placeholders come FIRST because they appear first in the text:
    # DuckDB binds `?` by order of appearance, and the projection sits in the
    # SELECT list, ahead of the WHERE clause. Numbering them the other way round
    # bound the project id to the pattern -- a real DuckDB refused it, which is
    # why `tests/conformance/test_cleanup_rule_dialects.py` executes rather than
    # only reads the string.
    compiled = compile_rule(
        rule_kind=rule_kind,
        source_field=source_field,
        pattern=pattern,
        dialect=dialect,
        first_param=0,
    )
    bound = len(compiled.params)
    project_ref = _placeholder(dialect, bound)
    field_ref = _placeholder(dialect, bound + 1)
    projection = (
        f", {compiled.projection_sql} AS becomes" if compiled.projection_sql is not None else ""
    )
    sql = (
        f"SELECT breakdown_value, {compiled.affected_sql} AS affected{projection} "
        f"FROM {_relation(mart_prefix)} "
        f"WHERE project_id = {project_ref} AND breakdown_dimension = {field_ref} "
        f"LIMIT {int(limit)}"
    )
    return sql, [*compiled.params, project_id, source_field]


def build_effect_sql(
    *,
    rule_kind: str,
    source_field: str,
    pattern: str,
    dialect: str,
    mart_prefix: str,
    project_id: str,
    start_date: str,
    end_date: str,
) -> tuple[str, list[str]]:
    """How many rows this rule changes over its window -- counted, not estimated.

    This is only answerable because the rule applies at READ: a row a provider
    filtered out at collection is never returned and can never be counted, which
    is exactly what `epic-60:102-104` says about irrecoverability.
    """
    compiled = compile_rule(
        rule_kind=rule_kind,
        source_field=source_field,
        pattern=pattern,
        dialect=dialect,
        first_param=4,
    )
    refs = [_placeholder(dialect, index) for index in range(4)]
    sql = (
        f"SELECT COUNT(*) AS affected_rows FROM {_relation(mart_prefix)} "
        f"WHERE project_id = {refs[0]} AND date BETWEEN {refs[1]} AND {refs[2]} "
        f"AND breakdown_dimension = {refs[3]} AND {compiled.affected_sql}"
    )
    return sql, [project_id, start_date, end_date, source_field, *compiled.match_params]


def preview_rule(
    *,
    rule_kind: str,
    source_field: str,
    pattern: str,
    dialect: str,
    mart_prefix: str,
    project_id: str,
    run_query,
    limit: int = 20,
) -> PreviewResult:
    """Validate, then render the rule on real rows through the real engine.

    Two failures are told apart, because they need different fixes: a structural
    refusal raised before anything is sent, and the warehouse refusing the SQL,
    returned as `error`. Neither ever yields a fabricated row -- the reason
    `derived_columns.preview_expression:304-308` states, and the reason this
    reuses its result type rather than inventing a second one.
    """
    sql, params = build_preview_sql(
        rule_kind=rule_kind,
        source_field=source_field,
        pattern=pattern,
        dialect=dialect,
        mart_prefix=mart_prefix,
        project_id=project_id,
        limit=limit,
    )
    try:
        rows = run_query(sql, params)
    except Exception as exc:  # noqa: BLE001 -- the engine's own words are the answer
        return PreviewResult(rows=(), error=str(exc)[:500])
    return PreviewResult(rows=tuple(rows or ()))


# ---------------------------------------------------------------------------
# Store. Every write goes through validate_pattern first, so a rule no engine
# could run can never be stored.
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _rows(cur) -> list[dict[str, Any]]:
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _is_unique_violation(exc: Exception) -> bool:
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate == "23505":
        return True
    text = str(exc).lower()
    return "duplicate key" in text or "unique constraint" in text


def _projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "project_id": str(row["project_id"]),
        "datastream_id": row.get("datastream_id"),
        "name": row["name"],
        "source_field": row["source_field"],
        "rule_kind": row["rule_kind"],
        "pattern": row["pattern"],
        "enabled": bool(row["enabled"]),
        "dry_run_state": row["dry_run_state"],
        "dry_run_detail": row.get("dry_run_detail"),
        "condition": describe(row["rule_kind"], row["source_field"], row["pattern"]),
        "created_by": row.get("created_by"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        # The head of the rule's own history (Story 60.5, migration 242). NULL
        # only for a rule written before that migration -- nothing backfills it.
        "current_version_id": row.get("current_version_id"),
        "datastream_count": (
            int(row["datastream_count"]) if row.get("datastream_count") is not None else None
        ),
    }


#: The list read. The reach is counted by the SAME query as the row, so a rule is
#: never listed beside a count taken at another moment. A rule bound to one
#: Datastream reaches one; a rule bound to none reaches every Datastream of the
#: Project, and that number is COUNTED rather than assumed.
LIST_RULES_SQL = """
    SELECT r.id, r.org_id, r.project_id, r.datastream_id, r.name, r.source_field,
           r.rule_kind, r.pattern, r.enabled, r.dry_run_state, r.dry_run_detail,
           r.created_by, r.created_at, r.updated_at, r.current_version_id,
           CASE WHEN r.datastream_id IS NOT NULL THEN 1 ELSE p.datastream_count END
               AS datastream_count
      FROM app.cleanup_rules r
      LEFT JOIN LATERAL (
          SELECT COUNT(*) AS datastream_count
            FROM app.datastreams d
           WHERE d.project_id = r.project_id
      ) p ON TRUE
     WHERE r.project_id = %(project_id)s
     ORDER BY lower(r.name), r.id
"""

GET_RULE_SQL = LIST_RULES_SQL.replace(
    "WHERE r.project_id = %(project_id)s",
    "WHERE r.project_id = %(project_id)s AND r.id = %(rule_id)s",
)

#: What the Processing tab reads: the rules that reach ONE Datastream, its own
#: plus the Project-wide ones. Read-only there -- `datastream-workbench-and-
#: wizard.md:989` says governed rules are referenced, not edited.
LIST_FOR_DATASTREAM_SQL = """
    SELECT id, name, source_field, rule_kind, pattern, enabled, datastream_id,
           dry_run_state, updated_at
      FROM app.cleanup_rules
     WHERE project_id = %(project_id)s
       AND (datastream_id IS NULL OR datastream_id = %(datastream_id)s)
     ORDER BY lower(name), id
"""


def list_rules(conn, *, project_id: str) -> list[dict[str, Any]]:
    """Project-scoped by construction (AD-5): a wrong Project returns nothing."""
    with conn.cursor() as cur:
        cur.execute(LIST_RULES_SQL, {"project_id": project_id})
        return [_projection(row) for row in _rows(cur)]


def get_rule(conn, *, rule_id: str, project_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(GET_RULE_SQL, {"rule_id": rule_id, "project_id": project_id})
        rows = _rows(cur)
    if not rows:
        raise CleanupRuleNotFound(rule_id)
    return _projection(rows[0])


#: Story 60.6. Named so the rollback names the same point the read opened.
_DATASTREAM_CHAIN_SAVEPOINT = "cleanup_rules_datastream_chain"


def _savepoint(conn, statement: str) -> bool:
    """Run one savepoint statement, best effort. `False` if it did not take.

    Never raises. `ROLLBACK TO SAVEPOINT` is one of the two statements PostgreSQL
    still accepts on a poisoned transaction; `SAVEPOINT` outside a transaction
    block is refused, which is the autocommit case where nothing can be poisoned.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    except Exception as exc:  # noqa: BLE001 -- a savepoint is a precaution, not a result
        logger.debug("cleanup_rules: %s unavailable: %s", statement, exc)
        return False
    return True


def read_datastream_chain(
    conn, *, project_id: str, datastream_id: str
) -> list[dict[str, Any]] | None:
    """The chain, or `None` -- AND A CONNECTION THAT IS STILL USABLE EITHER WAY.

    Story 60.3 gave the Processing tab three sentences (`available`, `empty`,
    `unavailable`) but put the fail-soft in the READER, as a bare `try/except` in
    `datastream_workbench`. That is the shape `dq_governance.read_open_issue_counts`
    documents at length as insufficient: a statement that fails leaves its
    transaction aborted, so the swallow protects the one key that would have
    degraded anyway and takes down whatever is read NEXT on the same connection --
    here, the capability projection two lines below it.

    A SAVEPOINT un-poisons it. `None` means the store could not be read; `[]`
    means it answered and no rule reaches this Datastream. The tab renders two
    different sentences, because "no rule" reads as "nothing is removed from my
    data" and that is not what an outage means.
    """
    marked = _savepoint(conn, f"SAVEPOINT {_DATASTREAM_CHAIN_SAVEPOINT}")
    try:
        rules = list_rules_for_datastream(
            conn, project_id=project_id, datastream_id=datastream_id
        )
    except Exception as exc:  # noqa: BLE001 -- one unreadable store takes down no screen
        logger.warning(
            "cleanup_rules: datastream chain unavailable ds=%s: %s", datastream_id, exc
        )
        if marked:
            _savepoint(conn, f"ROLLBACK TO SAVEPOINT {_DATASTREAM_CHAIN_SAVEPOINT}")
        return None
    if marked:
        _savepoint(conn, f"RELEASE SAVEPOINT {_DATASTREAM_CHAIN_SAVEPOINT}")
    return rules


def list_rules_for_datastream(conn, *, project_id: str, datastream_id: str) -> list[dict[str, Any]]:
    """The read-only chain of one Datastream: its rules plus the Project-wide ones."""
    with conn.cursor() as cur:
        cur.execute(
            LIST_FOR_DATASTREAM_SQL,
            {"project_id": project_id, "datastream_id": datastream_id},
        )
        rows = _rows(cur)
    return [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "source_field": row["source_field"],
            "rule_kind": row["rule_kind"],
            "enabled": bool(row["enabled"]),
            "scope": "datastream" if row.get("datastream_id") else "project",
            "dry_run_state": row["dry_run_state"],
            "condition": describe(row["rule_kind"], row["source_field"], row["pattern"]),
            "updated_at": _iso(row.get("updated_at")),
        }
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class RuleImpact:
    """How many Datastreams a rule reaches right now.

    An answer of zero means the store answered and the Project has no Datastream.
    It never means the count could not be taken: :func:`assess_rule_impact` raises
    in that case rather than returning a reassuring number
    (`value_mapping_tables.assess_table_impact:258-264`, same discipline).
    """

    datastream_count: int
    datastream_ids: tuple[str, ...]
    #: The same Datastreams, BY NAME. Story 60.5: `epic-60:55-56` asks a change to
    #: "say which ones before confirming", and an identifier is not a name. The
    #: value table side already answered with names (`assess_table_impact:284-291`);
    #: this is the same answer for the family that did not have it.
    datastreams: tuple[dict[str, Any], ...] = ()

    def describe(self) -> str:
        count = self.datastream_count
        return f"{count} Datastream{'' if count == 1 else 's'}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "impact_state": "known",
            "datastream_count": self.datastream_count,
            "datastream_ids": list(self.datastream_ids),
            "datastreams": [dict(entry) for entry in self.datastreams],
        }


def assess_rule_impact(
    conn, *, project_id: str, datastream_id: str | None
) -> RuleImpact:
    """The Datastreams a rule would reach -- read BEFORE the change, never after.

    Named as well as counted (Story 60.5): a rule whose `datastream_id` is NULL
    reaches EVERY Datastream of its Project, and "six flows change tonight" is
    not an answer until the six are named.

    Raises:
        CleanupRuleImpactUnavailable: the count could not be taken. Every caller
            fails closed; the surface says "unknown" and never a zero.
    """
    try:
        with conn.cursor() as cur:
            if datastream_id:
                cur.execute(
                    "SELECT id, name FROM app.datastreams WHERE id = %s AND project_id = %s",
                    (datastream_id, project_id),
                )
            else:
                cur.execute(
                    "SELECT id, name FROM app.datastreams WHERE project_id = %s "
                    "ORDER BY lower(name), id",
                    (project_id,),
                )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- an unreadable store is not a zero
        raise CleanupRuleImpactUnavailable(
            f"the Datastreams of project {project_id} could not be counted: {type(exc).__name__}"
        ) from exc
    ids = tuple(str(row[0]) for row in rows)
    return RuleImpact(
        datastream_count=len(ids),
        datastream_ids=ids,
        datastreams=tuple(
            {"datastream_id": str(row[0]), "datastream_name": row[1]} for row in rows
        ),
    )


def record_rule_version(conn, *, rule_id: str, project_id: str, identity: str) -> dict | None:
    """Record where this rule now stands, in the SAME transaction as the write.

    The body is the name, the field, the kind, the pattern and the reach. It is
    NOT `enabled`: switching a rule off is its lifecycle, the way
    `lifecycle_status` is a DQ Monitor's and lives on the parent in migration 145.

    Returns the version, or ``None`` when the rule was already at this exact body
    -- an edit that changed nothing records nothing, which is not a failure.
    """
    from core.rule_versions import (  # noqa: PLC0415 -- one ledger authority, imported late
        KIND_CLEANUP_RULE,
        RuleVersionUnchanged,
        cleanup_rule_body,
        record_version,
    )

    rule = get_rule(conn, rule_id=rule_id, project_id=project_id)
    try:
        return record_version(
            conn,
            kind=KIND_CLEANUP_RULE,
            object_id=rule["id"],
            org_id=rule["org_id"],
            project_id=project_id,
            body=cleanup_rule_body(rule),
            identity=identity,
        )
    except RuleVersionUnchanged:
        return None


def create_rule(
    conn,
    *,
    org_id: str,
    project_id: str,
    datastream_id: str | None,
    name: str,
    source_field: str,
    rule_kind: str,
    pattern: str,
    identity: str,
    dry_run_state: str,
    dry_run_detail: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Validate, then insert. Raises before touching the database.

    `dry_run_state` is decided by the CALLER, because only the caller can reach a
    warehouse. It is `passed` when an engine accepted the compiled SELECT and
    `not_attempted` when none could be reached -- and a dry run that FAILED never
    arrives here at all, because the API refuses the write with the engine's own
    message.
    """
    if not (name or "").strip():
        raise ExpressionError("the rule needs a name")
    if dry_run_state not in ("passed", "not_attempted"):
        raise ExpressionError(f"unknown dry run state: {dry_run_state!r}")
    validate_kind(rule_kind)
    validate_field(source_field)
    validate_pattern(pattern)

    rule_id = _mint_id(ID_PREFIX)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.cleanup_rules
                    (id, org_id, project_id, datastream_id, name, source_field,
                     rule_kind, pattern, enabled, dry_run_state, dry_run_detail, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    rule_id,
                    org_id,
                    project_id,
                    datastream_id,
                    name.strip(),
                    source_field.strip(),
                    rule_kind,
                    pattern,
                    enabled,
                    dry_run_state,
                    dry_run_detail,
                    identity,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            raise CleanupRuleConflict(
                "A cleanup rule already carries this name on this reach."
            ) from exc
        raise
    _write_semantics_audit(
        conn,
        identity=identity,
        action="created",
        entity_type=ENTITY_CLEANUP_RULE,
        entity_id=rule_id,
        scope_level="PROJECT",
        org_id=org_id,
        project_id=project_id,
        before=None,
        after={
            "name": name.strip(),
            "source_field": source_field.strip(),
            "rule_kind": rule_kind,
            "pattern": pattern,
            "datastream_id": datastream_id,
            "dry_run_state": dry_run_state,
        },
    )
    # Version 1 is written HERE and not on the first edit: without it, the first
    # edit would have no predecessor and "what the pattern was yesterday" would
    # start one change too late.
    record_rule_version(conn, rule_id=rule_id, project_id=project_id, identity=identity)
    return get_rule(conn, rule_id=rule_id, project_id=project_id)


def update_rule(
    conn,
    *,
    rule_id: str,
    project_id: str,
    identity: str,
    name: str | None = None,
    pattern: str | None = None,
    rule_kind: str | None = None,
    source_field: str | None = None,
    enabled: bool | None = None,
    dry_run_state: str | None = None,
    dry_run_detail: str | None = None,
) -> dict[str, Any]:
    """Change a rule. Every new pattern is validated exactly as a new one is."""
    before = get_rule(conn, rule_id=rule_id, project_id=project_id)
    new_name = (name or "").strip() or before["name"]
    new_pattern = pattern if pattern is not None else before["pattern"]
    new_kind = rule_kind or before["rule_kind"]
    new_field = (source_field or "").strip() or before["source_field"]
    new_enabled = before["enabled"] if enabled is None else bool(enabled)
    new_state = dry_run_state or before["dry_run_state"]
    if new_state not in ("passed", "not_attempted"):
        raise ExpressionError(f"unknown dry run state: {new_state!r}")
    validate_kind(new_kind)
    validate_field(new_field)
    validate_pattern(new_pattern)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.cleanup_rules
                   SET name = %s, pattern = %s, rule_kind = %s, source_field = %s,
                       enabled = %s, dry_run_state = %s, dry_run_detail = %s
                 WHERE id = %s AND project_id = %s
                """,
                (
                    new_name,
                    new_pattern,
                    new_kind,
                    new_field,
                    new_enabled,
                    new_state,
                    dry_run_detail if dry_run_detail is not None else before["dry_run_detail"],
                    rule_id,
                    project_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            raise CleanupRuleConflict(
                "A cleanup rule already carries this name on this reach."
            ) from exc
        raise
    _write_semantics_audit(
        conn,
        identity=identity,
        action="upserted",
        entity_type=ENTITY_CLEANUP_RULE,
        entity_id=rule_id,
        scope_level="PROJECT",
        org_id=before["org_id"],
        project_id=project_id,
        before={k: before[k] for k in ("name", "pattern", "rule_kind", "source_field", "enabled")},
        after={
            "name": new_name,
            "pattern": new_pattern,
            "rule_kind": new_kind,
            "source_field": new_field,
            "enabled": new_enabled,
        },
    )
    # A toggle alone records nothing: `enabled` is not part of the body, so
    # `record_rule_version` finds the rule already at its current version and
    # says so rather than writing a second identical row.
    record_rule_version(conn, rule_id=rule_id, project_id=project_id, identity=identity)
    return get_rule(conn, rule_id=rule_id, project_id=project_id)


def delete_rule(conn, *, rule_id: str, project_id: str, identity: str) -> dict[str, Any]:
    """Remove a rule. Nothing collected is touched: the rule applied at READ, so
    the rows it was removing come back on the next reading, unchanged."""
    before = get_rule(conn, rule_id=rule_id, project_id=project_id)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.cleanup_rules WHERE id = %s AND project_id = %s",
            (rule_id, project_id),
        )
    _write_semantics_audit(
        conn,
        identity=identity,
        action="deleted",
        entity_type=ENTITY_CLEANUP_RULE,
        entity_id=rule_id,
        scope_level="PROJECT",
        org_id=before["org_id"],
        project_id=project_id,
        before={"name": before["name"], "pattern": before["pattern"]},
        after=None,
    )
    return {"deleted": True, "rule_id": rule_id}


__all__ = [
    "DIALECTS",
    "EFFECT_WINDOW_DAYS",
    "ENTITY_CLEANUP_RULE",
    "GET_RULE_SQL",
    "ID_PREFIX",
    "LIST_FOR_DATASTREAM_SQL",
    "LIST_RULES_SQL",
    "MART_RELATION",
    "MAX_PATTERN_LENGTH",
    "MAX_PREVIEW_ROWS",
    "MIN_PREVIEW_ROWS",
    "RULE_KINDS",
    "CleanupRuleConflict",
    "CleanupRuleImpactUnavailable",
    "CleanupRuleNotFound",
    "CompiledRule",
    "ExpressionError",
    "PreviewResult",
    "RuleImpact",
    "assess_rule_impact",
    "build_dry_run_sql",
    "build_effect_sql",
    "build_preview_sql",
    "compile_rule",
    "compile_strip_expression",
    "create_rule",
    "delete_rule",
    "describe",
    "get_rule",
    "list_rules",
    "list_rules_for_datastream",
    "placeholder",
    "quote_identifier",
    "read_datastream_chain",
    "record_rule_version",
    "preview_rule",
    "update_rule",
    "validate_field",
    "validate_kind",
    "validate_pattern",
]
