"""A migration that names `org_purge` must be RIGHT about it.

WHY THIS FILE EXISTS. Three migrations in a row wrote the same false sentence in
the one place whose whole purpose is to say what the database guarantees:

    story 60.1  -- rejected for it;
    story 60.6  -- header corrected for it after review;
    story 61.1  -- wrote it again, in `244`, and it was caught by review again.

Nothing in the repository could see it. `finished_work_audit.py --gate` exits 0
on all three. The sentence is plausible, it sits in a comment, and the erasure it
describes really does happen -- by a DIFFERENT mechanism. So the next reader is
sent to `core/org_purge.py`, greps for the table, finds nothing, and concludes
the rows are never erased.

THE FACT THE CLAIM GETS WRONG, in one line of `org_purge.py` (`_FK_GRAPH_SQL`):

    AND c.confdeltype IN ('a', 'r')          -- NO ACTION / RESTRICT only

`plan_purge` walks the foreign-key graph through NO ACTION and RESTRICT edges
ONLY. A CASCADE edge is deliberately absent from that plan, because Postgres
already does the work. So a table whose every foreign key is ON DELETE CASCADE is
erased -- and `org_purge` names it in ZERO of its statements. That zero is not
quoted from the day this file was written: the last test below re-runs
`plan_purge` against the live catalog and measures it, so the total number of
statements -- which grows with the schema -- never has to be written down
anywhere to keep the claim honest.

WHAT THIS GUARD ASKS, AND IT IS THE NARROW VERSION. For every migration that
CREATES a table in `app.` and names `org_purge` anywhere, at least one table it
creates must carry a foreign key the graph can see -- `confdeltype IN ('a','r')`.
A migration whose tables are all CASCADE-only must instead carry the CANONICAL
DENIAL, the phrase migration 235 established:

    NOT `core.org_purge`

That is a sentence, checked as a string, and it is on purpose: the point of the
class is what the header SAYS, so what the guard reads is what the header says.

IT READS `pg_constraint`, NOT THE SQL TEXT, and that decision was measured. A
hand-rolled parser of the migration files was written first and disagreed with
the catalog on 66 of 283 tables -- `ON DELETE` clauses that wrap a line, foreign
keys added by a later `ALTER`, dollar-quoted `DO` bodies. A guard that is wrong
66 times is a guard with false teeth. This reads the same catalog `org_purge`
itself reads, so it cannot answer differently from the thing it is guarding.

ITS SCOPE, STATED BECAUSE A PARTIAL RATCHET THAT HIDES THE REST IS THE FAULT NEXT
DOOR. It binds migrations whose FILES may still change. The five historic claims
below CANNOT be repaired: `scripts/apply_migrations.py` raises `checksum drift:
applied migration changed` on any edit to an applied migration, and those five
are applied on preprod. They are listed one by one, with what each one says, so
they are inventory rather than silence.
"""

from __future__ import annotations

import pathlib
import re

MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / "infra" / "nango" / "migrations"

#: The exact phrase a migration writes when `org_purge` does NOT reach its rows.
#: Established by `235_a_client_value_table_names_the_streams_it_serves.sql`
#: ("It is NOT `core.org_purge` that reaches them") and reused by 240 and 244.
#: Checked as a literal so the correct form is contagious: a reader who trips
#: this guard is handed the sentence to write, not a rule to interpret.
DENIAL = "NOT `core.org_purge`"

#: The delete actions `org_purge._FK_GRAPH_SQL` admits, mirrored from it.
#: `a` = NO ACTION, `r` = RESTRICT. Everything else -- `c` CASCADE, `n` SET NULL,
#: `d` SET DEFAULT -- is invisible to `plan_purge` BY DESIGN.
GRAPH_VISIBLE_DELETE_ACTIONS = frozenset({"a", "r"})

