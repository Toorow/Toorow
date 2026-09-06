#!/usr/bin/env python
"""Validate every compiled dbt relation and test against BigQuery, at 0 bytes billed.

WHY THIS SCRIPT EXISTS. toorow builds the same dbt project twice: DuckDB in the
local fixture loop, BigQuery in production. A model that spells a type in only
one of the two dialects builds green locally and cannot run in production --
`docs/product-architecture/execution-substrate.md`, "Incomplete if" 23. The
criterion also names the measurement that costs nothing:

    QueryJobConfig(dry_run=True)

BigQuery parses, resolves names and plans the query, then bills 0 bytes and runs
nothing. It is the only instrument that can say "BigQuery accepts this SQL"
without a warehouse full of data and without a bill.

WHAT IT SEPARATES, and why that separation is the whole point. A dry run fails
for two very different reasons:

  * the SQL is not BigQuery SQL -- a parameterised DECIMAL in a CAST, a postfix
    `::`, `DOUBLE`, `VARCHAR`, DuckDB's `regexp_replace(..., 'g')`. That is the
    defect this gate exists to catch, and it is a FAILURE.
  * the SQL is fine but a relation it reads does not exist in this warehouse
    yet. That is `TOOROW_SOURCE_ABSENT` territory (criterion 21), it is normal
    on a warehouse that has not run, and it is NOT a dialect failure. Reported
    as `absent`, never as a red.

Anything else -- a permission, a quota -- is reported under its own heading
rather than folded into either, because a gate that cannot say which of the
three it hit is a gate nobody will trust the second time.

AND `absent` IS A BLIND SPOT, NOT A PASS -- say it out loud (2026-08-31).
BigQuery resolves names BEFORE it plans anything, so a node whose source table
is missing is refused at the first step and the rest of its SQL is never judged.
For the connector stagings that is every single one of them: their `raw_*`
sources live in a per-project dataset this gate does not point at. So the whole
of `server/modules/*/dbt/**` sits in the `absent` bucket, and a green line here
says NOTHING about it. That is how a correlated `LIMIT 1` in
`stg_youtube_breakdown` passed this gate and killed the production nightly the
same evening (revision mcp-server-00226). The count is now printed under its own
heading, `NEVER MEASURED`, so the number is visible instead of implied.

WHY NOT SYNTHESISE THE MISSING SOURCES. The obvious repair -- replace each
absent relation with an empty typed CTE so the SQL is really planned -- needs a
COLUMN LIST, and the sources declare none: `schema.yml` names the tables and
describes them in prose, never their columns (measured 2026-08-31: 0 of the
module source declarations carry a `columns:` block). The shapes exist only in
the local DuckDB fixture, which would make this gate depend on a pytest
temporary directory having been built first -- an instrument measuring another
instrument's leftovers. Until the sources declare their columns, the class is
held OFFLINE instead, in
`server/tests/conformance/test_dbt_speaks_both_engines.py`, where it needs no
warehouse at all.

`--tenancy` IS THE OTHER WAY OUT, AND IT SYNTHESISES NOTHING (2026-09-01). The
nightly does not build into `seeds_shared_staging`; it passes
`--vars '{"project": ..., "raw_schema": ...}'` and builds into
`staging_<project_id>` / `marts_<project_id>` over a raw zone that EXISTS.
Compiling with the same two vars points every node at the datasets production
really reads, so the module SQL is planned instead of skipped -- still at 0 bytes
billed, still reading nothing. Without it, measured 2026-09-01 at HEAD:
1006 compiled nodes, 176 accepted, 830 never measured, 442 of them module SQL.

AND A SCHEMA TEST IS THE ONLY NODE THAT EVER PLANS A VIEW BODY. `CREATE VIEW`
resolves names and types; it does not plan the scalar subqueries inside. So a
`materialized='view'` model can be created and still be unrunnable -- which is
exactly what happened to `stg_youtube_breakdown` on the night of 2026-09-01: the
view was created, and its three `not_null` tests died on *Correlated subqueries
that reference other tables are not supported*. 912 of this project's 1006
compiled nodes ARE tests, and every one of them names a RELATION rather than
carrying its model's SQL -- so a test dry run judges what the warehouse currently
holds, not what this checkout would build.

Both halves are therefore needed, and this script does both:

  * the compiled TEST SQL gets its referenced model relations REPLACED by that
    model's own compiled SQL, recursively, within a size budget. The test is then
    planned against THIS CHECKOUT, and says whether the change in front of you is
    safe -- before a deploy replaces the view. `--no-inline-tests` turns it off,
    which measures the deployed warehouse instead (a pre-flight of tonight's run).
  * the summary counts models and tests separately, because "830 never measured"
    hid the fact that almost all of them were tests.

USAGE

    python scripts/dbt_bigquery_dry_run.py                 # compile, then dry-run all
    python scripts/dbt_bigquery_dry_run.py --skip-compile  # reuse an existing compile
    python scripts/dbt_bigquery_dry_run.py --limit 50      # a bounded sample
    python scripts/dbt_bigquery_dry_run.py --tenancy proj_01... # the nightly's own vars

Exit code is 1 when a node fails for a DIALECT reason that is not in the frozen
`KNOWN_OPEN` allowlist below -- and ALSO when an allowlisted node starts passing,
because an exception nobody deletes is an exception nobody re-measures.
Credentials come from `google.auth.default()` -- gcloud impersonation on a
developer machine, the runtime service account on Cloud Run. No key is stored,
read or written by this script.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DBT_DIR = REPO_ROOT / "dbt"
RUNTIME_PROFILE = DBT_DIR / "profiles" / "runtime-profiles.yml"
#: Under `dbt/target/`, which is gitignored -- a compile artifact of the OTHER
#: engine must not become a tracked file.
DEFAULT_TARGET_PATH = DBT_DIR / "target" / "bigquery"

#: A missing relation is criterion 21's business, not this gate's. BigQuery
#: phrases it in exactly these ways and no other.
ABSENT_RELATION = re.compile(
    r"Not found: (Table|Dataset|Project)\b"
    r"|was not found in location\b"
    r"|does not have (a schema|bigquery\.tables\.get)\b",
    re.I,
)

#: An access or quota problem is "could not be measured", which is neither a
#: pass nor a dialect failure. Classification is otherwise deliberately
#: exhaustive: anything that is not one of these two IS a dialect failure,
#: because an allowlist of "acceptable" errors is how a gate stops measuring.
ACCESS_PROBLEM = re.compile(
    r"Access Denied|Permission .* denied|does not have permission|quota|Quota", re.I
)

#: A compiled node that came from a connector module. dbt mirrors the source
#: tree under `target/compiled/`, so the module path survives into the artifact.
MODULE_NODE = re.compile(r"/server/modules/[^/]+/dbt/")

#: A fully-qualified BigQuery relation as dbt renders it: `project`.`dataset`.`name`.
#: Only the last part identifies the node, which is what the inliner needs.
QUALIFIED_RELATION = re.compile(r"`[A-Za-z0-9_-]+`\.`[A-Za-z0-9_-]+`\.`([A-Za-z0-9_]+)`")

#: BigQuery refuses a query longer than 1 MB. Inlining is recursive, so a mart
#: test can pull a whole subtree in; past this budget the remaining references
#: are left as warehouse relations rather than truncating anything.
INLINE_BUDGET = 400_000

#: A string literal, a line comment or a block comment -- the three places where a
#: `project`.`dataset`.`name` is NOT a relation reference. dbt writes
#: `-- depends_on: \`p\`.\`d\`.\`m\`` above every ephemeral model, and substituting
#: a model body inside that comment buries the opening parenthesis in it and
#: leaves the body dangling. Measured: 76 nodes broke this way on the first run.
SQL_NON_CODE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"])*\"|--[^\n]*|/\*.*?\*/", re.S)


#: FROZEN ALLOWLIST -- EMPTY, and that is the measurement.
#:
#: It held seven entries on 2026-08-31: two `accepted_values` over BOOLEAN seed
#: columns, and five connector stagings spelling `SELECT * EXCLUDE (...)`. Both
#: were "outside the lot that built the gate", which is a true sentence and was
#: still a production outage: the FIRST real per-project run on BigQuery
#: (revision mcp-server-00225) failed on exactly those two classes, plus a third
#: -- `list_contains` in `stg_youtube_breakdown` -- that no allowlist had even
#: named because `server/modules/*/dbt/**` was outside the perimeter.
#:
#: All three classes are repaired and the perimeter now covers the modules, so
#: the ratchet is at zero. It may only ever SHRINK. A node that starts passing
#: while listed here is reported as a failure of its own, so nobody can leave a
#: healed entry sitting here looking like work.
KNOWN_OPEN: dict[str, str] = {}


def known_open_key(path):
    """The KNOWN_OPEN key this compiled node matches, if any."""
    posix = path.as_posix()
    for key in KNOWN_OPEN:
        if posix.endswith(key):
            return key
    return None


@dataclass(frozen=True)
class NodeResult:
    path: str
    verdict: str  # "ok" | "absent" | "dialect" | "access"
    message: str = ""
    kind: str = "model"  # "model" | "test"


def is_test_node(path: pathlib.Path) -> bool:
    """True for a compiled dbt TEST rather than a model.

    dbt writes a generic test under a directory named after the yml that declares
    it (`.../schema.yml/not_null_stg_x_date.sql`) and a singular test under the
    project's `tests/` path. Both spellings appear in this repo -- the connector
    modules carry their own `schema.yml`, and `dbt/tests/` holds the singular
    ones -- so both are recognised here rather than in two places.
    """
    posix = path.as_posix()
    return path.parent.name.endswith(".yml") or "/dbt/tests/" in posix


def model_bodies(target_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Every compiled MODEL, by relation name.

    Seeds compile to no SQL and are absent from this map on purpose: a seed is a
    real table in the warehouse, and a test that reads one should read it.
    """
    bodies: dict[str, pathlib.Path] = {}
    for path in compiled_sql_files(target_path):
        if is_test_node(path):
            continue
        bodies[path.stem] = path
    return bodies


