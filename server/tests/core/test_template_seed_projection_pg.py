"""Story 72.4 -- the projection of the shipped catalogue, against a real PostgreSQL.

WHY THESE CANNOT BE UNIT TESTS. What is proved here is that a projection run
twice writes once, that a version already stored is never rewritten, and that a
head the catalogue no longer declares survives being reported. Every one of those
properties is a trigger, a CHECK or a composite foreign key of migration 333. A
mocked cursor agrees with all of them cheerfully, which is exactly how an
additive projection becomes a comment.

THE RULE BEING PROVED. `docs/product-architecture/visualization-and-rendering.md`,
§ *Amendment, 2026-08-31*: the templates shipped with the product are code, and
the table is their idempotent, additive projection. A version already present is
left exactly as it is, because a Report may already pin it.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from core import visualization_template_seeds as catalogue
from core.chart_template_store import append_chart_template_version, archive_chart_template
from core.visualization_specs import VisualizationSpecRefused
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "register_visualization_template_seeds.py"
)


def _load_projection():
    name = "register_visualization_template_seeds"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    #  Registered BEFORE execution: `@dataclass` resolves annotations through
    #  `sys.modules[cls.__module__]`, and a module absent from it raises.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


projection = _load_projection()


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


class Chain:
    """org -> one active Project and one archived one, so this file runs on an empty base."""

    def __init__(self, conn):
        self.conn = conn
        self.org_id = _uid("org")
        self.project_id = _uid("proj")
        self.archived_project_id = _uid("proj")

    def build(self) -> "Chain":
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 72.4 fixture', %s, 'active', 'test')",
                (self.org_id, self.org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 72.4 fixture', %s, 'test')",
                (self.project_id, self.org_id, self.project_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects "
                "  (id, org_id, name, slug, created_by, status, archived_at) "
                "VALUES (%s, %s, 'Story 72.4 archived', %s, 'test', 'archived', NOW())",
                (
                    self.archived_project_id,
                    self.org_id,
                    self.archived_project_id.replace("_", "-"),
                ),
            )
        return self

    # -- driving the projection, scoped to this fixture's Project ----------
    #
    # `inspect` reads EVERY active Project of the database, which is what the
    # deploy step must do. The assertions below scope to this fixture so a
    # database that already holds Projects (the `default` seed of migration 018,
    # another session's fixture) neither fails them nor is written to.

    def inspect(self):
        findings, plans, _ = projection.inspect(self.conn)
        mine = [f for f in findings if f.project_id == self.project_id]
        my_plans = [p for p in plans if p.project_id == self.project_id]
        return mine, my_plans

    def project(self) -> int:
        _, plans = self.inspect()
        return projection.apply(self.conn, plans)

    def rows(self, seed_id: str):
        head_id = catalogue.seed_head_id(seed_id, self.project_id)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, label, seed_origin, seed_module_name, seed_template_id, "
                "       current_version_id, created_by "
                "FROM app.visualization_templates WHERE id = %s",
                (head_id,),
            )
            head = cur.fetchone()
            cur.execute(
                "SELECT version_number, id, family, spec_contract_version, schema_version, "
                "       content_hash, predecessor_version_id, proposed_by, document "
                "FROM app.visualization_template_versions "
                "WHERE template_id = %s ORDER BY version_number",
                (head_id,),
            )
            return head, cur.fetchall()


@pytest.fixture
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


def _states(findings) -> list[str]:
    return [finding.state for finding in findings]


# ---------------------------------------------------------------------------
# AC13, first direction: a seed declared in code and absent from the table.
# ---------------------------------------------------------------------------


def test_an_empty_project_is_missing_every_declared_seed(chain):
    findings, plans = chain.inspect()
    assert set(_states(findings)) == {projection.MISSING}
    assert {f.seed_id for f in findings} == {s.seed_id for s in catalogue.PLATFORM_SEEDS}
    assert len(plans) == len(catalogue.PLATFORM_SEEDS)


def test_the_projection_writes_the_declared_document_and_nothing_else(chain):
    written = chain.project()
    assert written == len(catalogue.PLATFORM_SEEDS)

    for seed in catalogue.PLATFORM_SEEDS:
        head, versions = chain.rows(seed.seed_id)
        assert head is not None, f"{seed.seed_id} produced no head"
        (_, label, origin, module_name, template_ref, current, created_by) = head
        assert label == seed.label
        assert origin == "platform_seed"
        #  `ck_visualization_templates_seed_pair` reserves the seed coordinates
        #  for `connector_seed`; a platform seed carries neither.
        assert module_name is None and template_ref is None
        assert created_by == catalogue.PLATFORM_SEED_AUTHOR

        assert len(versions) == 1
        (number, version_id, family, contract, schema, content_hash, pred, proposed, document) = (
            versions[0]
        )
        assert number == 1
        assert pred is None
        assert current == version_id, "the head did not advance onto the version it gained"
        assert family == seed.family
        assert contract == "chart-template.v1"
        assert schema == 1
        assert content_hash == seed.content_hash
        assert proposed == catalogue.PLATFORM_SEED_PROPOSED_BY
        stored = document if isinstance(document, dict) else json.loads(document)
        assert stored == seed.document
        #  What AD-2 closes never crosses.
        assert "widget_uri" not in json.dumps(stored)
        assert "bindings" not in stored


def test_the_projection_is_replayable_on_itself(chain):
    """Idempotent and additive: the second run writes nothing and finds nothing."""
    assert chain.project() == len(catalogue.PLATFORM_SEEDS)

    findings, plans = chain.inspect()
    assert plans == []
    assert [f for f in findings if f.state in (projection.MISSING, projection.UNDECLARED)] == []

    assert chain.project() == 0
    for seed in catalogue.PLATFORM_SEEDS:
        _, versions = chain.rows(seed.seed_id)
        assert len(versions) == 1, f"{seed.seed_id} gained a second version for nothing"


def test_an_archived_project_receives_nothing(chain):
    chain.project()
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.visualization_templates WHERE project_id = %s",
            (chain.archived_project_id,),
        )
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# A projected version is a pin a Report can actually resolve (72.1's key).
# ---------------------------------------------------------------------------


def test_a_projected_version_is_a_pin_a_report_can_resolve(chain):
    chain.project()
    seed = catalogue.PLATFORM_SEEDS[0]
    _, versions = chain.rows(seed.seed_id)
    version_id = versions[0][1]
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, project_id FROM app.presentation_version_registry "
            "WHERE presentation_kind = 'visualization_template_version' "
            "  AND presentation_version_id = %s",
            (version_id,),
        )
        row = cur.fetchone()
    assert row == (chain.org_id, chain.project_id), (
        "the projected version was not registered, so a Report pinning it would be refused"
    )


def test_a_projected_version_cannot_be_rewritten(chain):
    """The projection is additive because the row is insert-once, not by politeness."""
    chain.project()
    seed = catalogue.PLATFORM_SEEDS[0]
    _, versions = chain.rows(seed.seed_id)
    version_id = versions[0][1]
    with pytest.raises(Exception) as raised:
        with chain.conn.cursor() as cur:
            cur.execute(
                "UPDATE app.visualization_template_versions SET content_hash = %s WHERE id = %s",
                ("f" * 64, version_id),
            )
    assert "immutable" in str(raised.value).lower() or "evidence" in str(raised.value).lower()


# ---------------------------------------------------------------------------
# Mutations. A document altered in the table, and a seed removed from the code.
# ---------------------------------------------------------------------------


def test_a_stored_document_the_code_no_longer_declares_is_history_and_survives(chain):
    """The renderer-ledger precedent, on this object.

    A version whose document is not today's is NOT rewritten and NOT deleted: a
    Report may pin it, and the row is insert-once anyway. It is printed as
    HISTORY, and the declared document arrives as a NEW version naming it as its
    predecessor.
    """
    seed = catalogue.PLATFORM_SEEDS[0]
    head_id = catalogue.seed_head_id(seed.seed_id, chain.project_id)
    stale = dict(seed.document)
    stale["answers_question"] = "An earlier wording of the same question."
    from core.visualization_templates import validate_template_document

    validated = validate_template_document(stale)
    stale_version_id = catalogue.seed_version_id(seed.seed_id, chain.project_id, 1)

    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.visualization_templates "
            "  (id, org_id, project_id, label, seed_origin, created_by) "
            "VALUES (%s, %s, %s, %s, 'platform_seed', 'platform')",
            (head_id, chain.org_id, chain.project_id, seed.label),
        )
        cur.execute(
            """
            INSERT INTO app.visualization_template_versions
                (id, template_id, org_id, project_id, version_number, family,
                 spec_contract_version, schema_version, document, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, 1, %s::jsonb, %s, 'platform')
            """,
            (
                stale_version_id,
                head_id,
                chain.org_id,
                chain.project_id,
                validated.family,
                validated.document["spec_contract_version"],
                json.dumps(validated.document),
                validated.content_hash,
            ),
        )
        cur.execute(
            "UPDATE app.visualization_templates SET current_version_id = %s WHERE id = %s",
            (stale_version_id, head_id),
        )

    findings, _ = chain.inspect()
    mine = [f for f in findings if f.seed_id == seed.seed_id]
    assert projection.HISTORY in _states(mine)
    assert projection.MISSING in _states(mine)

    chain.project()
    _, versions = chain.rows(seed.seed_id)
    assert [row[0] for row in versions] == [1, 2]
    assert versions[0][1] == stale_version_id
    assert versions[0][5] == validated.content_hash, "the earlier version was rewritten"
    assert versions[1][5] == seed.content_hash
    assert versions[1][6] == stale_version_id, "the appended version names no predecessor"

    #  And the second run is quiet again except for the history it keeps saying.
    findings, plans = chain.inspect()
    assert plans == []
    assert [f.state for f in findings if f.seed_id == seed.seed_id] == [projection.HISTORY]


def test_a_head_the_catalogue_no_longer_declares_is_undeclared_and_is_never_deleted(chain):
    """A seed removed from the code. `--check` says it; nothing repairs it by deleting."""
    stray_id = f"vtpl_pseed_retired_card__{chain.project_id}"
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.visualization_templates "
            "  (id, org_id, project_id, label, seed_origin, created_by) "
            "VALUES (%s, %s, %s, 'A template this build no longer declares', "
            "        'platform_seed', 'platform')",
            (stray_id, chain.org_id, chain.project_id),
        )

    findings, _ = chain.inspect()
    undeclared = [f for f in findings if f.state == projection.UNDECLARED]
    assert [f.seed_id for f in undeclared] == [stray_id]

    chain.project()
    with chain.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.visualization_templates WHERE id = %s", (stray_id,))
        assert cur.fetchone()[0] == 1, "an undeclared head was deleted rather than reported"


def test_archiving_a_project_does_not_make_its_seeds_undeclared(chain):
    """Measured false positive, closed.

    Deriving the declared set from the ACTIVE Projects turned every head of an
    archived Project into an UNDECLARED the moment it was archived -- 48 lines
    from eight archived Projects on a disposable database -- and a `--check` that
    refuses a deployment because somebody archived a Project is a gate that gets
    worked around. An archived Project's rows are evidence, not a defect.
    """
    chain.project()
    with chain.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.projects SET status = 'archived', archived_at = NOW() WHERE id = %s",
            (chain.project_id,),
        )

    findings, plans, _ = projection.inspect(chain.conn)
    mine = [f for f in findings if f.project_id == chain.project_id]
    assert [f for f in mine if f.state == projection.UNDECLARED] == []
    #  And nothing is written into it either: the projection reaches active
    #  Projects only.
    assert [p for p in plans if p.project_id == chain.project_id] == []


# ---------------------------------------------------------------------------
# AI-353. A head a person has taken responsibility for -- archived, or edited and
# thereby owned -- is NAMED and is never written to.
# ---------------------------------------------------------------------------


def _restate_catalogue(monkeypatch, seed):
    """The shipped catalogue, with ONE seed's document (and hash) moved on.

    The day a shipped Chart Template is reworded is the day the projection has a
    version to append, and it is the only day these two states can be told from
    an ordinary quiet run. Restated through the 72.2 validator so what is declared
    is a legal document, exactly as the catalogue derives it.
    """
    from core.visualization_templates import validate_template_document

    later = dict(seed.document)
    later["answers_question"] = "A later wording of the very same question."
    validated = validate_template_document(later)
    restated = dataclasses.replace(
        seed, document=validated.document, content_hash=validated.content_hash
    )
    monkeypatch.setattr(
        catalogue,
        "PLATFORM_SEEDS",
        tuple(restated if s.seed_id == seed.seed_id else s for s in catalogue.PLATFORM_SEEDS),
    )
    monkeypatch.setattr(
        catalogue, "SEEDS_BY_ID", {**catalogue.SEEDS_BY_ID, seed.seed_id: restated}
    )
    return restated


def _blocking(findings) -> list:
    """What `--check` would refuse a deployment for, by the script's own rule."""
    return [f for f in findings if f.state in projection.BLOCKING]


