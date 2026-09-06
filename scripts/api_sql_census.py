r"""Which `*_api.py` modules execute SQL, and a ratchet that only lets the count fall.

WHY THIS EXISTS. Criterion 4 of `docs/product-architecture/module-boundaries.md`
says what a route module may hold:

    A `*_api.py` module contains business logic rather than request parsing,
    authorization and a call into a service module.

AD-43 named the repair, ordered it, and delivered its FIRST step -- the 877
lines of service logic that were mounted by nothing left `admin_api.py` on
2026-08-12. Step three, *"a handler parses, authorizes and calls a service"*,
was never done, and nothing in the repository was counting. Measured 2026-08-30
under AI-330: **72 of the 103 `*_api.py` modules execute SQL**, and no guard
said so. That is the gap this instrument closes. It does NOT remove the 72 --
the wave is a separate, arbitrated piece of work; what was missing is the
number, and a number nobody re-runs is a paragraph.

WHAT COUNTS AS EXECUTING SQL. A call to `.execute(...)`, `.executemany(...)` or
`.executescript(...)` on any receiver, found by AST rather than by grep so a
statement split across lines counts once and a mention inside a docstring counts
not at all. Measured over the whole scope on 2026-08-31, **every** receiver in
every one of these modules is the name `cur`, and no other SQL-bearing shape is
used: no `client.query(`, no `read_sql`, no bespoke `execute_sql(` helper. That
is stated because it is the instrument's blind spot -- a module that reaches the
database through a NEW shape would be invisible here, so `--report` prints the
receivers it saw and `--gate` refuses a receiver vocabulary it does not know.

THE SCOPE, DECLARED. `server/core/*_api.py`, and every `*_api.py` under
`server/` outside the test tree lives there -- 103 of 103, re-checked on every
run. A guard whose scope stops before a directory does not report that directory
clean; it goes quiet, and the silence reads as health (criterion 13). So a
`*_api.py` appearing anywhere else under `server/` reddens the gate rather than
being skipped.

THE ONE EXEMPTION, AND WHY IT IS BY NAME. `admin_api.py` executes SQL too, and
AD-40 put it there on purpose: *"what stays in `admin_api.py`: the `Router`
assembly, the middleware, the auth/scope seam, and nothing else"*, and AD-43
re-stated the same verdict on the seam -- *"stays. Authorization IS a route
module's job."* Excluding it is what makes this census reproduce AI-330's 72
rather than 73. An exemption with no bound is an amnesty, so it carries a
ceiling on the statements it may hold: the seam may not grow a business query
under cover of being the seam.

    python scripts/api_sql_census.py            # per-module verdicts + totals
    python scripts/api_sql_census.py --executing # only the modules that execute
    python scripts/api_sql_census.py --json
    python scripts/api_sql_census.py --gate     # ratchet, both directions
    python scripts/api_sql_census.py --baseline # today's set, ready to paste

THE RATCHET TURNS ONE WAY, and it is red in both directions on purpose:

  * a module that executes SQL and is NOT in the frozen set is refused the day
    it is written -- the only day the repair is free;
  * a frozen module that no longer executes SQL is refused too, so the set can
    never become a record of debt nobody has any more. A wave that removes SQL
    from a module deletes its line in the SAME commit, and the count falls with
    the measurement instead of being remembered.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "server"
CORE = SERVER / "core"

#: The calls that hand a statement to a database cursor.
EXEC_ATTRS = frozenset({"execute", "executemany", "executescript"})

#: The receivers seen on 2026-08-31 over the whole scope. This is not a filter --
#: every `.execute(` is counted whatever it is called on. It is a tripwire: a
#: NEW receiver name means a second way of reaching the database has appeared,
#: and the reader should be told rather than left with a number that quietly
#: stopped covering it.
KNOWN_RECEIVERS = frozenset({"cur"})

#: `admin_api.py` is the assembler and the declared home of the auth/scope seam
#: (AD-40, AD-43). It is the ONLY module the criterion exempts, and the
#: exemption is bounded by the number of statements the seam held when it was
#: granted. A seam that grows a query is a route module implementing logic
#: again, which is the whole criterion.
EXEMPT = "admin_api.py"
EXEMPT_STATEMENT_CEILING = 7

#: How many `*_api.py` modules the scope holds, measured 2026-08-31. A census
#: that reads an empty tree finds zero SQL and reports the debt cleared -- the
#: instrument must not measure its own copy.
API_MODULES_AT_2026_08_31 = 103

#: The 72 `*_api.py` modules that executed SQL on 2026-08-30 (AI-330), unchanged
#: on 2026-08-31 when this instrument was written. SHRINK-ONLY.
#:
#: These are not 72 bugs. They are 72 modules where a handler still parses,
#: authorizes AND queries, so a change to the query and a change to the door are
#: the same edit. AD-43 names the cure -- a handler calls a service -- and its
#: first step (the 877 lines that were mounted by nothing) is the proof the move
#: is mechanical. The list is ordered by nothing; a wave should take the heaviest
#: first, which `--report` prints.
SQL_EXECUTING_API_MODULES_AT_2026_08_31: frozenset[str] = frozenset(
    {
        "ai_paths_api.py",
        "alert_definitions_api.py",
        "analyze_artifacts_api.py",
        "analyze_workbench_api.py",
        "answerable_topics_api.py",
        "business_taxonomy_api.py",
        "cards_api.py",
        "catalog_api.py",
        "cleanup_rules_api.py",
        "connections_api.py",
        "connector_installation_api.py",
        "context_api.py",
        "context_events_api.py",
        "controls_quality_api.py",
        "credential_accounts_api.py",
        "datamodel_api.py",
        "dataset_access_api.py",
        "datastream_daily_breakdown_api.py",
        "datastream_executions_api.py",
        "datastream_mapping_api.py",
        "datastream_preconfiguration_api.py",
        "datastream_progress_api.py",
        "datastream_recovery_api.py",
        "datastream_sample_api.py",
        "datastream_workbench_api.py",
        "datastreams_api.py",
        "dimension_lineage_api.py",
        "dq_api.py",
        "entity_detail_gaps_api.py",
        "entry_api.py",
        "evaluation_runs_api.py",
        "event_stream_arming_api.py",
        "feedback_regression_api.py",
        "feedback_review_api.py",
        "file_import_api.py",
        "file_source_template_api.py",
        "first_value_api.py",
        "flux_projects_api.py",
        "getting_started_api.py",
        "golden_questions_api.py",
        "google_oauth_api.py",
        "import_templates_api.py",
        "inbound_health_api.py",
        "invitations_api.py",
        "jobs_api.py",
        "language_bindings_api.py",
        "legacy_evidence_api.py",
        "managed_feed_imports_api.py",
        "mcp_hosts_api.py",
        "mdm_canonical_fields_api.py",
        "me_api.py",
        "mediaplan_api.py",
        "metric_grain_api.py",
        "metric_semantics_api.py",
        "multi_source_api.py",
        "notebooks_api.py",
        "org_members_api.py",
        "organizations_api.py",
        "pivot_api.py",
        "platform_maintenance_api.py",
        "project_connections_api.py",
        "project_settings_api.py",
        "projects_api.py",
        "publication_reviews_api.py",
        "query_specs_api.py",
        "render_shares_console_api.py",
        "rule_versions_api.py",
        "source_delegations_api.py",
        "trace_observation_api.py",
        "unresolved_values_api.py",
        "value_mapping_api.py",
        "visualization_specs_api.py",
    }
)

_VERBS = (
    "select",
    "insert",
    "update",
    "delete",
    "with",
    "create",
    "alter",
    "drop",
    "set",
    "savepoint",
    "release",
    "rollback",
    "truncate",
)


@dataclass
class ApiModule:
    """One `*_api.py` module and the SQL its own body hands to a cursor."""

    name: str
    path: str
    statements: int
    lines: int
    verbs: dict[str, int]
    receivers: list[str]
    exempt: bool

    @property
    def executes(self) -> bool:
        return self.statements > 0


def _receiver(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _receiver(node.func)
    return f"<{type(node).__name__}>"


def _verb(node: ast.expr | None) -> str:
    """The leading SQL keyword of an argument, when it is written as a literal.

    An argument built at runtime -- a name, a call, a join -- is counted as a
    statement all the same and filed under `?`. Refusing to count what it cannot
    read would let a module hide behind a variable.
    """

    text: str | None = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                text = part.value
                break
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _verb(node.left)
    if text is None:
        return "?"
    head = text.strip().lstrip("(").strip().split(None, 1)
    if not head:
        return "?"
    word = head[0].lower().strip(";")
    return word if word in _VERBS else "?"


def _scan(path: Path) -> ApiModule:
    source = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)
    statements = 0
    verbs: dict[str, int] = {}
    receivers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in EXEC_ATTRS:
            continue
        statements += 1
        receivers.add(_receiver(func.value))
        verb = _verb(node.args[0] if node.args else None)
        verbs[verb] = verbs.get(verb, 0) + 1
    return ApiModule(
        name=path.name,
        path=str(path.relative_to(REPO)).replace("\\", "/"),
        statements=statements,
        lines=len(source.splitlines()),
        verbs=dict(sorted(verbs.items())),
        receivers=sorted(receivers),
        exempt=path.name == EXEMPT,
    )


def out_of_scope_modules() -> list[str]:
    """`*_api.py` files under `server/` that are NOT in the declared scope.

    Route modules all live in `server/core/`; test doubles under `server/tests/`
    are not route modules. Anything else is scope drift, and a census that
    silently skipped it would report the tree clean while a route module sat
    outside its walk.
    """

    found: list[str] = []
    for path in sorted(SERVER.rglob("*_api.py")):
        parts = path.parts
        if "__pycache__" in parts or "tests" in parts:
            continue
        if path.parent != CORE:
            found.append(str(path.relative_to(REPO)).replace("\\", "/"))
    return found


@lru_cache(maxsize=2)
def census() -> tuple[ApiModule, ...]:
    """Every `*_api.py` of the declared scope, with the SQL it executes."""

    return tuple(_scan(path) for path in sorted(CORE.glob("*_api.py")))


def executing_names(modules: tuple[ApiModule, ...]) -> set[str]:
    """The modules the criterion counts: they execute SQL and are not exempt."""

    return {m.name for m in modules if m.executes and not m.exempt}


# --------------------------------------------------------------------------- #
# The ratchet
# --------------------------------------------------------------------------- #


def gate_failures(modules: tuple[ApiModule, ...]) -> list[str]:
    """Every reason this census refuses today's tree, one sentence each."""

    failures: list[str] = []

    if len(modules) < API_MODULES_AT_2026_08_31:
        failures.append(
            f"the census scanned {len(modules)} `*_api.py` modules, "
            f"{API_MODULES_AT_2026_08_31} were there on 2026-08-31. "
            "server/core moved or the walk broke, so every verdict below is "
            "about nothing."
        )

    drift = out_of_scope_modules()
    if drift:
        failures.append(
            "a `*_api.py` module lives outside the declared scope "
            "(server/core), so this census does not cover it: "
            + ", ".join(drift)
        )

    live = executing_names(modules)
    baseline = SQL_EXECUTING_API_MODULES_AT_2026_08_31

    new = sorted(live - baseline)
    if new:
        failures.append(
            f"{len(new)} `*_api.py` module(s) execute SQL that did not on "
            "2026-08-31. Criterion 4 says a route module parses, authorizes and "
            "calls a SERVICE: move the statement into a service module (AD-43's "
            "first step is the worked example), or, if it is authorization, say "
            "so where the seam is declared.\n  " + "\n  ".join(new)
        )

    gone = sorted(baseline - live)
    if gone:
        failures.append(
            f"{len(gone)} frozen module(s) no longer execute SQL. Delete them "
            "from SQL_EXECUTING_API_MODULES_AT_2026_08_31 in "
            "scripts/api_sql_census.py in the commit that repaired them -- the "
            "recorded count falls WITH the measurement, and a baseline that "
            "outlives its subjects is a record of debt nobody has:\n  "
            + "\n  ".join(gone)
        )

    for module in modules:
        if module.exempt and module.statements > EXEMPT_STATEMENT_CEILING:
            failures.append(
                f"{module.name} holds {module.statements} statements, "
                f"{EXEMPT_STATEMENT_CEILING} when it was exempted. The "
                "exemption covers the auth/scope seam AD-40 left there, not a "
                "new query written under its cover."
            )

    unknown = sorted(
        {r for m in modules for r in m.receivers if r not in KNOWN_RECEIVERS}
    )
    if unknown:
        failures.append(
            "an unknown cursor receiver appeared -- "
            + ", ".join(unknown)
            + ". A second way of reaching the database may exist that this "
            "census cannot see; widen it, or add the name to KNOWN_RECEIVERS."
        )

    return failures