def inline_model_bodies(sql: str, bodies: dict[str, pathlib.Path]) -> str:
    """Replace every reference to a compiled model by that model's own SQL.

    WHY. A compiled test names a RELATION, so dry-running it as written asks
    BigQuery about the view the warehouse holds RIGHT NOW -- which is the last
    deployment's SQL, not this checkout's. Substituting the model's compiled body
    makes the test measure the tree in front of you, which is the only thing a
    pre-deploy gate can usefully say. It is also the only place a view body is
    ever PLANNED: `CREATE VIEW` resolves names and stops there.

    Recursive, so a mart test carries its staging bodies too, and bounded twice:
    a node already inlined on the current path is left alone (a self-referencing
    incremental model would otherwise not terminate), and expansion stops at
    INLINE_BUDGET rather than emitting a query BigQuery would reject for size.
    """

    def expand(text: str, seen: frozenset[str]) -> str:
        masked = [m.span() for m in SQL_NON_CODE.finditer(text)]

        def in_comment_or_string(start: int) -> bool:
            return any(lo <= start < hi for lo, hi in masked)

        def one(match: re.Match[str]) -> str:
            name = match.group(1)
            source = bodies.get(name)
            if source is None or name in seen or in_comment_or_string(match.start()):
                return match.group(0)
            body = source.read_text(encoding="utf-8").rstrip()
            if len(text) + len(body) > INLINE_BUDGET:
                return match.group(0)
            # The newlines are load-bearing: a compiled model can end on a `--`
            # comment, which would otherwise swallow the closing parenthesis.
            return "(\n" + expand(body, seen | {name}) + "\n)"

        return QUALIFIED_RELATION.sub(one, text)

    return expand(sql, frozenset())


