"""toorow -- cleanup rules reach the served warehouse read (AI-260, second half).

A cleanup rule is written on ``(project_id, datastream_id NULLABLE, source_field)``
(migration 240, read here through `cleanup_rules.LIST_RULES_SQL`'s own module).
``fact_daily_kpi`` carries NO ``datastream_id`` -- two marts state it in their own
headers (`fee_tax_country_resolution.sql`, `fee_tax_rules_effective.sql`) -- so a
served read over the fact keys on **connector**. This module crosses the same
bridge `core.value_table_resolution` crossed for the first half of AI-260, with
the same ratified rule, quoted from `fee_tax_country_resolution.sql:304-305`:

    "two datastreams of one connector that do not resolve to the SAME source type
    is an AMBIGUITY -- a gap, never a pick."

Transposed to cleanup rules: a Datastream-scoped rule applies to connector-keyed
rows only when EVERY live Datastream of that connector carries the same rule body
(kind + pattern on the field). A body carried by some Datastreams and not others
would remove or rewrite rows of a Datastream the client never scoped it to -- a
pick. It comes back as a NAMED gap (which rule, which Datastreams are missing),
and the rule is compiled into no statement. A project-wide rule (``datastream_id``
NULL) reaches every Datastream by construction and applies directly.

THE RELATION READ IS THE ONE THE MIRROR IS MADE FROM. `app.datastreams_dim_v`
(migration 119:532) is exactly what `mirror_sync` copies into
`mirror.datastreams_dim`, live rows only -- the same discipline as
`value_table_resolution._connector_datastreams`, so the two halves of AI-260
resolve the SAME set of Datastreams.

TWO HALVES, TWO PLANES. Resolution (this file's first half) runs on the control
plane and emits no warehouse SQL. Compilation (the second half) emits WAREHOUSE
SQL, so the two-dialect obligation of story 60.3 binds it fully: every fragment
comes from `core.cleanup_rules` (`compile_rule`, `compile_strip_expression`), the
pattern travels as a bound parameter, and
`tests/conformance/test_cleanup_application_dialects.py` executes the DuckDB
emission against a real engine and holds the BigQuery emission to the banned-token
discipline of `test_cleanup_rule_dialects.py`.

WHAT A RULE SEES. Row rules (`exclude_row` / `keep_row`) test the RAW collected
value -- the value the client wrote the pattern against ("Keeps a row only when
<field> does not match", `cleanup_rules.describe`). `strip_match` rules rewrite
the served value, composing in the store's own deterministic order (project-wide
rules first, in `LIST_RULES_SQL` order; then connector-resolved bodies). The
cleaned value is what every later stage receives: the client value mapping table
of the first half translates the value cleanup has already produced, because a
value table "exists to normalise a column before anything maps it"
(`docs/product-architecture/governance.md:166`) and what it normalises is the
column AS SERVED.

AN OUTAGE IS NOT "NO RULE". When the store cannot be read the state is
``unavailable``, never ``none`` -- "I could not check" and "nothing is removed
from my data" are different facts (`cleanup_rules.read_datastream_chain`, same
sentence). The caller decides what an outage renders -- the served read proceeds
UNCLEANED and NAMES the outage, because refusing every spend read over a
governance-store outage would take down the honest majority of reads that no rule
touches. A gap, by contrast, is a measured disagreement: the rule applies
nowhere, and the disagreement is named, never resolved by either side.

Read-only, org-unguarded like the first half: the calling surface has checked
access; the rules and Datastreams read are the project's own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.cleanup_rules import (
    _placeholder,
    _quote,
    compile_rule,
    compile_strip_expression,
    validate_field,
)

logger = logging.getLogger(__name__)

#: The four states an application can land in, the same words as the first half.
#: ``none`` means the stores answered and no enabled rule names this field.
STATE_APPLIED = "applied"
STATE_NONE = "none"
STATE_AMBIGUOUS = "ambiguous"
STATE_UNAVAILABLE = "unavailable"

#: Typed reasons, one word each, so a surface can branch without parsing prose.
REASON_NO_RULE = "no_rule_on_field"
REASON_PARTIAL_ASSIGNMENT = "rule_not_assigned_on_every_datastream"
REASON_NO_LIVE_DATASTREAM = "no_live_datastream_for_rule"

#: Same shape and same measured reason as
#: `value_table_resolution._RESOLUTION_SAVEPOINT`: a failed statement poisons its
#: transaction, and a caller that swallowed the Python exception would take down
#: whatever it reads next on the same connection.
_APPLICATION_SAVEPOINT = "cleanup_rule_application"


@dataclass(frozen=True, slots=True)
class CleanupRuleApplication:
    """What the project's cleanup rules say about one exact served field.

    ``rules`` are the units compiled into the read; ``gaps`` are the NAMED holes
    -- rules that reach some Datastreams of a connector and not others, or a
    Datastream no longer live -- checkable by the person reading them rather
    than asserted at them. A gap is never compiled and never silently dropped.
    """

    state: str
    project_id: str
    source_field: str
    reason: str = ""
    #: Applied units: {scope, connector, rule_kind, pattern, rules: [{rule_id, name}]}.
    rules: tuple[dict[str, Any], ...] = ()
    #: Named holes: {reason, connector, rule_kind, pattern,
    #:               rules: [{rule_id, name, datastream_id}],
    #:               missing_datastreams: [{datastream_id, datastream_name}]}.
    gaps: tuple[dict[str, Any], ...] = ()

    @property
    def applies(self) -> bool:
        return self.state == STATE_APPLIED and bool(self.rules)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "state": self.state,
            "reason": self.reason,
            "rules": [dict(unit) for unit in self.rules],
            "gaps": [dict(gap) for gap in self.gaps],
        }


def _savepoint(conn, statement: str) -> bool:
    """Best-effort savepoint statement; `False` if it did not take. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    except Exception as exc:  # noqa: BLE001 -- a savepoint is a precaution, not a result
        logger.debug("cleanup_rule_application: %s unavailable: %s", statement, exc)
        return False
    return True