def test_an_archived_seed_is_named_and_never_completed(chain, monkeypatch):
    """AI-353. The catalogue moves on; a seed retired in this Project stays retired.

    AI-351 settled the rule -- "a re-projection does not resurrect what a person
    retired here" -- and it held only while the catalogue never changed a
    `content_hash`. The day it does, the head is stale AND archived, and what the
    projection must NOT do is complete it: `append_chart_template_version` refuses
    an archived head by name, and a projection that appended one in raw SQL would
    be doing behind the service what the service forbids.
    """
    chain.project()
    seed = catalogue.PLATFORM_SEEDS[0]
    head_id = catalogue.seed_head_id(seed.seed_id, chain.project_id)
    archive_chart_template(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id, template_id=head_id
    )

    restated = _restate_catalogue(monkeypatch, seed)
    findings, plans = chain.inspect()
    mine = [f for f in findings if f.seed_id == seed.seed_id]

    assert [f.state for f in mine] == [projection.ARCHIVED_STALE]
    #  Named in the words of the person who archived it, with the gesture that
    #  would bring the shipped version in.
    assert "archived this seed" in mine[0].detail
    assert "restore it here" in mine[0].detail
    assert plans == [], "a plan was made for a head this Project archived"

    assert chain.project() == 0
    _, versions = chain.rows(seed.seed_id)
    assert [row[0] for row in versions] == [1], "the archived head gained a version"
    assert versions[0][5] == seed.content_hash
    assert restated.content_hash not in {row[5] for row in versions}

    #  And an operator's own choice never refuses a deployment.
    assert _blocking(findings) == []