def credentials_available() -> tuple[bool, str]:
    """True when `google.auth.default()` yields usable credentials."""
    try:
        import google.auth
    except ImportError as exc:  # pragma: no cover - hard dependency of the server
        return False, f"google-auth is not installed ({exc})"
    try:
        _creds, project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/bigquery"]
        )
    except Exception as exc:  # DefaultCredentialsError and friends
        return False, str(exc)
    return True, project or ""


def resolve_project(explicit: str | None = None) -> str:
    for candidate in (
        explicit,
        os.environ.get("GCP_PROJECT"),
        os.environ.get("GOOGLE_CLOUD_PROJECT"),
    ):
        if candidate:
            return candidate
    _ok, project = credentials_available()
    return project or ""


def tenancy_vars(project_id: str, raw_schema: str | None = None) -> str:
    """The `--vars` the nightly passes, composed the one way it composes them.

    `scheduler._run_dbt_per_project` sends exactly two: `project`, read by
    `generate_schema_name` to place staging and marts, and `raw_schema`, read by
    every module `schema.yml` to place the raw source. `warehouse_tenancy`
    composes the raw zone as `raw_<project_id>`; passing `--raw-schema` overrides
    it for a warehouse that names its zones otherwise.
    """
    return json.dumps({"project": project_id, "raw_schema": raw_schema or f"raw_{project_id}"})


