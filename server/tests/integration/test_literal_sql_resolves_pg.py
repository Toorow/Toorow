"""Every literal SQL string of the server must name columns that EXIST.

WHY THIS FILE EXISTS. Three defects of the same shape were found in a single day
(2026-08-23), none of them by a test:

  * `branding._BRANDING_SQL` named `o.org_id` on `app.organizations` and
    `p.project_id` on `app.projects` -- two columns out of three. Every report
    was served with the default theme while a client's colors sat in the
    database;
  * `dimension_lineage._org_of_project` named `project_id` on the same table, so
    every dimension fell back to its stable identifier -- the exact outcome
    `governance.md` forbids;
  * `entity_bindings._datastreams_by_id` selected `capability_fingerprint` from
    `app.datastreams`, where it does not live, so binding confirmation raised for
    every project that had an applicable proposal.

The first two degrade by contract ("any error -> None, never raised"), so they
failed in SILENCE for months. Their unit tests were green and could not have been
otherwise: they patch `core.db.get_connection` with a `MagicMock`, and a mocked
cursor accepts any string. The one thing those tests cannot judge is the query.

WHAT THIS DOES. `PREPARE` asks Postgres to parse and plan a statement WITHOUT
running it: no row is read, no row is written, and a column or relation the
schema does not have is refused right there. Every literal SQL string in
`server/core`, `server/inbound` and `server/modules` is put through it.

AND THE OTHER HALF, ADDED 2026-08-31 (AI-313). Until then the extractor dropped
every statement containing `{`: "an f-string template, assembled elsewhere and
belonging to its builder". Nobody built it anywhere else -- the builder is the
f-string, and 527 statements of `core`, `inbound` and `modules` were leaving
through that door unjudged, among them whole `SELECT ... FROM app.<table>` whose
only interpolation is a WHERE fragment. A template is now RENDERED: every
interpolation becomes a bare marker (`_dyn1`, `_dyn2`, ...), and a module-level
string constant referenced by name is substituted with its own text.

The marker is what makes this safe rather than noisy: when Postgres refuses the
statement over a name that IS one of the markers, the guard declines to judge --
that name was never in the repository, it is the shape of the hole. What is left
is the literal half of the template, and a column it names that the schema does
not have is a defect exactly as it is in a whole statement. Measured on the day
it landed: 527 templates, 80 planned clean, 141 fragments, 289 declined on a
marker, 3 CTE names, 14 refused over a VALUE rather than a name -- and 0 defects.

TWO CLASSES OF FAILURE ARE IGNORED, EACH FOR A STATED REASON -- an exclusion
without one is how a guard rots:

  * `42601` (syntax error). Most SQL here is assembled: a module-level constant
    holds a WHERE fragment, a builder concatenates the clauses. A fragment is not
    a statement and Postgres is right to refuse it. Counting those would drown
    the real signal (334 of them, measured) and force an ignore-list nobody
    maintains;
  * a missing relation whose name carries NO schema (`merged`, `latest`). Those
    are CTE names defined in the half of the statement this fragment does not
    carry. A real missing table in this repository is always schema-qualified
    (`app.`, `mirror.`, `marts.`).

What is left -- an undefined COLUMN, or an undefined SCHEMA-QUALIFIED relation,
in a statement Postgres could otherwise parse -- is always a defect.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

_SERVER = Path(__file__).resolve().parents[2]
#: EVERY tree that talks to this Postgres, not just `core` (AI-313, 2026-08-23).
#: `inbound` and `modules` add 157 statements for two more exemptions -- and both
#: turned out to need none: Google Ads' own query language (`FROM customer_client`,
#: `FROM customer`) names relations WITHOUT a schema, so the CTE rule below
#: already declines to judge them. A guard whose scope stops at one package is
#: how the same defect comes back through the door next to it.
_ROOTS = (_SERVER / "core", _SERVER / "inbound", _SERVER / "modules")
_SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.IGNORECASE)

#: SQLSTATEs this guard judges. Everything else is either a fragment (see the
#: module docstring) or a typing complaint about placeholders we bound as `$n`.
_UNDEFINED_COLUMN = "42703"
_UNDEFINED_TABLE = "42P01"

#: Statements that are NOT this deployment's Postgres, or are deliberately dead.
#: Each entry is (file stem, reason). A path that stops matching is a signal in
#: itself -- the reason has expired and someone should read it.
_EXEMPT: dict[str, str] = {
    # BigQuery SQL: `@named` parameters and `INFORMATION_SCHEMA.SCHEMATA`. Handing
    # it to Postgres tests nothing about either engine.
    "external_bq_registration": "BigQuery SQL, not Postgres",
    # `_publish_datastream_first_publication` is RETIRED: mounted by nothing,
    # called by nothing, kept as a trace and policed by
    # `tests/conformance/test_retired_admin_routes.py`. Its SQL names columns the
    # table lost; repairing dead code would only hide that it is dead.
    "admin_api": "retired handler kept as a trace (AD-43)",
}


def _literal_sql(path: Path):
    """Every string constant that looks like a whole statement, with its line."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a broken file is another test's job
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            # `{` -> a template. It is not dropped any more, it is rendered and
            # judged by `_templated_sql` below. Named `%(name)s` parameters used
            # to be excluded here too, "params we cannot number" -- see
            # `_numbered`, which now numbers them.
            if _SQL_START.match(text) and "{" not in text:
                yield node.lineno, text


