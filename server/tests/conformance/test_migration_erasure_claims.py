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

WHAT THIS GUARD ASKS. For every migration that CREATES a table in `app.` and
names `org_purge` anywhere, at least one table it creates must be NAMED by
`plan_purge` in a statement of its own. A migration whose tables the plan never
names must instead carry the CANONICAL DENIAL, the phrase migration 235
established:

    NOT `core.org_purge`

That is a sentence, checked as a string, and it is on purpose: the point of the
class is what the header SAYS, so what the guard reads is what the header says.

IT ASKS THE PLANNER, AND IT USED TO ASK A PROXY -- AI-365, 2026-09-06. What this
file asked until then was the STRUCTURAL version: *at least one table it creates
carries a foreign key `confdeltype IN ('a','r')`*. That is a proxy for "the
eraser reaches it", and a proxy can be satisfied by an edge that leads nowhere.
Migration 317 satisfied it by accident and its false claim went through:

    fk_context_relationships_current_version      -> context_relationship_versions   a
    fk_context_relationship_versions_predecessor  -> context_relationship_versions   a
    fk_context_relationship_versions_relation     -> context_relationships           c
    fk_context_relationships_project              -> app.projects                    c

Two graph-visible edges, both pointing INSIDE the pair the migration creates, and
nothing attaching that pair to the tenant tree. The traversal starts at
`app.organizations`, so it never arrived: measured on a cluster at migration 351,
`plan_purge` emitted 3430 statements over 209 tables and named NEITHER table.
The structural check said yes while the mechanism said no -- a green by
coincidence, on the RGPD path, in the one file whose job is to refuse exactly
that.

So the question is now put to the planner itself: is the table in `plan_purge`'s
own statements? That is the sentence the headers make, asked of the thing that
would have to be true. It cannot be satisfied by an edge that leads nowhere,
because the walk either arrives or it does not. Migration 352 flips both of
317's edges to RESTRICT, which is what makes 317's sentence true.

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

#: An org id that exists nowhere. `plan_purge` builds its statements from the
#: foreign-key graph and not from rows, so the plan is complete for any id, and
#: nothing this file touches can leave a trace.
_NO_SUCH_ORG = "org_EXAMPLE"

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