def compile_for_bigquery(
    target_path: pathlib.Path, project: str, vars_json: str | None = None
) -> None:
    """`dbt compile --target bigquery`, with the profile the served image ships.

    `runtime-profiles.yml` is the production profile, and it is NOT named
    `profiles.yml` on purpose (the local DuckDB loop regenerates that name). It
    is copied under the name dbt looks for into a throwaway directory, so this
    script never edits a tracked file.

    `vars_json`, when given, is the nightly's own `--vars` (see `tenancy_vars`):
    without it every per-project node compiles against a dataset that does not
    exist and comes back `absent`, which is the blind spot named at the top.
    """
    if not RUNTIME_PROFILE.exists():
        raise SystemExit(f"missing {RUNTIME_PROFILE} -- nothing declares the BigQuery target")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="toorow-bq-profiles-"))
    try:
        shutil.copyfile(RUNTIME_PROFILE, tmp / "profiles.yml")
        env = dict(os.environ, GCP_PROJECT=project)
        cmd = [
            "uv", "run", "--project", "../server", "dbt", "compile",
            "--profiles-dir", str(tmp),
            "--target", "bigquery",
            "--target-path", str(target_path),
        ]
        if vars_json:
            cmd += ["--vars", vars_json]
        proc = subprocess.run(cmd, cwd=DBT_DIR, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout[-6000:])
            sys.stderr.write(proc.stderr[-4000:])
            raise SystemExit("dbt compile --target bigquery failed; nothing to dry-run")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compiled_sql_files(target_path: pathlib.Path) -> list[pathlib.Path]:
    root = target_path / "compiled"
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.sql") if p.is_file())


def dry_run_one(client, sql: str) -> tuple[str, str]:
    from google.cloud import bigquery

    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        client.query(sql, job_config=config)
    except Exception as exc:
        message = str(exc).split("\n")[0][:400]
        if ABSENT_RELATION.search(str(exc)):
            return "absent", message
        if ACCESS_PROBLEM.search(str(exc)):
            return "access", message
        return "dialect", message
    return "ok", ""