def test_appending_to_an_archived_head_is_refused_by_name(chain):
    """The guard, not the filter. A plan built by a hand meets the service refusal.

    `inspect` plans nothing for an archived head, so this proves the OTHER half:
    the write path itself asks `chart_template_store` and is refused in the same
    words `append_chart_template_version` uses -- which is what makes raw SQL in
    `apply` a defect rather than a style.
    """
    chain.project()
    seed = catalogue.PLATFORM_SEEDS[0]
    head_id = catalogue.seed_head_id(seed.seed_id, chain.project_id)
    archive_chart_template(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id, template_id=head_id
    )

    forced = projection.Plan(
        org_id=chain.org_id,
        project_id=chain.project_id,
        seed_id=seed.seed_id,
        head_id=head_id,
        head_exists=True,
        version_number=2,
        predecessor_version_id=catalogue.seed_version_id(seed.seed_id, chain.project_id, 1),
    )
    with pytest.raises(VisualizationSpecRefused) as refused:
        projection.apply(chain.conn, [forced])
    body = refused.value.as_dict()
    assert body["code"] == "chart_template_archived"
    assert "restore" in " ".join(r["remedy"] or "" for r in body["refusals"]).lower()

    _, versions = chain.rows(seed.seed_id)
    assert [row[0] for row in versions] == [1]