#: The claims that predate this guard and that no edit can repair, each with what
#: it actually says. Dated 2026-08-09; nothing may be added here without a reason
#: of the same kind, and a NEW migration can never qualify -- an unapplied file is
#: editable, so its header is a choice and not a fact.
#: CORRECTED BY MIGRATION 266 (AI-263, 2026-08-16), and the entries stay.
#:
#: These five cannot be repaired in place -- `apply_migrations.py` raises
#: `checksum drift: applied migration changed` and all five are applied on
#: preprod -- so the true statement lives in a FORWARD migration whose header
#: carries the canonical denial for the tables they created, with the
#: measurement that supports it: ZERO plan statements name any of them, and every
#: one is CASCADE-only.
#:
#: Migration 266 wrote that measurement as a fraction of a count that has since
#: moved, and its file is frozen by checksum, so the count in it can only ever be
#: a dated fact. The count is NOT copied here: what has to stay true today is
#: derived from the live catalog by
#: `test_no_frozen_claim_table_is_named_by_the_plan` below, which re-runs
#: `plan_purge` and asserts the zero rather than quoting it.
#:
#: The entries below are therefore INVENTORY, not exemption: each one still says
#: what its file gets wrong, and 266 is where a reader finds the right address.
#: Removing them would make five false headers invisible again.
FROZEN_CLAIMS: dict[str, str] = {
    "105_language_dimension_family.sql": (
        "« l'effacement d'org passe par la FK, decouverte par le graphe de "
        "core/org_purge.py » -- `dimension_field_bindings` is CASCADE-only, so the "
        "graph never sees it. The erasure is real; the mechanism named is not."
    ),
    "106_dimension_labels.sql": (
        "The same French sentence as 105, over `dimension_labels`, which is also "
        "CASCADE-only."
    ),
    "107_target_fields_versions.sql": (
        "This one is a DENIAL -- « org_purge.py, which never references "
        "target_fields » -- written before 235 fixed the wording, so it does not "
        "carry the canonical phrase. The header is CORRECT; only its form is old."
    ),
    "119_fee_tax_alignment.sql": (
        "The sharpest instance: « org_purge.py discovers the tenant tree from the "
        "FK graph, so the ON DELETE CASCADE below is picked up with no "
        "registration ». A CASCADE edge is exactly what that graph excludes."
    ),
    "146_tax_fee_governed_ladder.sql": (
        "« org_purge walks the FK graph to reach them » about "
        "`fx_rate_observations`, a table 146 does not create. The claim is about "
        "somebody else's table, which is a second shape of the same fault."
    ),
}

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?app\.([a-z0-9_]+)", re.IGNORECASE
)


def _created_tables(sql: str) -> set[str]:
    """Every `app.` table this migration creates. Over-reads rather than under-reads:
    a `CREATE TABLE` inside a `DO` body counts, which is what several of them use."""
    return set(_CREATE_TABLE.findall(sql))