def _gate(modules: tuple[ApiModule, ...]) -> int:
    live = executing_names(modules)
    exempt = [m for m in modules if m.exempt]
    print(f"`*_api.py` modules in server/core : {len(modules)}")
    print(
        f"  executing SQL                   : {len(live)}   "
        f"(frozen 2026-08-31: {len(SQL_EXECUTING_API_MODULES_AT_2026_08_31)})"
    )
    print(
        f"  exempt (the auth/scope seam)    : "
        f"{', '.join(f'{m.name} [{m.statements}/{EXEMPT_STATEMENT_CEILING}]' for m in exempt) or '-'}"
    )
    print(f"  clean                           : {len(modules) - len(live) - len(exempt)}")

    failures = gate_failures(modules)
    if failures:
        print(f"\nREFUSED on {len(failures)} count(s):")
        for failure in failures:
            print(f"\n- {failure}")
        return 1
    print("\nOK: no new SQL-executing route module, no stale line in the baseline.")
    return 0


def _report(modules: tuple[ApiModule, ...], executing_only: bool) -> None:
    shown = [m for m in modules if m.executes] if executing_only else list(modules)
    shown.sort(key=lambda m: (-m.statements, m.name))
    print("stmts  lines  verbs                          module")
    for module in shown:
        verbs = " ".join(f"{k}:{v}" for k, v in module.verbs.items())
        mark = "  (exempt)" if module.exempt else ""
        print(f"{module.statements:>5}  {module.lines:>5}  {verbs[:28]:<28}  {module.name}{mark}")

    live = executing_names(modules)
    total = sum(m.statements for m in modules if not m.exempt)
    print("\n" + "-" * 68)
    print(f"`*_api.py` modules in server/core : {len(modules)}")
    print(f"  executing SQL                   : {len(live)}")
    print(f"  statements they hold            : {total}")
    print(f"  exempt                          : {EXEMPT} (the auth/scope seam)")
    print("Criterion 4 of docs/product-architecture/module-boundaries.md: a route")
    print("module parses, authorizes and calls a SERVICE. The ratchet is")
    print("`python scripts/api_sql_census.py --gate`.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable")
    parser.add_argument(
        "--executing", action="store_true", help="only the modules that execute SQL"
    )
    parser.add_argument("--gate", action="store_true", help="refuse a NEW one, and a stale line")
    parser.add_argument("--baseline", action="store_true", help="print today's set")
    args = parser.parse_args()

    modules = census()
    if args.gate:
        return _gate(modules)
    if args.baseline:
        for name in sorted(executing_names(modules)):
            print(f'        "{name}",')
        return 0
    if args.json:
        print(json.dumps([asdict(m) for m in modules], indent=2))
        return 0
    _report(modules, args.executing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