def _live_datastreams(conn, *, project_id: str) -> list[dict[str, Any]]:
    """Every live Datastream of the project, from the relation the mirror mirrors.

    The SET comes from `app.datastreams_dim_v` (live rows only, ``connector``
    being ``module_name`` aliased) so this bridge and the mart's bridge resolve
    the SAME Datastreams; the join to `app.datastreams` only decorates each row
    with its NAME, because "six Datastreams are missing" is not an answer until
    the six are named (story 60.5, `assess_rule_impact`, same sentence).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.datastream_id, v.connector, d.name "
            "FROM app.datastreams_dim_v v "
            "JOIN app.datastreams d ON d.id = v.datastream_id "
            "WHERE v.project_id = %s "
            "ORDER BY v.connector, v.datastream_id",
            (project_id,),
        )
        rows = cur.fetchall() or []
    return [
        {"datastream_id": str(row[0]), "connector": row[1], "datastream_name": row[2]}
        for row in rows
        if row and row[0]
    ]


def resolve_cleanup_rules(
    conn, *, project_id: str, source_field: str
) -> CleanupRuleApplication:
    """Resolve which cleanup rules govern one exact served field of one project.

    The bridge, then the ratified rule:

    1. the project's rules on the EXACT ``source_field`` (string equality, never
       a translation), enabled only, read through the store's own module;
    2. project-wide rules (``datastream_id`` NULL) apply directly;
    3. Datastream-scoped rules resolve through the dim the mirror is made from:
       one body applies at connector scope when EVERY live Datastream of the
       connector carries it; any Datastream missing it -> a NAMED gap, never a
       pick; a rule on a Datastream no longer live -> a named gap too.

    Fail-soft to ``unavailable`` on any read failure, with the transaction
    un-poisoned through a savepoint so the caller's next read on this connection
    survives. ``unavailable`` is never reported as ``none``.
    """

    def _refused(reason: str) -> CleanupRuleApplication:
        return CleanupRuleApplication(
            state=STATE_UNAVAILABLE,
            project_id=project_id,
            source_field=source_field,
            reason=reason,
        )

    marked = _savepoint(conn, f"SAVEPOINT {_APPLICATION_SAVEPOINT}")
    try:
        application = _classify(conn, project_id=project_id, source_field=source_field)
    except Exception as exc:  # noqa: BLE001 -- one unreadable store takes down no read
        logger.warning(
            "cleanup_rule_application: unavailable project=%s field=%s: %s",
            project_id,
            source_field,
            exc,
        )
        if marked:
            _savepoint(conn, f"ROLLBACK TO SAVEPOINT {_APPLICATION_SAVEPOINT}")
        return _refused(f"store unreadable: {type(exc).__name__}")
    if marked:
        _savepoint(conn, f"RELEASE SAVEPOINT {_APPLICATION_SAVEPOINT}")
    return application


def _classify(conn, *, project_id: str, source_field: str) -> CleanupRuleApplication:
    """Apply the ratified ambiguity rule to the rules actually found."""
    from core.cleanup_rules import list_rules  # noqa: PLC0415 -- the store's SQL stays in its owner

    exact_field = validate_field(source_field)
    reaching = [
        rule
        for rule in list_rules(conn, project_id=project_id)
        if rule["enabled"] and rule["source_field"] == exact_field
    ]
    common = {"project_id": project_id, "source_field": exact_field}
    if not reaching:
        return CleanupRuleApplication(state=STATE_NONE, reason=REASON_NO_RULE, **common)

    units: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    # Project-wide rules, in the store's own order (LIST_RULES_SQL sorts them).
    for rule in reaching:
        if rule["datastream_id"] is None:
            units.append(
                {
                    "scope": "project",
                    "connector": None,
                    "rule_kind": rule["rule_kind"],
                    "pattern": rule["pattern"],
                    "rules": [{"rule_id": rule["id"], "name": rule["name"]}],
                }
            )

    # Datastream-scoped rules, through the bridge. The dim is read ONLY when a
    # scoped rule exists: a project whose rules are all project-wide keeps the
    # single-store read it had.
    scoped = [rule for rule in reaching if rule["datastream_id"] is not None]
    if scoped:
        live = _live_datastreams(conn, project_id=project_id)
        connector_of = {row["datastream_id"]: row["connector"] for row in live}
        live_by_connector: dict[str, list[dict[str, Any]]] = {}
        for row in live:
            live_by_connector.setdefault(row["connector"], []).append(row)

        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for rule in scoped:
            datastream_id = str(rule["datastream_id"])
            connector = connector_of.get(datastream_id)
            if connector is None:
                # The rule names a Datastream the dim no longer carries: the
                # connector's served rows cannot be attributed to it, so the
                # rule reaches nothing -- named, never silently dropped.
                gaps.append(
                    {
                        "reason": REASON_NO_LIVE_DATASTREAM,
                        "connector": None,
                        "rule_kind": rule["rule_kind"],
                        "pattern": rule["pattern"],
                        "rules": [
                            {
                                "rule_id": rule["id"],
                                "name": rule["name"],
                                "datastream_id": datastream_id,
                            }
                        ],
                        "missing_datastreams": [],
                    }
                )
                continue
            grouped.setdefault(
                (connector, rule["rule_kind"], rule["pattern"]), []
            ).append({**rule, "datastream_id": datastream_id})

        for (connector, rule_kind, pattern), body_rules in sorted(grouped.items()):
            covered = {rule["datastream_id"] for rule in body_rules}
            missing = [
                row
                for row in live_by_connector.get(connector, [])
                if row["datastream_id"] not in covered
            ]
            named_rules = [
                {
                    "rule_id": rule["id"],
                    "name": rule["name"],
                    "datastream_id": rule["datastream_id"],
                }
                for rule in sorted(body_rules, key=lambda r: r["datastream_id"])
            ]
            if missing:
                # The body reaches SOME Datastreams of the connector. Applying
                # it to connector-keyed rows would clean rows of a Datastream
                # the client never scoped it to -- a pick; not applying it while
                # saying nothing would ignore what the client stated. It comes
                # back NAMED instead: a gap, never a choice.
                gaps.append(
                    {
                        "reason": REASON_PARTIAL_ASSIGNMENT,
                        "connector": connector,
                        "rule_kind": rule_kind,
                        "pattern": pattern,
                        "rules": named_rules,
                        "missing_datastreams": [
                            {
                                "datastream_id": row["datastream_id"],
                                "datastream_name": row["datastream_name"],
                            }
                            for row in missing
                        ],
                    }
                )
            else:
                units.append(
                    {
                        "scope": "connector",
                        "connector": connector,
                        "rule_kind": rule_kind,
                        "pattern": pattern,
                        "rules": [
                            {"rule_id": entry["rule_id"], "name": entry["name"]}
                            for entry in named_rules
                        ],
                    }
                )

    if units:
        state = STATE_APPLIED
    elif gaps:
        state = STATE_AMBIGUOUS
    else:
        state = STATE_NONE
    return CleanupRuleApplication(
        state=state,
        reason="" if units else (gaps[0]["reason"] if gaps else REASON_NO_RULE),
        rules=tuple(units),
        gaps=tuple(gaps),
        **common,
    )


# ---------------------------------------------------------------------------
# Compilation -- WAREHOUSE SQL, so the 60.3 two-dialect obligation binds here.
# Every fragment is emitted by `core.cleanup_rules`; the pattern is always a
# bound parameter; identifiers are validated, never formatted from client input.
# ---------------------------------------------------------------------------


def compile_predicates(
    application: CleanupRuleApplication,
    *,
    dialect: str,
    value_column: str = "breakdown_value",
    first_param: int = 0,
) -> tuple[str | None, list[str]]:
    """The AND-able WHERE fragment of the row rules, in one dialect.

    Row rules test the RAW collected value (the value the client wrote the
    pattern against), so the fragment reads the column, not the strip
    projection. A connector-resolved unit guards its predicate behind
    ``CASE WHEN connector = ? THEN ... ELSE TRUE END`` -- rows of every other
    connector pass untouched, and the connector value is bound, never inlined.

    ``first_param`` is where this fragment's placeholders start: the caller has
    already bound a project and a window, and BigQuery numbers one continuous
    sequence per statement (`test_cleanup_rule_dialects.py`'s own rule).
    """
    fragments: list[str] = []
    params: list[str] = []
    for unit in application.rules:
        if unit["rule_kind"] == "strip_match":
            continue
        if unit["connector"] is None:
            compiled = compile_rule(
                rule_kind=unit["rule_kind"],
                source_field=application.source_field,
                pattern=unit["pattern"],
                dialect=dialect,
                value_column=value_column,
                first_param=first_param + len(params),
            )
            fragments.append(compiled.keep_sql or "")
            params.extend(compiled.match_params)
        else:
            connector_ref = _placeholder(dialect, first_param + len(params))
            params.append(unit["connector"])
            compiled = compile_rule(
                rule_kind=unit["rule_kind"],
                source_field=application.source_field,
                pattern=unit["pattern"],
                dialect=dialect,
                value_column=value_column,
                first_param=first_param + len(params),
            )
            params.extend(compiled.match_params)
            fragments.append(
                f"CASE WHEN connector = {connector_ref} "
                f"THEN {compiled.keep_sql} ELSE TRUE END"
            )
    if not fragments:
        return None, []
    return " AND ".join(fragments), params


def compile_projection(
    application: CleanupRuleApplication,
    *,
    dialect: str,
    value_column: str = "breakdown_value",
    first_param: int = 0,
) -> tuple[str | None, list[str]]:
    """The served value EXPRESSION after every strip rule, in one dialect.

    Strip rules compose in the application's deterministic order: each rewrite
    wraps the previous expression, so the second rule strips what the first one
    produced. A connector-resolved strip only rewrites its connector's rows:
    ``CASE WHEN connector = ? THEN <stripped> ELSE <expression> END``.

    THE TWO DIALECTS COUNT DIFFERENTLY, and this function is where that fact
    lives instead of leaking to callers. The connector-scoped CASE embeds the
    inner expression TWICE (rewritten and untouched). DuckDB binds ``?`` by
    order of appearance, so every duplication of the expression re-appends its
    parameters, in appearance order -- a real DuckDB refuses the statement
    otherwise (the lesson `build_preview_sql` already records). BigQuery binds
    by NAME, so the duplicated text reuses its ``@pN`` names and each parameter
    is bound once.

    Returns ``(None, [])`` when no strip rule applies -- the caller keeps its
    bare column and its parameter numbering.
    """
    strips = [unit for unit in application.rules if unit["rule_kind"] == "strip_match"]
    if not strips:
        return None, []
    expression = _quote(dialect, validate_field(value_column))
    #: DuckDB's list: the expression's parameters in ORDER OF APPEARANCE in its
    #: text. A CASE puts the connector placeholder BEFORE the text it wraps, so
    #: this list is rebuilt per wrap rather than appended to -- an appended list
    #: would bind the connector where an inner pattern belongs.
    appearance: list[str] = []
    #: BigQuery's ledger: one entry per ALLOCATED name, `allocation[i]` bound to
    #: ``@p{first_param + i}``. Duplicated text reuses its names; nothing is
    #: appended twice.
    allocation: list[str] = []
    for unit in strips:
        pattern = unit["pattern"]
        if unit["connector"] is None:
            fragment, bound = compile_strip_expression(
                pattern=pattern,
                dialect=dialect,
                value_sql=expression,
                first_param=first_param + len(allocation),
            )
            allocation.extend(bound)
            expression = fragment
            appearance = [*appearance, pattern]
        else:
            connector_ref = _placeholder(dialect, first_param + len(allocation))
            allocation.append(unit["connector"])
            stripped, bound = compile_strip_expression(
                pattern=pattern,
                dialect=dialect,
                value_sql=expression,
                first_param=first_param + len(allocation),
            )
            allocation.extend(bound)
            untouched = expression
            expression = (
                f"CASE WHEN connector = {connector_ref} "
                f"THEN {stripped} ELSE {untouched} END"
            )
            appearance = [
                unit["connector"],
                *appearance,
                pattern,
                *appearance,
            ]
    return expression, (appearance if dialect == "duckdb" else allocation)


__all__ = [
    "REASON_NO_LIVE_DATASTREAM",
    "REASON_NO_RULE",
    "REASON_PARTIAL_ASSIGNMENT",
    "STATE_AMBIGUOUS",
    "STATE_APPLIED",
    "STATE_NONE",
    "STATE_UNAVAILABLE",
    "CleanupRuleApplication",
    "compile_predicates",
    "compile_projection",
    "resolve_cleanup_rules",
]