#: One interpolation of a template, rendered. A BARE name on purpose: rendered
#: into a `FROM`, Postgres reports it as an unqualified relation, which the CTE
#: rule already declines to judge; rendered anywhere else, the guard recognises
#: its own marker in the message and declines there. Either way it never becomes
#: a verdict about a name the repository does not contain.
_TEMPLATE_MARKER = re.compile(r"_dyn\d+")

#: `{...}` of a `.format()` template. Doubled braces are not interpolations and
#: this does not match them.
_BRACED = re.compile(r"\{[^{}]*\}")


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """`NAME = "..."` at the top level -- the half of a template that IS known.

    A fragment held in a module constant (`_WHERE_ACTIVE`, `_ORDER`) is text this
    repository wrote, so substituting it judges MORE of the statement rather than
    less. Anything else -- a call, an argument, an attribute -- gets a marker.
    """
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found[target.id] = node.value.value
    return found


def _templated_sql(path: Path):
    """Every ASSEMBLED statement, rendered so Postgres can parse it, with its line.

    Two shapes, because the repository uses both: an f-string (`ast.JoinedStr`),
    and a plain constant carrying `{}` for a later `.format()`.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a broken file is another test's job
        return
    constants = _module_string_constants(tree)
    counter = 0

    def _marker() -> str:
        nonlocal counter
        counter += 1
        return f"_dyn{counter}"

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                elif (
                    isinstance(piece, ast.FormattedValue)
                    and isinstance(piece.value, ast.Name)
                    and piece.value.id in constants
                ):
                    parts.append(constants[piece.value.id])
                else:
                    parts.append(_marker())
            text = "".join(parts)
            if _SQL_START.match(text):
                yield node.lineno, text
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if _SQL_START.match(text) and "{" in text:
                yield node.lineno, _BRACED.sub(lambda _match: _marker(), text)


#: `%(name)s`, psycopg's named placeholder.
_NAMED_PARAM = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")


def _numbered(sql: str) -> str:
    """`%s` and `%(name)s` -> `$1, $2, ...`, which is what PREPARE understands.

    NAMED PARAMETERS WERE SKIPPED ENTIRELY UNTIL 2026-08-24, and the hole was
    measured before it was closed: 2 580 statements judged, **98 never looked
    at** because they carried one `%(name)s`. The defect that exposed it is the
    fourth of the class this file was written for --
    `semantic_coverage._UNMAPPED_DATASTREAMS` selected `d.status` from
    `app.datastreams`, a column that has never existed, so Mapping Coverage
    answered `unavailable` for every Project of every deployment since
    2026-07-30. It was invisible here for one reason only: its parameter has a
    name. A guard that judges 96 % of its subject reads as a guard.

    One `$n` PER NAME, in order of first appearance: the same name used twice is
    one parameter, which is exactly what psycopg does with it. `PREPARE` only
    parses and plans, so the value never matters -- the position does.
    """
    order: dict[str, int] = {}

    def _name(match: re.Match[str]) -> str:
        return f"${order.setdefault(match.group(1), len(order) + 1)}"

    sql = _NAMED_PARAM.sub(_name, sql)
    index = len(order)
    out: list[str] = []
    position = 0
    while position < len(sql):
        if sql[position : position + 2] == "%s":
            index += 1
            out.append(f"${index}")
            position += 2
        else:
            out.append(sql[position])
            position += 1
    return "".join(out)


def _unqualified_relation(message: str) -> bool:
    """Is the missing relation a CTE name rather than a real table?

    A table this repository owns is always schema-qualified. Postgres quotes the
    name it could not find with typographic quotes in a French locale and plain
    ones otherwise, so the check reads what sits between the first pair of either.
    """
    match = re.search(r'[«"]\s*([A-Za-z0-9_.]+)\s*[»"]', message)
    return bool(match) and "." not in match.group(1)


#: What one `PREPARE` answered about one statement. One word per outcome, because
#: three of the five are SILENCES and a silence that shares a name with a verdict
#: is how a guard is read as covering what it declined.
_PLANNED = "planned"
_FRAGMENT = "fragment"  # 42601 -- half a statement; see the module docstring
_CTE_NAME = "cte"  # a bare relation, defined in the half this text does not carry
_ON_A_MARKER = "marker"  # the unknown name IS this guard's own interpolation
_NOT_ABOUT_A_NAME = "value"  # a type or value complaint; the names all resolved
_DEFECT = "defect"


def _judge(cur, sql: str) -> tuple[str, str]:
    """`(outcome, message)` for one statement. THE rule, in one place.

    Both sweeps below run through this, so a whole statement and a rendered
    template are judged by the same sentence -- and a rule loosened for one
    cannot stay tight for the other without somebody noticing.
    """
    cur.execute("SAVEPOINT literal_sql_probe")
    try:
        cur.execute("PREPARE _literal_sql_probe AS " + _numbered(sql))
        cur.execute("DEALLOCATE _literal_sql_probe")
        cur.execute("RELEASE SAVEPOINT literal_sql_probe")
        return _PLANNED, ""
    except psycopg.Error as exc:
        cur.execute("ROLLBACK TO SAVEPOINT literal_sql_probe")
        state = getattr(exc, "sqlstate", None) or ""
        message = str(exc).splitlines()[0]
        if state not in (_UNDEFINED_COLUMN, _UNDEFINED_TABLE):
            return (_FRAGMENT if state == "42601" else _NOT_ABOUT_A_NAME), message
        # A marker only excuses a statement that CARRIES one: a whole statement
        # cannot be talked out of a verdict by a rule written for templates.
        if _TEMPLATE_MARKER.search(sql) and _TEMPLATE_MARKER.search(message):
            return _ON_A_MARKER, message
        if state == _UNDEFINED_TABLE and _unqualified_relation(message):
            return _CTE_NAME, message
        return _DEFECT, f"[{state}] {message}"


def _sweep(conn, extractor) -> tuple[dict[str, int], list[str]]:
    """Run *extractor* over the whole subject tree and count what came back."""
    outcomes: dict[str, int] = {}
    failures: list[str] = []
    for path in sorted(p for root in _ROOTS for p in root.rglob("*.py")):
        if path.stem in _EXEMPT:
            continue
        for lineno, sql in extractor(path):
            with conn.cursor() as cur:
                outcome, message = _judge(cur, sql)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome == _DEFECT:
                failures.append(f"{path.name}:{lineno}  {message}")
    return outcomes, failures


def test_every_literal_core_sql_resolves_against_the_live_schema(live_postgres):
    outcomes, failures = _sweep(live_postgres, _literal_sql)
    prepared = outcomes.get(_PLANNED, 0)

    assert prepared > 100, (
        f"only {prepared} statements were prepared -- the extractor stopped matching "
        "SQL, so a green here would mean nothing. Fix the extractor, not this bound."
    )
    assert not failures, (
        "these statements name a column or a schema-qualified relation the database "
        "does not have. Postgres refuses them at PREPARE, which means they cannot run "
        "-- and a caller that degrades on error will hide it:\n  " + "\n  ".join(failures)
    )


def test_the_probe_still_catches_the_defect_it_was_written_for(live_postgres):
    """TEETH. A green above could also mean the probe stopped judging anything.

    The statement below is `branding._BRANDING_SQL` as it was before 2026-08-23,
    to the character. It must be refused with `42703`, and it must NOT be talked
    out of it by the CTE exemption -- the relation it names is schema-qualified.
    """
    was_shipped = (
        "SELECT o.org_id, o.brand_primary FROM app.projects p "
        "JOIN app.organizations o ON o.org_id = p.org_id WHERE p.project_id = $1"
    )
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT teeth")
        with pytest.raises(psycopg.Error) as caught:
            cur.execute("PREPARE _teeth AS " + was_shipped)
        cur.execute("ROLLBACK TO SAVEPOINT teeth")

    assert caught.value.sqlstate == _UNDEFINED_COLUMN
    assert not _unqualified_relation(str(caught.value).splitlines()[0])


def test_a_CTE_name_is_not_mistaken_for_a_missing_table(live_postgres):
    """The other half of the rule, and the reason it is a rule and not a list.

    A fragment that reads from a CTE its own half does not define fails with
    `42P01` on a BARE name. Judging those would make the guard cry wolf on every
    assembled query in the tree.
    """
    fragment = "SELECT * FROM merged"
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT cte")
        with pytest.raises(psycopg.Error) as caught:
            cur.execute("PREPARE _cte AS " + fragment)
        cur.execute("ROLLBACK TO SAVEPOINT cte")

    assert caught.value.sqlstate == _UNDEFINED_TABLE
    assert _unqualified_relation(str(caught.value).splitlines()[0]), (
        "a bare relation name must read as a CTE, or the guard drowns in fragments"
    )


def test_a_named_parameter_statement_is_judged_and_not_skipped(live_postgres):
    """TEETH for the half added on 2026-08-24, on the defect that revealed it.

    The text below is `semantic_coverage._UNMAPPED_DATASTREAMS` as it shipped
    from 2026-07-30 to 2026-08-24, to the character, named parameter included.
    Two things must both hold, and neither used to:

      * `_numbered` turns `%(project_id)s` into `$1` so Postgres can parse it at
        all -- without that, the statement fails with a SYNTAX error (`42601`),
        which this guard deliberately ignores, and a phantom column hides behind
        the ignore;
      * the result is refused with `42703`, on a schema-qualified relation, so
        the CTE exemption cannot talk it out of the verdict.
    """
    was_shipped = (
        "SELECT d.id AS datastream_id, d.name AS datastream_name, d.status "
        "FROM app.datastreams d "
        "WHERE d.project_id = %(project_id)s "
        "AND d.current_mapping_version_id IS NULL"
    )
    numbered = _numbered(was_shipped)
    assert "$1" in numbered and "%(" not in numbered

    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT named_param_teeth")
        with pytest.raises(psycopg.Error) as caught:
            cur.execute("PREPARE _named_param_teeth AS " + numbered)
        cur.execute("ROLLBACK TO SAVEPOINT named_param_teeth")

    assert caught.value.sqlstate == _UNDEFINED_COLUMN
    assert not _unqualified_relation(str(caught.value).splitlines()[0])


def test_the_extractor_actually_yields_named_parameter_statements():
    """The other half: a `_numbered` that works on nothing proves nothing.

    `_literal_sql` filtered `%(` out entirely, so the repair above would have
    been dead code. Asserted on the whole subject tree rather than on one file,
    because a count of one is a fact about that file.
    """
    named = [
        f"{path.name}:{lineno}"
        for root in _ROOTS
        for path in sorted(root.rglob("*.py"))
        if path.stem not in _EXEMPT
        for lineno, sql in _literal_sql(path)
        if _NAMED_PARAM.search(sql)
    ]
    assert len(named) >= 90, (
        f"only {len(named)} named-parameter statements reach the probe; 98 were "
        "measured on 2026-08-24. The extractor has stopped matching them again."
    )


# ---------------------------------------------------------------------------
# THE OTHER HALF -- AI-313, 2026-08-31.
#
# `_literal_sql` dropped every statement containing `{`. The reason it gave --
# "an f-string template, assembled elsewhere and belonging to its builder" -- was
# not true of anything: the builder IS the f-string, and nothing else ever judged
# it. Measured the day this landed: 527 templates in `core`, `inbound` and
# `modules`, none of them looked at by any test, and among them plain
# `SELECT ... FROM app.<table>` statements whose only interpolation is a WHERE
# fragment appended at the end.
# ---------------------------------------------------------------------------


def test_every_templated_sql_resolves_against_the_live_schema(live_postgres, capsys):
    """The literal half of an assembled statement is judged like a whole one.

    THE COVERAGE IS PRINTED, not asserted as a ratchet. Three of the outcomes are
    silences -- a fragment, a CTE name, a marker -- and each is a statement this
    guard declined to speak about. A silence that is not counted is indistinguishable
    from a verdict, and the day the extractor stops rendering templates the count
    of what it judged falls in the output of the run that broke it.
    """
    outcomes, failures = _sweep(live_postgres, _templated_sql)
    total = sum(outcomes.values())

    with capsys.disabled():
        print()
        print(f"  templates rendered and probed : {total}")
        for outcome, count in sorted(outcomes.items()):
            print(f"      {count:4d}  {outcome}")

    assert total >= 400, (
        f"only {total} templates reached the probe; 527 were measured on "
        "2026-08-31. The extractor has stopped rendering them, and a green here "
        "would be a statement about nothing."
    )
    assert outcomes.get(_PLANNED, 0) >= 60, (
        f"only {outcomes.get(_PLANNED, 0)} templates planned clean; 80 did on "
        "2026-08-31. Every template now failing on a marker is one this guard "
        "stopped being able to read -- render it, do not lower this bound."
    )
    assert not failures, (
        "these ASSEMBLED statements name a column or a schema-qualified relation "
        "the database does not have -- in their LITERAL half, the part no "
        "interpolation can change. The `{...}` around them is not what is wrong:\n  "
        + "\n  ".join(failures)
    )


def test_a_template_is_judged_on_its_literal_half_and_never_on_its_hole(live_postgres):
    """TEETH for the half added on 2026-08-31, both directions.

    The rendering is only worth having if it still convicts. Two statements of
    the same shape -- one interpolation, one relation, one column -- and the only
    difference is whether the column exists. The first must be a defect; the
    second must be a SILENCE named `marker`, not a verdict, because the only name
    Postgres cannot find is the one this guard invented.
    """
    with live_postgres.cursor() as cur:
        # The literal half is wrong: `app.projects` has no `project_label`.
        convicted, message = _judge(
            cur, "SELECT p.project_label FROM app.projects p WHERE _dyn1"
        )
        # The literal half is right; the hole is where the relation was.
        declined, _ = _judge(cur, "SELECT count(*) FROM _dyn1 WHERE org_id = $1")
        # A whole statement carrying no marker cannot borrow the marker excuse.
        unrelated, _ = _judge(cur, "SELECT o.no_such_column FROM app.organizations o")

    assert convicted == _DEFECT, (
        f"a wrong column in the literal half of a template was not judged: {message}"
    )
    assert "project_label" in message
    assert declined in (_ON_A_MARKER, _CTE_NAME), (
        "a name this guard invented was turned into a verdict about the repository"
    )
    assert unrelated == _DEFECT


def test_the_extractor_actually_renders_the_two_shapes_of_template():
    """An f-string and a `.format()` constant, both, on the subject tree itself.

    Counted separately from the probe because a renderer that works on nothing
    would let the sweep above pass on an empty subject and print a `0` nobody
    reads as a failure.
    """
    rendered = [
        sql
        for root in _ROOTS
        for path in sorted(root.rglob("*.py"))
        if path.stem not in _EXEMPT
        for _lineno, sql in _templated_sql(path)
    ]
    with_marker = [sql for sql in rendered if _TEMPLATE_MARKER.search(sql)]
    fully_known = [sql for sql in rendered if not _TEMPLATE_MARKER.search(sql)]

    assert len(rendered) >= 400, len(rendered)
    assert with_marker, "no interpolation was ever replaced -- the renderer is inert"
    assert fully_known, (
        "not one template resolved WITHOUT a marker; `_module_string_constants` "
        "has stopped substituting the fragments this repository holds in constants"
    )
    # A NAMED hole, never a brace: `'{}'::jsonb` written `'{{}}'` in an f-string
    # is already the runtime text, and Postgres reads it as the empty JSON it is.
    # What must not survive is an interpolation still spelled `{something}`, which
    # would be refused as a syntax error and drop the statement in silence again.
    unrendered = [sql for sql in rendered if re.search(r"\{[A-Za-z_]", sql)]
    assert not unrendered, (
        "a rendered template still carries an interpolation:\n  "
        + "\n  ".join(sql.strip()[:120] for sql in unrendered)
    )