#: EXPOSED BY THE TIGHTENING OF 2026-09-06 (AI-365), and not repaired by it.
#: Inventory, never silence: each entry says what its header gets wrong, and
#: `test_every_plan_blind_claim_is_still_blind` re-measures the blindness on the
#: live catalog, so an entry cannot outlive the defect it describes.
#:
#: These are NOT frozen claims. `FROZEN_CLAIMS` holds five files that no edit can
#: repair; these two are repairable the way 317 was -- by a FORWARD migration
#: that gives the table a delete rule the walk can follow, or by one that carries
#: the canonical denial on their behalf, as 266 does for the five. Each needs its
#: own arbitration and its own migration number, which is why AI-365 names them
#: here instead of deciding for them: a session that repairs one deletes its
#: entry, and the test above turns red if it does not.
PLAN_BLIND_CLAIMS: dict[str, str] = {
    "242_a_transformation_rule_remembers_what_it_was.sql": (
        "ITS HEADER IS RIGHT AND ITS FORM IS OLD -- the same shape as the "
        "`107` entry above. It states the fact in its own words: « "
        "`core.org_purge.plan_purge` walks `confdeltype IN ('a','r')` only, so a "
        "CASCADE edge is absent from its plan and the foreign key is what makes "
        "the final `DELETE FROM app.organizations` reach these rows ». What it "
        "does not carry is the canonical DENIAL string, which is what this file "
        "reads. `cleanup_rule_versions` and `value_mapping_table_versions` are "
        "CASCADE to org, project and parent rule; their only graph-visible edge "
        "is the self-referencing `predecessor_version_id`, which attaches them "
        "to nothing -- 317's incident exactly. The repair is a forward migration "
        "carrying the canonical phrase for these two tables, not a schema change: "
        "flipping them to RESTRICT would make 242's own sentence false."
    ),
    "347_a_topic_names_its_semantic_views_and_the_paths_it_allows.sql": (
        "A FALSE CLAIM, the same one 317 made: « the foreign-key graph "
        "`core.org_purge` walks reaches this table through the Semantic View "
        "version as well as being cascaded from the topic ». "
        "`answerable_topic_view_bindings` does carry two NO ACTION edges "
        "(`fk_atvb_view_head`, `fk_atvb_view_version`), but measured 2026-09-06 "
        "neither `app.semantic_views` nor `app.semantic_view_versions` is itself "
        "named by the plan, so the walk never arrives and the binding is named "
        "in none of the eraser's statements. Its third edge, `fk_atvb_topic`, is "
        "CASCADE onto `app.answerable_topics`, which the plan DOES name -- so "
        "the smallest true repair is a forward migration flipping that one edge "
        "to RESTRICT, exactly as 352 did for 317."
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


def _existing_tables(conn) -> set[str]:
    """Every ordinary table of the `app` schema, by bare name.

    Existence is read from `pg_class` and not from `pg_constraint`, because a
    table with NO foreign key at all exists and is invisible to BOTH mechanisms
    -- which is the state migration 338 was written to close. Reading existence
    off the constraint catalogue would have silently excused exactly that table.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'app' AND c.relkind = 'r'"
        )
        return {str(row[0]) for row in cur.fetchall()}


def _planned_tables(conn) -> set[str]:
    """Every `app.` table `org_purge` NAMES in a statement of its OWN, bare name.

    The production planner, on the live catalog, for an org id that exists
    nowhere: the plan is derived from the schema and not from rows, so it is
    complete for any id and touches nothing.
    """
    from core.org_purge import plan_purge  # noqa: PLC0415 -- lazy, pg-gated

    return {op.table.split(".", 1)[-1] for op in plan_purge(conn, _NO_SUCH_ORG)}


def _offenders(conn) -> list[str]:
    """Every migration whose header claims `org_purge` and whose tables refuse it.

    The verdict is `plan_purge`'s own statement list, never a property of an
    individual foreign key: an edge the graph can SEE is not an edge the walk
    ARRIVES by, and a pair of tables pointing at each other satisfies the first
    while failing the second (AI-365, migration 317).
    """
    existing = _existing_tables(conn)
    planned = _planned_tables(conn)
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
        known = [table for table in tables if table in existing]
        if not known:
            # Every table it creates is gone from the schema, or the database is
            # behind this migration. Nothing measurable, so nothing asserted --
            # `test_the_guard_is_not_vacuous` is what stops that from swallowing
            # the whole corpus.
            continue
        if any(table in planned for table in known):
            continue
        offenders.append(path.name)
    return offenders


def test_a_migration_naming_org_purge_is_reached_by_org_purge(live_postgres) -> None:
    known_blind = set(FROZEN_CLAIMS) | set(PLAN_BLIND_CLAIMS)
    offenders = sorted(set(_offenders(live_postgres)) - known_blind)

    assert not offenders, (
        "migration(s) whose header names `core.org_purge` while `plan_purge` "
        f"names NONE of the tables they create: {offenders}\n\n"
        "`plan_purge` walks `confdeltype IN ('a','r')` only -- NO ACTION and "
        "RESTRICT -- from `app.organizations` down. A table reached only by ON "
        "DELETE CASCADE is erased by POSTGRES, from the parent statement "
        "`org_purge` does emit, and `org_purge` names it in none of its own.\n\n"
        "A GRAPH-VISIBLE FOREIGN KEY IS NOT ENOUGH AND NEVER WAS: this used to "
        "ask for one, and migration 317 satisfied it with two edges pointing at "
        "the other table of its own pair, attached to the tenant tree by "
        "nothing. What is asked is that the WALK ARRIVES.\n\n"
        "Two ways out, and only these two:\n"
        "  - give the table a delete rule the walk can follow -- RESTRICT or NO "
        "ACTION, on an edge whose PARENT the plan already names (migration 352 "
        "did this for 317); or\n"
        f"  - write the truth, in the words migration 235 fixed: `{DENIAL}` -- "
        "then say WHICH parent statement cascades down to it, and measure it.\n\n"
        "Do not repair this by deleting the sentence: an org-scoped table naming "
        "no parent at all is invisible to BOTH mechanisms, and that is the bug "
        "this comment exists to prevent."
    )


def test_the_guard_is_not_vacuous(live_postgres) -> None:
    """It really reads the catalog, and it really refuses a planted claim.

    Four assertions, because each covers a different way this file could pass on
    nothing: an empty catalog, a corpus it never opened, a rule that is only the
    old proxy under a new name, and a rule with no teeth.
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

    # 3. THE RULE IS NOT THE OLD PROXY UNDER A NEW NAME. `cleanup_rule_versions`
    #    (migration 242) carries a graph-visible foreign key -- its own
    #    `predecessor_version_id`, NO ACTION -- and the walk still never arrives,
    #    because that edge points at the table itself. The two questions really
    #    do give different answers on a real table of this schema, which is what
    #    317 exploited by accident.
    planned = _planned_tables(live_postgres)
    assert len(planned) > 100, len(planned)
    assert actions["cleanup_rule_versions"] & GRAPH_VISIBLE_DELETE_ACTIONS
    assert "cleanup_rule_versions" not in planned
    assert "master_data_aliases" in planned

    # 4. THE TEETH. Plant the claim in a COPY of the file that is right today --
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


def test_every_plan_blind_claim_is_still_blind(live_postgres) -> None:
    """The second inventory cannot outlive the defects it names either.

    Two things are asked of every entry, and both are re-measured rather than
    quoted: the file still exists and still names `org_purge`, and `plan_purge`
    still names NONE of the tables it creates. The day a forward migration gives
    one of them a delete rule the walk can follow -- or carries the canonical
    denial on its behalf -- this turns red and the entry has to go, which is the
    only thing that keeps an inventory from ageing into an exemption.
    """
    planned = _planned_tables(live_postgres)
    existing = _existing_tables(live_postgres)
    repaired: dict[str, list[str]] = {}

    for name, reason in PLAN_BLIND_CLAIMS.items():
        path = MIGRATIONS / name
        assert path.exists(), f"{name} no longer exists; drop its entry from PLAN_BLIND_CLAIMS"
        sql = path.read_text(encoding="utf-8", errors="replace")
        assert "org_purge" in sql, (
            f"{name} no longer names org_purge; drop its entry from PLAN_BLIND_CLAIMS"
        )
        assert DENIAL not in sql, (
            f"{name} now carries the canonical denial and needs no entry here"
        )
        assert reason.strip(), name
        created = _created_tables(sql)
        assert created & existing, (
            f"{name} creates no table this schema still has; drop its entry"
        )
        hit = sorted(table for table in created if table in planned)
        if hit:
            repaired[name] = hit

    assert not repaired, (
        "a table named by a PLAN_BLIND_CLAIMS entry is now NAMED by "
        f"`org_purge`'s own plan: {repaired}\n\n"
        "That is the repair landing, not a regression. Delete the entry: leaving "
        "it in place would keep a header the guard no longer needs to excuse, and "
        "would hide the next one that does."
    )

    # It is the OTHER list that is frozen. Everything here is repairable by a
    # forward migration, so an entry below the frozen ceiling would be an entry
    # in the wrong inventory.
    assert min(int(name[:3]) for name in PLAN_BLIND_CLAIMS) >= 200, sorted(PLAN_BLIND_CLAIMS)
    assert not set(PLAN_BLIND_CLAIMS) & set(FROZEN_CLAIMS), "an entry in both inventories"