def test_a_seed_this_project_edited_is_named_and_never_re_seeded(chain, monkeypatch):
    """AC15's other face. An edited seed head is project-owned, and stays that way.

    Measured before this held (2026-09-01, disposable base): the head was invisible
    to the projection -- `seed_origin` had become `project` -- so it was reported
    MISSING, `--check` exited 1, and `apply` died on migration 333's own trigger,
    `a stable head advances only to a NEWER version (2 -> 1)`.
    """
    chain.project()
    seed = catalogue.PLATFORM_SEEDS[0]
    head_id = catalogue.seed_head_id(seed.seed_id, chain.project_id)
    edited = dict(seed.document)
    edited["answers_question"] = "What this Project asks instead."
    append_chart_template_version(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        template_id=head_id,
        document=edited,
        actor="owner@example.com",
    )

    #  An edit alone says nothing: the shipped document is still version 1 of this
    #  head, so there is nothing to report and nothing to write.
    findings, plans = chain.inspect()
    assert [f for f in findings if f.seed_id == seed.seed_id] == []
    assert plans == []
    assert chain.project() == 0

    restated = _restate_catalogue(monkeypatch, seed)
    findings, plans = chain.inspect()
    mine = [f for f in findings if f.seed_id == seed.seed_id]
    assert [f.state for f in mine] == [projection.PROJECT_OWNED]
    assert plans == []
    assert _blocking(findings) == []

    assert chain.project() == 0
    _, versions = chain.rows(seed.seed_id)
    assert [row[0] for row in versions] == [1, 2]
    assert restated.content_hash not in {row[5] for row in versions}


def test_a_project_created_after_a_run_is_missing_and_the_next_run_closes_it(chain):
    """The named lag: idempotent and additive means running again is always safe."""
    chain.project()
    latecomer = _uid("proj")
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, 'Story 72.4 latecomer', %s, 'test')",
            (latecomer, chain.org_id, latecomer.replace("_", "-")),
        )

    findings, plans, _ = projection.inspect(chain.conn)
    late = [f for f in findings if f.project_id == latecomer]
    assert set(_states(late)) == {projection.MISSING}

    written = projection.apply(chain.conn, [p for p in plans if p.project_id == latecomer])
    assert written == len(catalogue.PLATFORM_SEEDS)
