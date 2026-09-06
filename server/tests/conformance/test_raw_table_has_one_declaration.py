"""A raw table is declared ONCE, and DDL, landing and dbt staging all read it.

WHY THIS TEST EXISTS
    A connector used to describe its raw table twice: a DuckDB ``CREATE TABLE``
    string for one backend, and a second hand-written ``columns=`` list for the
    BigQuery path of ``core.raw_landing.land_raw_rows``. Nothing compared them.

    Measured 2026-08-17, seven of those pairs had drifted, and the drifted copy
    was the one production runs -- ``TOOROW_DB_MODE=bigquery`` is set on every
    deploy. ``google-ads`` landed ``metric_name`` / ``metric_value`` /
    ``currency`` while its DDL *and* ``stg_google_ads_daily.sql`` both read
    ``metric`` / ``value_num`` / ``cost_source_currency``: every column dbt
    selected was absent from the table the connector had just written. The pull
    was green, the staging model was green in the DuckDB fixture, and the
    production warehouse held rows no model could see.

    Two copies of one fact do not stay equal, so the repair is not seven renames
    -- it is this test. It reads the FOUR places a raw table's columns appear
    and refuses any disagreement between them:

        DDL / declaration  <->  land_raw_rows(columns=...)  <->  dbt staging
                           <->  seeds/load_<name>_seed.py

    THE SEED IS THE FOURTH COPY, AND IT WAS OUTSIDE THIS TEST UNTIL 2026-08-17.
    Every module ships a seed loader carrying its own ``CREATE TABLE`` for the
    same raw table, because ``dbt build`` over a local DuckDB file has to have
    something to read. Nothing compared that copy to anything. Measured the day
    this reader was first pointed at them, one had already drifted: ``gsc``'s seed
    declared ten columns and appended ``query`` / ``search_type`` /
    ``search_appearance`` / ``hour`` through ALTER guards, so a seeded file held
    them LAST while a real pull creates them right after ``device``. Two tables,
    one name -- and the fixture that exists to prove the staging model would have
    proved it against the wrong shape.

    A seed may INSERT into a subset of the columns (a fixture need not populate
    everything), but the table it CREATES is the landing table or it is a
    different table wearing its name.

    Everything here is derived from the files on disk. There is no allowlist: a
    module cannot be added to an exemption list, only made to agree.

WHAT "AGREE" MEANS, PRECISELY
    * every table a module LANDS into must be declared by that module;
    * the landed column list must equal the declaration, in order (order is the
      contract: the connectors build positional value tuples);
    * every column a staging model READS must exist in the declaration -- the
      converse is not required, a landed column no model reads yet is fine;
    * the staging reader must actually find columns wherever a staging model
      exists, so this test can never pass by reading nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_MODULES = Path(__file__).resolve().parents[2] / "modules"

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_SOURCE_REF = re.compile(
    r"\{\{\s*source\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\)\s*\}\}"
)
_CTE_HEAD = re.compile(r"(?:\bWITH\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", re.IGNORECASE)

#: Words that appear where a column name would and are not columns. Kept narrow
#: on purpose: a keyword wrongly listed here would HIDE a real column from the
#: comparison, which is the failure this whole test exists to prevent.
_SQL_WORDS = {
    "select", "from", "where", "and", "or", "not", "null", "is", "as", "on",
    "join", "left", "right", "inner", "outer", "full", "cross", "using",
    "group", "order", "by", "partition", "over", "qualify", "having", "limit",
    "distinct", "case", "when", "then", "else", "end", "asc", "desc", "with",
    "union", "all", "in", "between", "like", "cast", "true", "false", "row_number",
    "coalesce", "sum", "count", "max", "min", "avg", "nullif", "config",
    "materialized", "view", "table", "ref", "source", "date", "interval",
}


def _module_dirs() -> list[Path]:
    return sorted(p for p in _MODULES.iterdir() if p.is_dir() and (p / "connector.py").exists())


def _module_ids() -> list[str]:
    return [p.name for p in _module_dirs()]


# ---------------------------------------------------------------------------
# Reading the connector: declarations and landings
# ---------------------------------------------------------------------------


def _ddl_column_names(body: str) -> list[str]:
    """Column names of a CREATE TABLE body, ignoring table-level constraints.

    Line comments are stripped BEFORE the split, not after. Stripping them after
    was a reader bug with real consequences: a `-- ... , ...` comment inside the
    DDL split the body at a comma that was not a column separator, and the words
    of the sentence were then read as column names. Three shipped DDLs carry such
    a comment, so this reader silently compared prose against columns and could
    have reported a phantom disagreement -- or, worse, masked a real one by
    turning one side into noise on both.
    """
    body = re.sub(r"--[^\n]*", "", body)
    parts, depth, current = [], 0, ""
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)

    names = []
    for part in parts:
        cleaned = re.sub(r"--.*", "", part).strip()
        if not cleaned:
            continue
        head = cleaned.split()[0]
        if head.upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"}:
            continue
        names.append(head.strip('"'))
    return names


def _literal_columns(
    node: ast.AST, strings: dict[str, list[str]] | None = None
) -> list[str] | None:
    """``[("date", "STRING"), ...]`` -> the ordered names, or None if not that shape.

    A declaration may splice in a list it already keeps elsewhere --
    ``*[(column, "INTEGER") for column in _METRIC_COLUMNS]`` -- precisely so the
    metric names are written once. That is the shape this test wants to
    encourage, so it is evaluated rather than treated as unreadable.
    """
    if not isinstance(node, ast.List):
        return None
    names = []
    for element in node.elts:
        if (
            isinstance(element, ast.Tuple)
            and element.elts
            and isinstance(element.elts[0], ast.Constant)
            and isinstance(element.elts[0].value, str)
        ):
            names.append(element.elts[0].value)
            continue
        spliced = _spliced_names(element, strings or {})
        if spliced is None:
            return None
        names.extend(spliced)
    return names or None


def _spliced_names(element: ast.AST, strings: dict[str, list[str]]) -> list[str] | None:
    """``*[(name, "TYPE") for name in <a module-level list of strings>]``."""
    if not (isinstance(element, ast.Starred) and isinstance(element.value, ast.ListComp)):
        return None
    comprehension = element.value
    if len(comprehension.generators) != 1:
        return None
    generator = comprehension.generators[0]
    if not (isinstance(generator.iter, ast.Name) and isinstance(generator.target, ast.Name)):
        return None
    if not (
        isinstance(comprehension.elt, ast.Tuple)
        and comprehension.elt.elts
        and isinstance(comprehension.elt.elts[0], ast.Name)
        and comprehension.elt.elts[0].id == generator.target.id
    ):
        return None
    return strings.get(generator.iter.id)


def _assignment(node: ast.AST) -> tuple[ast.AST | None, ast.AST | None]:
    """The (target, value) of a plain or ANNOTATED assignment.

    Annotated is not a detail: the declarations written most carefully are the
    ones that carry `: list[tuple[str, str]]`, and reading only bare assignments
    would have skipped exactly those.
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.targets[0], node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return node.target, node.value
    return None, None


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _read_connector(module: Path) -> dict:
    """Declarations, DDL literals and landings of one connector, read statically.

    Static rather than imported: a connector pulls in provider SDKs and this test
    must be able to read all thirty-nine of them cheaply, and must not depend on
    a module being importable to notice that its table drifted.
    """
    source = (module / "connector.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    #: every list-of-pairs bound to a name, in source order. A list, not a dict:
    #: several connectors bind `columns` inside more than one function, and
    #: keeping only one of them would compare a landing against a list from a
    #: DIFFERENT table -- a false verdict in either direction.
    #: module-level lists of plain strings, so a declaration may splice one in
    string_lists: dict[str, list[str]] = {}
    for node in tree.body:
        target, value = _assignment(node)
        if isinstance(target, ast.Name) and isinstance(value, ast.List):
            items = [
                element.value
                for element in value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            if items and len(items) == len(value.elts):
                string_lists[target.id] = items

    bindings: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(tree):
        target, value = _assignment(node)
        if isinstance(target, ast.Name) and value is not None:
            names = _literal_columns(value, string_lists)
            if names:
                bindings.append((node.lineno, target.id, names))
    bindings.sort()

    def resolve(name: str, before: int) -> list[str] | None:
        """The binding of *name* in force at line *before* -- the nearest one above."""
        candidates = [cols for line, bound, cols in bindings if bound == name and line < before]
        return candidates[-1] if candidates else None

    declarations = {name: cols for _, name, cols in bindings}

    #: module-level string constants, so a table named `_RAW_TABLE` resolves
    strings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                strings[target.id] = node.value.value

    def table_of(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return strings.get(node.id)
        return None

    #: table -> (columns, origin) from a CREATE TABLE literal or a derived DDL
    declared_tables: dict[str, tuple[list[str], str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = _CREATE_TABLE.match(node.value.strip())
            if match:
                declared_tables[match.group(1)] = (
                    _ddl_column_names(match.group(2)),
                    "CREATE TABLE literal",
                )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "duckdb_ddl" and node.args:
            table = table_of(node.args[0])
            if table is None:
                continue
            columns_node = node.args[1] if len(node.args) > 1 else None
            names = None
            if isinstance(columns_node, ast.Name):
                names = declarations.get(columns_node.id)
            elif columns_node is not None:
                names = _literal_columns(columns_node, string_lists)
            if names:
                declared_tables[table] = (names, "duckdb_ddl(...) declaration")

    #: child -> parent, so a call can find the function it sits in
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_function(node: ast.AST) -> ast.FunctionDef | None:
        current = node
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.FunctionDef):
                return current
        return None

    def argument_at_call_sites(function: ast.FunctionDef, parameter: str) -> list[ast.AST]:
        """Every expression passed as *parameter* wherever *function* is called.

        A connector may wrap the landing seam in a helper of its own -- google-
        business-profile does, to serve three raw tables from one function -- and
        the table then arrives as an argument. Following the call sites is what
        keeps such a module inside this check instead of silently outside it.
        """
        names = [argument.arg for argument in function.args.args]
        if parameter not in names:
            return []
        index = names.index(parameter)
        passed = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == function.name):
                continue
            if len(node.args) > index:
                passed.append(node.args[index])
            for keyword in node.keywords:
                if keyword.arg == parameter:
                    passed.append(keyword.value)
        return passed

    #: every land_raw_rows call
    landings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "land_raw_rows":
            continue
        table_node = node.args[0] if node.args else None
        columns_node = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "columns"), None
        )

        function = enclosing_function(node)
        table_nodes = [table_node]
        columns_nodes = [columns_node]
        if function is not None:
            parameter_names = {argument.arg for argument in function.args.args}
            if isinstance(table_node, ast.Name) and table_node.id in parameter_names:
                table_nodes = argument_at_call_sites(function, table_node.id)
            if isinstance(columns_node, ast.Name) and columns_node.id in parameter_names:
                columns_nodes = argument_at_call_sites(function, columns_node.id)

        for table_expression, columns_expression in zip(table_nodes, columns_nodes):
            columns, inline = None, False
            if isinstance(columns_expression, ast.Name):
                columns = resolve(columns_expression.id, node.lineno) or declarations.get(
                    columns_expression.id
                )
            elif columns_expression is not None:
                columns = _literal_columns(columns_expression, string_lists)
                inline = columns is not None
            landings.append(
                {
                    "line": node.lineno,
                    "table": table_of(table_expression),
                    "columns": columns,
                    "inline": inline,
                }
            )

    return {
        "declarations": declarations,
        "tables": declared_tables,
        "landings": landings,
    }


# ---------------------------------------------------------------------------
# Reading the dbt staging models
# ---------------------------------------------------------------------------


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _identifiers(fragment: str) -> set[str]:
    """Bare identifiers of a fragment, minus SQL words, jinja and qualified heads."""
    fragment = re.sub(r"--.*", "", fragment)
    fragment = re.sub(r"\{\{.*?\}\}", " ", fragment, flags=re.DOTALL)
    fragment = re.sub(r"'[^']*'", " ", fragment)
    # `raw.country AS country_source` names a column and then names its OUTPUT.
    # The output name belongs to the model, not to the raw table, and reading it
    # as one would report a phantom missing column on every renaming model. The
    # same strip removes `CAST(x AS timestamp)`'s type.
    fragment = re.sub(r"\bAS\s+[A-Za-z_][A-Za-z0-9_]*", " ", fragment, flags=re.IGNORECASE)
    found = set()
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\b(\s*[.(])?", fragment):
        if match.group(2):  # a qualifier (`raw.`) or a function call (`sum(`)
            continue
        word = match.group(1)
        if word.lower() in _SQL_WORDS:
            continue
        found.add(word)
    return found


def _staging_columns(module: Path) -> dict[str, dict[str, set[str]]]:
    """table -> {model file: columns that model reads from the raw relation}.

    The shipped staging models share one shape: a CTE that reads the raw source,
    then an outer SELECT that names ``<cte>.<column>``. Both are harvested, and
    the harvest is SOUND rather than complete -- every name returned is genuinely
    a column of the raw relation, so an inclusion check on it cannot be a false
    alarm. `test_the_staging_reader_finds_columns_wherever_a_model_exists` keeps
    the incompleteness from ever becoming silence.
    """
    dbt_dir = module / "dbt"
    result: dict[str, dict[str, set[str]]] = {}
    if not dbt_dir.exists():
        return result

    for sql_path in sorted(dbt_dir.rglob("*.sql")):
        text = sql_path.read_text(encoding="utf-8")
        stripped = re.sub(r"--.*", "", text)

        ctes = []
        for head in _CTE_HEAD.finditer(stripped):
            open_index = stripped.index("(", head.end() - 1)
            ctes.append((head.group(1), open_index, _matching_paren(stripped, open_index)))

        for ref in _SOURCE_REF.finditer(stripped):
            table = ref.group(1)
            enclosing = [c for c in ctes if c[1] < ref.start() < c[2]]
            if enclosing:
                alias, open_index, close_index = min(enclosing, key=lambda c: c[2] - c[1])
                body = stripped[open_index + 1 : close_index]
            else:
                alias, body = None, stripped

            columns: set[str] = set()
            if alias:
                columns |= set(re.findall(rf"\b{alias}\.([A-Za-z_][A-Za-z0-9_]*)\b", stripped))
                # A CTE may ADD a computed column (`COALESCE(data_level,
                # 'CAMPAIGN') AS data_level_resolved`), and the outer SELECT then
                # says `raw.data_level_resolved` -- qualified by the CTE, but not
                # a column of the raw table. Names the CTE itself introduces are
                # therefore removed before the comparison.
                columns -= set(
                    re.findall(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)", body, re.IGNORECASE)
                )

            # Bare identifiers of the reading block are raw columns only when
            # that block reads ONE relation; a join would mix in the other side's
            # names and this check must never invent a column.
            relations = len(re.findall(r"\b(?:FROM|JOIN)\b", body, re.IGNORECASE))
            if relations == 1:
                columns |= _identifiers(body)

            if not columns and re.search(r"SELECT\s+\*", body, re.IGNORECASE):
                # A pass-through model (`SELECT * FROM source`) names no column,
                # so it cannot disagree with one. Marked rather than dropped, so
                # the coverage check below can tell "reads everything" apart from
                # "this reader failed to understand the model".
                columns = {"*"}
            if columns:
                result.setdefault(table, {})[sql_path.name] = columns
    return result


# ---------------------------------------------------------------------------
# Reading the seed loaders (the fourth copy)
# ---------------------------------------------------------------------------


def _seed_files(module: Path) -> list[Path]:
    seeds = module / "seeds"
    if not seeds.exists():
        return []
    return sorted(seeds.glob("*.py"))


def _seed_tables(path: Path) -> dict[str, list[str]]:
    """table -> ordered column names, for every CREATE TABLE literal in a seed.

    Static like the connector reader, and for a stronger reason: a seed loader
    imports duckdb and its sibling generator, and this test must be able to read
    fifty-two of them without any of that being installed or importable.
    """
    tables: dict[str, list[str]] = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return tables
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = _CREATE_TABLE.match(node.value.strip())
            if match:
                tables[match.group(1)] = _ddl_column_names(match.group(2))
    return tables


def _seed_inserted_columns(path: Path) -> dict[str, set[str]]:
    """table -> the column names any ``INSERT INTO <table> (a, b, c)`` names."""
    inserted: dict[str, set[str]] = {}
    text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
    for match in re.finditer(
        r"INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_.]*)\s*\(([^)]*)\)",
        text,
        re.IGNORECASE,
    ):
        names = {
            part.strip().strip('"')
            for part in match.group(2).split(",")
            if part.strip()
        }
        if names:
            inserted.setdefault(match.group(1), set()).update(names)
    return inserted


def _declared_columns_for(read: dict, table: str) -> list[str] | None:
    """The one declaration of *table*: its DDL, or the landing's own list."""
    if table in read["tables"]:
        return read["tables"][table][0]
    for landing in read["landings"]:
        if landing["table"] == table and landing["columns"]:
            return landing["columns"]
    return None


# ---------------------------------------------------------------------------
# The three-way agreement
# ---------------------------------------------------------------------------


#: Connector modules on 2026-08-31. A FLOOR, not an equality.
#:
#: EVERY test below is parametrized over `_module_dirs()`, and an empty
#: parametrize list does not fail -- pytest simply collects nothing and the file
#: is green. That is criterion 13 of
#: `docs/product-architecture/module-boundaries.md` in its purest form: rename
#: `server/modules/` and this whole file stops measuring without saying so.
_MODULE_DIRS_AT_2026_08_31 = 39


def test_the_module_population_is_whole():
    """The one test here that is NOT parametrized, so it still runs on nothing."""
    found = _module_dirs()
    assert len(found) >= _MODULE_DIRS_AT_2026_08_31, (
        f"{len(found)} connector modules discovered under {_MODULES}, "
        f"{_MODULE_DIRS_AT_2026_08_31} were there on 2026-08-31. Every other "
        "test in this file is parametrized over that list, so a shrunken scan "
        "collects fewer cases and reports nothing at all."
    )


@pytest.mark.parametrize("module", _module_dirs(), ids=_module_ids())
def test_every_landed_table_is_declared_by_its_module(module: Path):
    """A landing whose table nothing else declares writes where nothing reads.

    That is not hypothetical: three modules carried a BigQuery branch landing
    into ``raw_shopify_orders_daily`` / ``raw_square_payments_daily`` /
    ``raw_stripe_charges_daily`` while the DDL and the staging both named the
    table WITHOUT the ``_daily`` suffix.
    """
    read = _read_connector(module)
    staging = _staging_columns(module)
    offenders = []
    for landing in read["landings"]:
        table = landing["table"]
        if table is None:
            offenders.append(f"line {landing['line']}: table is not a literal")
            continue
        if table not in read["tables"] and table not in staging:
            offenders.append(
                f"line {landing['line']}: lands into {table!r}, which the module "
                "neither declares in DDL nor reads in any staging model"
            )
    assert not offenders, f"{module.name}: " + "; ".join(offenders)


@pytest.mark.parametrize("module", _module_dirs(), ids=_module_ids())
def test_landed_columns_equal_the_declaration_in_order(module: Path):
    """The BigQuery landing and the DuckDB DDL are the same table or they are two.

    Order is part of the contract, not cosmetic: the connectors build ONE
    positional value tuple and hand it to both backends.
    """
    read = _read_connector(module)
    offenders = []
    for landing in read["landings"]:
        table = landing["table"]
        if table is None:
            continue
        if landing["columns"] is None:
            offenders.append(
                f"line {landing['line']}: columns= is not a resolvable list of "
                "(name, type) pairs, so nothing can compare it to the DDL"
            )
            continue
        declared = read["tables"].get(table)
        if declared is None:
            continue  # the landing IS the single declaration -- nothing to disagree with
        if declared[0] != landing["columns"]:
            only_declared = [c for c in declared[0] if c not in landing["columns"]]
            only_landed = [c for c in landing["columns"] if c not in declared[0]]
            offenders.append(
                f"line {landing['line']}: {table} landing disagrees with its "
                f"{declared[1]} -- only declared: {only_declared or 'ordering'}; "
                f"only landed: {only_landed or 'ordering'}"
            )
    assert not offenders, f"{module.name}: " + "; ".join(offenders)


@pytest.mark.parametrize("module", _module_dirs(), ids=_module_ids())
def test_staging_reads_only_columns_the_module_declares(module: Path):
    """dbt selecting a column that never lands is the defect seen from the other end."""
    read = _read_connector(module)
    offenders = []
    for table, models in _staging_columns(module).items():
        declared = _declared_columns_for(read, table)
        if declared is None:
            continue  # the raw relation is provisioned elsewhere (seed, shared zone)
        for model, columns in models.items():
            missing = sorted(columns - set(declared) - {"*"})
            if missing:
                offenders.append(
                    f"{model} reads {missing} from {table}, which does not declare them"
                )
    assert not offenders, f"{module.name}: " + "; ".join(offenders)


@pytest.mark.parametrize("module", _module_dirs(), ids=_module_ids())
def test_the_staging_reader_finds_columns_wherever_a_model_exists(module: Path):
    """The comparison above must never pass by having read nothing.

    A guard that silently covers half its subject is worse than no guard: it
    reports green over the case it stopped seeing. If a staging model is written
    in a shape this reader does not understand, that is a failure HERE, to be
    fixed in the reader -- not a module quietly dropping out of the check.
    """
    dbt_dir = module / "dbt"
    if not dbt_dir.exists():
        pytest.skip(f"{module.name} ships no dbt models")
    harvested = _staging_columns(module)
    read = _read_connector(module)
    declared_tables = set(read["tables"]) | {
        landing["table"] for landing in read["landings"] if landing["table"]
    }
    unread = []
    for sql_path in sorted(dbt_dir.rglob("*.sql")):
        text = re.sub(r"--.*", "", sql_path.read_text(encoding="utf-8"))
        for ref in _SOURCE_REF.finditer(text):
            table = ref.group(1)
            if table not in declared_tables:
                continue  # a shared relation, not this module's raw table
            if not harvested.get(table, {}).get(sql_path.name):
                unread.append(f"{sql_path.name} reads {table} but no column could be read")
    assert not unread, f"{module.name}: " + "; ".join(sorted(set(unread)))


@pytest.mark.parametrize("module", _module_dirs(), ids=_module_ids())
def test_no_landing_writes_its_column_list_inline(module: Path):
    """A list spelled at the call site is the second copy, being born again.

    Naming the declaration is what lets the DDL derive from it, and what lets the
    test above compare something to something rather than a literal to itself.
    """
    inline = [
        f"line {landing['line']}"
        for landing in _read_connector(module)["landings"]
        if landing["inline"]
    ]
    assert not inline, (
        f"{module.name}: land_raw_rows is given an inline column list at "
        + ", ".join(inline)
        + " -- bind it to a module-level declaration instead"
    )


@pytest.mark.parametrize("module", _module_dirs(), ids=_module_ids())
def test_seed_declares_the_same_table_as_the_connector(module: Path):
    """The seeded table and the landed table are one table, columns and order.

    A seed that creates a DIFFERENT shape makes every local `dbt build` a proof
    about a table production does not have -- the exact failure mode this file
    was written for, one copy further along. Order is part of it for the same
    reason as the landing: these are positional tuples, and a fixture whose
    columns sit in another order is a fixture that cannot be inserted into the
    real table.

    Only tables the CONNECTOR declares are compared. A seed is free to create
    tables of its own (a `mirror.context_events` fixture, a projection) -- those
    are not second copies of anything, so there is nothing to disagree with.
    """
    read = _read_connector(module)
    offenders = []
    for seed_path in _seed_files(module):
        for table, columns in _seed_tables(seed_path).items():
            declared = _declared_columns_for(read, table)
            if declared is None:
                continue  # a fixture-only table, not a copy of the landing table
            if declared != columns:
                only_declared = [c for c in declared if c not in columns]
                only_seeded = [c for c in columns if c not in declared]
                offenders.append(
                    f"{seed_path.name}: {table} disagrees with the connector's "
                    f"declaration -- only in connector: "
                    f"{only_declared or 'ordering'}; only in seed: "
                    f"{only_seeded or 'ordering'}"
                )
    assert not offenders, f"{module.name}: " + "; ".join(offenders)


@pytest.mark.parametrize("module", _module_dirs(), ids=_module_ids())
def test_seed_inserts_only_columns_the_connector_declares(module: Path):
    """A seed may fill a subset; it may not fill a column that does not exist.

    The other end of the same drift. An INSERT naming a column the landing table
    never had lands rows in the fixture that no pull could ever produce, and a
    staging model written against them reads a shape production will not deliver.
    """
    read = _read_connector(module)
    offenders = []
    for seed_path in _seed_files(module):
        seeded = _seed_tables(seed_path)
        for table, columns in _seed_inserted_columns(seed_path).items():
            declared = _declared_columns_for(read, table)
            if declared is None:
                continue  # a fixture-only table
            # A column the seed itself adds by ALTER is legitimate only if the
            # connector declares it too, which the check below is exactly about.
            missing = sorted(columns - set(declared))
            if missing:
                offenders.append(
                    f"{seed_path.name}: INSERT INTO {table} names {missing}, "
                    f"which the connector's declaration does not carry"
                )
            unknown_to_seed = sorted(columns - set(seeded.get(table, declared)))
            if seeded.get(table) and unknown_to_seed:
                offenders.append(
                    f"{seed_path.name}: INSERT INTO {table} names "
                    f"{unknown_to_seed}, which its own CREATE TABLE does not "
                    f"declare (an ALTER guard is not a declaration)"
                )
    assert not offenders, f"{module.name}: " + "; ".join(offenders)