def _delete_actions(conn) -> dict[str, set[str]]:
    """`{table -> {confdeltype}}` for every foreign key of the `app` schema.

    The SAME catalog read `org_purge` performs, minus its filter -- the filter is
    what this guard is checking against, so applying it here would make the
    comparison compare a thing with itself.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.conrelid::regclass::text, c.confdeltype "
            "FROM pg_constraint c "
            "WHERE c.contype = 'f' AND c.connamespace = 'app'::regnamespace"
        )
        rows = cur.fetchall()
    actions: dict[str, set[str]] = {}
    for table, confdeltype in rows:
        actions.setdefault(str(table).split(".")[-1].strip('"'), set()).add(confdeltype)
    return actions


def _offenders(conn) -> list[str]:
    """Every migration whose header claims `org_purge` and whose tables refuse it."""
    actions = _delete_actions(conn)
    offenders: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8", errors="replace")
        if "org_purge" not in sql:
            continue
        if DENIAL in sql:
            continue
        tables = _created_tables(sql)
        if not tables:
            # It names `org_purge` and creates nothing -- a grant, a trigger, a
            # hatch. There is no table for the claim to be wrong about.
            continue
        known = [table for table in tables if table in actions]
        if not known:
            # Every table it creates is gone from the schema, or the database is
            # behind this migration. Nothing measurable, so nothing asserted --
            # `test_the_guard_is_not_vacuous` is what stops that from swallowing
            # the whole corpus.
            continue
        if any(actions[table] & GRAPH_VISIBLE_DELETE_ACTIONS for table in known):
            continue
        offenders.append(path.name)
    return offenders


def test_a_migration_naming_org_purge_is_reached_by_org_purge(live_postgres) -> None:
    offenders = sorted(set(_offenders(live_postgres)) - set(FROZEN_CLAIMS))

    assert not offenders, (
        "migration(s) whose header names `core.org_purge` while every table they "
        f"create is invisible to its FK graph: {offenders}\n\n"
        "`plan_purge` walks `confdeltype IN ('a','r')` only -- NO ACTION and "
        "RESTRICT. A table reached only by ON DELETE CASCADE is erased by "
        "POSTGRES, from the parent statement `org_purge` does emit, and "
        "`org_purge` names it in none of its own.\n\n"
        "Two ways out, and only these two:\n"
        "  - give the table a foreign key the graph can see; or\n"
        f"  - write the truth, in the words migration 235 fixed: `{DENIAL}` -- "
        "then say WHICH parent statement cascades down to it, and measure it.\n\n"
        "Do not repair this by deleting the sentence: an org-scoped table naming "
        "no parent at all is invisible to BOTH mechanisms, and that is the bug "
        "this comment exists to prevent."
    )


def test_the_guard_is_not_vacuous(live_postgres) -> None:
    """It really reads the catalog, and it really refuses a planted claim.

    Three assertions, because each covers a different way this file could pass on
    nothing: an empty catalog, a corpus it never opened, and a rule with no teeth.
    """
    actions = _delete_actions(live_postgres)
    # 1. The catalog answered, and it carries BOTH shapes -- otherwise the
    #    comparison is an accident.
    assert len(actions) > 100, len(actions)
    assert any(value & GRAPH_VISIBLE_DELETE_ACTIONS for value in actions.values())
    assert any(not (value & GRAPH_VISIBLE_DELETE_ACTIONS) for value in actions.values())

    # 2. The corpus was really opened, and the two known shapes are found in it.
    #    `plan_line_placement_mappings` (migration 244) is CASCADE-only and
    #    carries the denial; `master_data_aliases` (143) is graph-visible.
    assert actions.get("plan_line_placement_mappings") == {"c"}
    assert actions["master_data_aliases"] & GRAPH_VISIBLE_DELETE_ACTIONS
    assert len(list(MIGRATIONS.glob("*.sql"))) > 200

    # 3. THE TEETH. Plant the claim in a COPY of the file that is right today --
    #    strip its denial -- and require the guard to name it. Without this, a
    #    rule that returns `[]` unconditionally passes every assertion above.
    planted = MIGRATIONS / "244_a_plan_line_names_the_placements_it_bought.sql"
    original = planted.read_text(encoding="utf-8")
    assert DENIAL in original, "the fixture of this proof no longer carries the denial"
    try:
        claimed = original.replace(DENIAL, "reached by `core.org_purge`")
        planted.write_text(claimed, encoding="utf-8")
        assert planted.name in _offenders(live_postgres)
    finally:
        planted.write_text(original, encoding="utf-8")
    # And restored byte for byte: a guard that leaves the corpus modified is a
    # guard that breaks the migration checksums of the next run.
    assert planted.read_text(encoding="utf-8") == original


#: An org id that exists nowhere. `plan_purge` builds its statements from the
#: foreign-key graph and not from rows, so the plan is complete for any id, and
#: nothing this test touches can leave a trace.
_NO_SUCH_ORG = "org_EXAMPLE"


def test_no_frozen_claim_table_is_named_by_the_plan(live_postgres) -> None:
    """The zero migration 266 measured, re-measured instead of quoted.

    266's header says the tables the five frozen claims create are named in NONE
    of `org_purge`'s statements. It wrote that as a fraction of a statement count
    -- and the count grew with the schema while the file stayed frozen by
    checksum, so the sentence aged into something no longer reproducible.

    What has to be true is the ZERO, and a zero is derivable. This runs the same
    planner production runs, against the live catalog, and asserts it. Nobody has
    to keep a total in a comment for the claim to stay honest.
    """
    from core.org_purge import plan_purge  # noqa: PLC0415 -- lazy, pg-gated

    plan = plan_purge(live_postgres, _NO_SUCH_ORG)
    # Not vacuous: an empty or collapsed plan names nothing, which would satisfy
    # the assertion below while proving the opposite of what it claims.
    assert len({op.table for op in plan}) > 100, len(plan)

    planned = {op.table.split(".", 1)[-1] for op in plan}
    named: dict[str, list[str]] = {}
    for name in FROZEN_CLAIMS:
        created = _created_tables((MIGRATIONS / name).read_text(encoding="utf-8"))
        hit = sorted(table for table in created if table in planned)
        if hit:
            named[name] = hit

    assert not named, (
        "a table created by a frozen claim is now NAMED by `org_purge`'s own "
        f"plan: {named}\n\n"
        "That is not a regression in the purge -- it is the frozen claim coming "
        "true. Migration 266 carries the canonical denial for these tables on "
        "their behalf; a table the graph can now see no longer needs it, and "
        "leaving the denial in place would send the next reader to the wrong "
        "mechanism. Write the correction in a FORWARD migration and drop the "
        "entry from FROZEN_CLAIMS."
    )


def test_every_frozen_claim_still_exists_and_still_names_org_purge() -> None:
    """The allowlist is inventory, not silence -- and it must not outlive its entries.

    No database: this only asks that each named file is still there and still
    contains the sentence the entry describes. An entry whose file was renamed, or
    whose sentence was rewritten, is an entry nobody is reading any more, and a
    stale exemption is how a ratchet quietly stops ratcheting.
    """
    for name, reason in FROZEN_CLAIMS.items():
        path = MIGRATIONS / name
        assert path.exists(), f"{name} no longer exists; drop its entry from FROZEN_CLAIMS"
        assert "org_purge" in path.read_text(encoding="utf-8", errors="replace"), (
            f"{name} no longer names org_purge; drop its entry from FROZEN_CLAIMS"
        )
        assert reason.strip(), name
    # It may never be used to wave through a migration that can still be edited.
    # `apply_migrations` refuses an edit to an APPLIED migration (`checksum drift:
    # applied migration changed`), and that -- not convenience -- is the only
    # thing this list stands on.
    assert max(int(name[:3]) for name in FROZEN_CLAIMS) < 200, sorted(FROZEN_CLAIMS)