def dry_run_all(
    files: list[pathlib.Path],
    project: str,
    threads: int = 8,
    bodies: dict[str, pathlib.Path] | None = None,
) -> list[NodeResult]:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)

    def run(path: pathlib.Path) -> NodeResult:
        kind = "test" if is_test_node(path) else "model"
        sql = path.read_text(encoding="utf-8").strip()
        if not sql:
            return NodeResult(str(path), "ok", kind=kind)
        if kind == "test" and bodies:
            sql = inline_model_bodies(sql, bodies)
        verdict, message = dry_run_one(client, sql)
        return NodeResult(str(path), verdict, message, kind=kind)

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        return list(pool.map(run, files))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BigQuery dry run over the compiled dbt project")
    parser.add_argument("--skip-compile", action="store_true")
    parser.add_argument("--target-path", default=str(DEFAULT_TARGET_PATH))
    parser.add_argument("--project", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument(
        "--tenancy",
        default=None,
        metavar="PROJECT_ID",
        help="compile with the nightly's own --vars so per-project nodes address "
             "datasets that exist (staging_<id> / marts_<id> over raw_<id>)",
    )
    parser.add_argument(
        "--raw-schema",
        default=None,
        help="raw dataset for --tenancy; defaults to raw_<PROJECT_ID>",
    )
    parser.add_argument(
        "--no-inline-tests",
        action="store_true",
        help="dry-run each test against the relation it names instead of this "
             "checkout's model SQL -- i.e. measure the deployed warehouse",
    )
    args = parser.parse_args(argv)

    ok, detail = credentials_available()
    if not ok:
        print(f"SKIPPED -- no BigQuery credentials reachable: {detail}")
        print("  `gcloud auth application-default login`, or run this on the deployment.")
        return 0

    project = resolve_project(args.project)
    if not project:
        print("SKIPPED -- credentials carry no project; pass --project or set GCP_PROJECT")
        return 0

    target_path = pathlib.Path(args.target_path)
    vars_json = tenancy_vars(args.tenancy, args.raw_schema) if args.tenancy else None
    if not args.skip_compile:
        print(f"compiling for target=bigquery project={project} -> {target_path}")
        if vars_json:
            print(f"  tenancy vars: {vars_json}")
        compile_for_bigquery(target_path, project, vars_json)

    files = compiled_sql_files(target_path)
    if not files:
        print(f"SKIPPED -- no compiled SQL under {target_path / 'compiled'}")
        return 0
    if args.limit:
        files = files[: args.limit]

    bodies = None if args.no_inline_tests else model_bodies(target_path)
    print(f"dry-running {len(files)} compiled nodes against BigQuery project={project}")
    print(
        "  tests read %s"
        % (
            "the relations they name (deployed warehouse)"
            if bodies is None
            else f"this checkout's SQL -- {len(bodies)} model bodies inlined"
        )
    )
    results = dry_run_all(files, project, threads=args.threads, bodies=bodies)

    buckets: dict[str, list[NodeResult]] = {"ok": [], "absent": [], "dialect": [], "access": []}
    for r in results:
        buckets[r.verdict].append(r)

    new_failures, allowed = [], []
    for r in buckets["dialect"]:
        (allowed if known_open_key(pathlib.Path(r.path)) else new_failures).append(r)

    # THE RATCHET. An allowlisted node BigQuery now accepts is a stale entry, and
    # a stale exception is how an allowlist stops being a measurement.
    healed = sorted(
        {
            known_open_key(pathlib.Path(r.path))
            for r in results
            if r.verdict != "dialect" and known_open_key(pathlib.Path(r.path))
        }
    )

    if new_failures:
        print("\n== BIGQUERY REFUSED THE SQL ITSELF -- %d ==" % len(new_failures))
        for r in new_failures:
            print("  %s\n      %s" % (pathlib.Path(r.path).as_posix(), r.message))
    if allowed:
        print("\n== KNOWN OPEN, frozen allowlist -- %d ==" % len(allowed))
        for r in allowed:
            key = known_open_key(pathlib.Path(r.path))
            print("  %s\n      %s\n      why kept: %s"
                  % (pathlib.Path(r.path).as_posix(), r.message, KNOWN_OPEN[key]))
    if healed:
        print("\n== ALLOWLISTED BUT NOW ACCEPTED -- delete from KNOWN_OPEN -- %d ==" % len(healed))
        for key in healed:
            print("  " + key)
    if buckets["access"]:
        print("\n== COULD NOT BE MEASURED (access / quota) -- %d ==" % len(buckets["access"]))
        for r in buckets["access"]:
            print("  %s\n      %s" % (pathlib.Path(r.path).as_posix(), r.message))

    # THE BLIND SPOT, NAMED. `absent` means BigQuery stopped at name resolution,
    # so nothing downstream of the missing relation was planned -- and for the
    # connector stagings that is the whole file. Printing the number is the
    # difference between a gate that knows what it did not look at and one that
    # reads as green over it.
    never_measured = buckets["absent"]
    module_blind = [r for r in never_measured if MODULE_NODE.search(r.path.replace("\\", "/"))]
    blind_tests = [r for r in never_measured if r.kind == "test"]
    if never_measured:
        print(
            "\n== NEVER MEASURED -- BigQuery stopped at name resolution -- %d "
            "(%d of them connector-module SQL, %d of them TESTS) =="
            % (len(never_measured), len(module_blind), len(blind_tests))
        )
        print(
            "  A relation they read is not in this warehouse, so their SQL was never "
            "planned and this run says NOTHING about it. Two defects have already hidden "
            "here and reached production: a correlated `LIMIT 1` in a module staging, and "
            "`BOOL_OR` in the mirror-reading `plan_pacing_*` marts. The offline half of "
            "tests/conformance/test_dbt_speaks_both_engines.py is what covers them."
        )
        if not args.tenancy:
            print(
                "  Most of this is tenancy, not dialect: without --tenancy every "
                "per-project node addresses seeds_shared_staging / seeds_shared_marts, "
                "which no warehouse holds. Pass --tenancy <project_id> to compile with "
                "the nightly's own vars and have them planned."
            )

    tested = [r for r in results if r.kind == "test"]
    modelled = [r for r in results if r.kind == "model"]
    print(
        "\naccepted=%d  never-measured=%d (module-sql=%d)  "
        "dialect-failures=%d  known-open=%d  stale-allowlist=%d  unmeasurable=%d  "
        "bytes billed=0"
        % (
            len(buckets["ok"]),
            len(never_measured),
            len(module_blind),
            len(new_failures),
            len(allowed),
            len(healed),
            len(buckets["access"]),
        )
    )
    # THE SPLIT, because "830 never measured" said nothing about WHICH half was
    # blind, and 912 of the 1006 nodes are tests -- the only nodes that ever plan
    # a view body.
    print(
        "  models: %d planned / %d\n  tests:  %d planned / %d"
        % (
            sum(1 for r in modelled if r.verdict == "ok"),
            len(modelled),
            sum(1 for r in tested if r.verdict == "ok"),
            len(tested),
        )
    )
    # ANTI-VACUITY. Pointed at a project whose datasets do not exist, every node
    # comes back `absent` and the run would report a serene green having validated
    # nothing at all. A gate that measured nothing says so.
    if not buckets["ok"]:
        print(
            "\nMEASURED NOTHING: every node came back absent or refused. Point this at "
            "the warehouse the project really builds into (GCP_PROJECT / --project) "
            "before reading the line above as a pass."
        )
        return 1

    return 1 if (new_failures or healed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
