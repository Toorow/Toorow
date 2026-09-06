"""No governed rule family may grow a second store that ANSWERS beside it.

`governance.md`'s `Incomplete if` clause 3 -- *"reconciliation, DQ or capability
rules create a parallel semantic or evidence store"* -- was closed on 2026-08-17.
Closing it needed two repairs on the same day, both of the same shape, and this
file exists so the shape cannot come back quietly.

WHAT THE SHAPE IS. A story moves an authority (a view redefined over a published
Rule Set version, a runtime cut over to a governed resolver) and leaves ONE
reader behind. Nothing goes red, because the abandoned store still answers -- it
just answers differently, and no test compares the two. Both defects found this
week ran for days in exactly that state:

* AI-295 -- the metric reference served a reconciliation rule for a project id
  that exists nowhere, because the read surfaces stayed on the retired
  `app.overlap_groups` cascade after the runtime left it.
* The Cost tab named its rules from `app.fee_tax_rules` while its numbers came
  from a mart reading `app.fee_tax_rules_dim_v`, redefined over the published
  `tax_fee` version by migration 146. The two id namespaces cannot collide, so
  the screen served "this Project's rule store no longer carries them" as a
  permanent state.

WHAT IS GUARDED HERE is the third one, caught before it fired: a mutable
`reporting_timezone` still reaches the warehouse through
`mirror.project_preferences` -> `dim_project`, while
`capability_compilers.ReportingTimezoneCompiler` was deliberately moved OFF that
column onto the governed Timezone Policy version, because the column carries
`DEFAULT 'Europe/Paris'` and *"a default is not a decision"*.

Today that column is DORMANT: it is projected by `dim_project` and read by no
model. Dormant is not the same as absent, and it is the more dangerous of the
two -- the next model that needs a timezone finds one already in `dim_project`
and uses the default nobody chose. This test does not forbid the column; it
forbids a CONSUMER appearing without the decision being taken first.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_DBT_MODELS = _REPO_ROOT / "dbt" / "models"

#: The model allowed to name the column: the projection itself.
_PROJECTOR = "dim_project.sql"


def _sql_models() -> list[Path]:
    return [p for p in _DBT_MODELS.rglob("*.sql") if "target" not in p.parts]


def test_no_dbt_model_consumes_the_mutable_reporting_timezone():
    """`dim_project.reporting_timezone` is projected and must stay unread.

    A consumer would make `app.project_preferences.reporting_timezone` a SECOND
    reporting-boundary authority beside the governed Timezone Policy version,
    which is the clause this file exists to keep closed.
    """
    consumers = [
        model.relative_to(_REPO_ROOT).as_posix()
        for model in _sql_models()
        if model.name != _PROJECTOR and "reporting_timezone" in model.read_text(encoding="utf-8")
    ]
    assert not consumers, (
        "a dbt model reads `reporting_timezone` from the warehouse: "
        f"{consumers}. The column reaching `dim_project` comes from "
        "`app.project_preferences`, whose DEFAULT 'Europe/Paris' is not a "
        "decision -- `capability_compilers.ReportingTimezoneCompiler` was moved "
        "off it onto the published Timezone Policy version for that reason. "
        "Reading it here re-opens a second authority. Take the decision first: "
        "either the governed policy reaches the warehouse under its own name, or "
        "this column is dropped from the projection."
    )


def test_the_cost_cascade_names_its_rules_from_the_governed_projection():
    """The repair of 2026-08-17, held at the file that made the mistake.

    `datastream_workbench_cost` must name rules from the same relation the mart's
    `applied_rule_ids` are minted by. Held here as well as in its own unit test
    because this is where the CLASS is written down, and the class is what comes
    back.
    """
    source = (_REPO_ROOT / "server" / "core" / "datastream_workbench_cost.py").read_text(
        encoding="utf-8"
    )
    selects = re.findall(r"FROM\s+(app\.fee_tax_rules\w*)", source)
    assert selects, "the cascade no longer reads any fee/tax rule relation -- check this test"
    assert set(selects) == {"app.fee_tax_rules_dim_v"}, (
        f"the cost cascade reads {sorted(set(selects))}. Its numbers come from "
        "`fee_tax_ladder_daily`, whose ids are minted by `app.fee_tax_rules_dim_v` "
        "over the published `tax_fee` version (migration 146). Reading the mutable "
        "table beside it is the parallel store clause 3 forbids, and it renders as "
        "`LEVEL_UNRESOLVED_REASON` on every row."
    )


# ---------------------------------------------------------------------------
# Retired stores: a relation a migration declared un-adoptable must have no
# live SQL anywhere in the server tree.
# ---------------------------------------------------------------------------

#: relation -> (the governed authority that replaced it, the migration that said so).
#:
#: A TABLE AND NOT A HARDCODED NAME, because this is a class and not an
#: instance. `app.dq_baselines` is the third store of the same shape found this
#: month; the first two (`app.overlap_groups`, the `app.fee_tax_rules` cascade)
#: were each repaired on their own and each needed someone to notice. Retiring
#: the next one is a line here.
_RETIRED_STORES = {
    "app.dq_baselines": (
        "the `baseline.columns` of the Datastream's published `schema` "
        "DQ Monitor version, read through `dq_monitor_bridge.published_baseline`",
        "infra/nango/migrations/145_controls_and_quality.sql",
    ),
    "app.overlap_groups": (
        "the published Rule Set version, read through "
        "`controls_quality.governed_runtime_rule`",
        "infra/nango/migrations/145_controls_and_quality.sql",
    ),
    "app.overlap_group_members": (
        "the `sources` a published Rule Set version pins",
        "infra/nango/migrations/145_controls_and_quality.sql",
    ),
}

#: `FROM app.x`, `JOIN app.x`, `INTO app.x`, `UPDATE app.x`, `DELETE FROM app.x`.
#: Five spellings and not one: a guard that reads only `FROM` is blind to every
#: writer, which is exactly the half that mattered here.
_SQL_VERBS = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM|TABLE)\s+(app\.[a-z_]+)", re.IGNORECASE
)


def _python_sources() -> list[Path]:
    """Every module of the server tree -- never one package.

    A guard scoped to `server/core/` reads nothing of `server/inbound/`,
    `server/modules/` or `server/jobs/`, and a reader that moved out of `core`
    would take the retired store with it and stay green.
    """
    root = _REPO_ROOT / "server"
    return [
        p
        for p in root.rglob("*.py")
        if "tests" not in p.parts and "__pycache__" not in p.parts
    ]


#: The two populations this file reads, on 2026-08-31. FLOORS, not equalities.
#: Criterion 13 of `docs/product-architecture/module-boundaries.md`: the tests
#: below all end on `assert not offenders`, and an empty rglob says exactly that.
_DBT_MODELS_AT_2026_08_31 = 33
_SERVER_MODULES_AT_2026_08_31 = 679


def test_both_scans_still_reach_their_tree():
    """A renamed `dbt/models/` or `server/` must redden HERE, not go quiet."""
    models = _sql_models()
    modules = _python_sources()
    assert len(models) >= _DBT_MODELS_AT_2026_08_31, (
        f"{len(models)} dbt models under {_DBT_MODELS}, "
        f"{_DBT_MODELS_AT_2026_08_31} on 2026-08-31 -- the timezone verdict "
        "below is about nothing."
    )
    assert len(modules) >= _SERVER_MODULES_AT_2026_08_31, (
        f"{len(modules)} python modules under server/, "
        f"{_SERVER_MODULES_AT_2026_08_31} on 2026-08-31 -- the retired-store "
        "verdict below is about nothing."
    )


def test_no_module_reads_or_writes_a_retired_store():
    """The last live writer of `app.dq_baselines` is gone, and stays gone.

    THE DEFECT THIS HOLDS SHUT, measured 2026-08-21. `governance.md` ratified an
    order on 2026-08-17 -- a published DQ Monitor version wins, `app.dq_baselines`
    answers only for a Datastream no published monitor covers -- and no code
    anywhere implemented it: `_run_monitors_for_datastream` passed `version=None`
    for EVERY Datastream, so the nightly sweep read and wrote the side table for
    every stream, including the ones a published monitor already covered. The
    ratified sentence was true of the manual route only.

    Migration 145 REFUSES to apply while `app.dq_baselines` holds a single row --
    such a row "carries no monitor identity, no version and no decision date, so
    adopting it would invent the governed identity it never had" -- and the sweep
    re-created exactly those rows every night. A store one migration declares
    un-adoptable and the runtime keeps refilling is a parallel evidence store,
    whichever layer it is called.
    """
    offenders: dict[str, list[str]] = {}
    for path in _python_sources():
        for relation in set(_SQL_VERBS.findall(path.read_text(encoding="utf-8"))):
            if relation.lower() in _RETIRED_STORES:
                offenders.setdefault(relation.lower(), []).append(
                    path.relative_to(_REPO_ROOT).as_posix()
                )
    assert not offenders, "\n".join(
        f"{relation} is read or written by {sorted(files)} -- it was retired by "
        f"{_RETIRED_STORES[relation][1]}. The authority is {_RETIRED_STORES[relation][0]}. "
        "Read that one, or take the decision to un-retire this store in "
        "`docs/product-architecture/governance.md` first."
        for relation, files in sorted(offenders.items())
    )


def test_the_retired_store_table_names_relations_a_migration_actually_retired():
    """A table nobody can trust is worse than no table.

    Each entry must name a migration file that exists and that mentions the
    relation -- otherwise an entry can be added by hand, protect nothing, and
    read as proof.
    """
    unproven = [
        relation
        for relation, (_, migration) in _RETIRED_STORES.items()
        if not (_REPO_ROOT / migration).exists()
        or relation.split(".")[-1] not in (_REPO_ROOT / migration).read_text(encoding="utf-8")
    ]
    assert not unproven, (
        f"{unproven} claim a retiring migration that does not name them. "
        "An entry here is a fact about a migration, not a wish."
    )
