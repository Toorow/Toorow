"""Story 37.9 on a REAL Postgres: one geographic authority, and a used-by guard with input.

TWO DEFECTS ARE PROVEN CLOSED HERE, and both were silent.

1. TAX READ A SECOND GEOGRAPHIC MAPPING. `capabilities/country.md` is incomplete when
   "Analyze, Context Hub or Tax uses a second geographic mapping". Analyze read the
   PUBLISHED hierarchy version through `country_registry.load_projection`; the Tax
   cascade read `app.project_preferences.geographic_mode` / `local_markets` -- the
   columns `country_registry.py` says it "replaces outright", and which the Country
   capability confirmation never writes back. A Project governed through the ratified
   capability therefore showed the cascade an EMPTY posture and had every spend row
   reported unresolvable while its geography was fully published.

   Migration 269's `app.country_market_projection_v` is the one authority both engines
   read. This file proves the VIEW and `load_projection` return the same rows, on the
   same published version, against the real schema -- not against a fixture that agrees
   with itself.

2. THE USED-BY GUARD HAD NOTHING TO REFUSE. `core.market_governance` could already
   block a market change that would re-mean a bound figure, and
   `register_market_binding` had NO production caller: `fetch_used_by` truthfully
   returned an empty list, so every market change was authorized. A guard whose input
   nobody writes reports safety it never checked.

   Publishing a Tax & Fee ladder now DECLARES the markets its rules pin
   (`tax_fee_rule_set.ladder_governed_dependencies`, called by
   `governance_rule_sets.publish_version`). This file proves the declaration reaches
   the store, that a supersession releases the version it replaced, and that the
   Country workspace refusal fires on the real reference.

WHY POSTGRES AND NOT A DOUBLE. The view is SQL, the used-by store is a table with an
ON CONFLICT key and a trigger-frozen version, and the release is an UPDATE with four
predicates. A mock of any of that would prove the test author's beliefs.
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

ORG = "org_test_fixture"


def _new_project(conn) -> str:
    project_id = f"proj_c19_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, 'Country authority', %s, 'active', 'pytest', %s)
            """,
            (project_id, f"c19-{uuid.uuid4().hex[:12]}", ORG),
        )
    return project_id


def _publish_france(conn, project_id: str) -> dict[str, object]:
    """Materialize and PUBLISH the France preset. Returns the identities under test.

    Built through the real services rather than by INSERT: the node ids are minted, the
    version is frozen by trigger and the registry pointer moves in one statement, and a
    hand-written row set would skip every one of those.
    """
    from core import country_registry as registry_service
    from core import master_data

    vocabulary = registry_service.import_country_vocabulary(
        conn, actor="pytest", source_version="ISO-3166-1:2026-01", effective_date=date(2026, 1, 1)
    )
    presets = registry_service.seed_country_presets(conn)
    france = next(item for item in presets if "france" in str(item["preset_key"]))

    registry = registry_service.ensure_country_registry(
        conn, org_id=ORG, project_id=project_id, actor="pytest"
    )
    draft = registry_service.materialize_preset(
        conn,
        org_id=ORG,
        project_id=project_id,
        registry_id=str(registry["id"]),
        preset_version_id=str(france["id"]),
        actor="pytest",
        vocabulary_version_id=str(vocabulary["id"]),
    )
    published = master_data.publish_version(
        conn, project_id=project_id, version_id=str(draft["id"]), actor="pytest"
    )
    return {
        "registry_id": str(registry["id"]),
        "draft_id": str(draft["id"]),
        "version_id": str(published["id"]),
    }


def _view_rows(conn, project_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT country_code, market_id, market_label, market_kind,
                   hierarchy_version_id
            FROM app.country_market_projection_v
            WHERE project_id = %s
            ORDER BY country_code
            """,
            (project_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# 1. One geographic authority.
# ---------------------------------------------------------------------------


def test_the_view_and_load_projection_return_the_same_published_meaning(live_postgres):
    """The two engines' inputs must be one reading, not two agreeing readings."""

    from core.country_registry import MARKET, load_projection

    project_id = _new_project(live_postgres)
    identities = _publish_france(live_postgres, project_id)

    projection = load_projection(live_postgres, project_id=project_id)
    assert projection is not None
    assert projection.hierarchy_version_id == identities["version_id"]

    rows = _view_rows(live_postgres, project_id)
    assert rows, "a published France hierarchy must project at least one assigned country"

    # The view is `market_of_value` restricted to TRACKED markets. Compared as sets so a
    # difference names the country, not an ordering.
    tracked_in_python = {
        code
        for code, market_id in projection.market_of_value.items()
        if projection.kinds.get(str(market_id)) == MARKET
    }
    assert {row[0] for row in rows} == tracked_in_python

    for country_code, market_id, market_label, market_kind, version_id in rows:
        assert projection.market_of_value[country_code] == market_id
        assert market_kind == MARKET
        # The label comes from the VERSION payload, so a rename after publication
        # cannot retroactively change what the published version meant.
        assert market_label == projection.labels.get(market_id)
        assert version_id == identities["version_id"]

    live_postgres.rollback()


def test_an_unpublished_project_projects_nothing_rather_than_global(live_postgres):
    """`load_projection` returns None here and forbids reading it as Global.

    The view must agree: NO ROW, which the cascade reads as "cannot decide". A row set
    that defaulted to anything would be the second mapping arriving by another door.
    """

    from core.country_registry import ensure_country_registry, load_projection

    project_id = _new_project(live_postgres)
    # A registry with no published version is the state a Project sits in between
    # enabling Country and confirming its first model, and it is NOT an error.
    ensure_country_registry(live_postgres, org_id=ORG, project_id=project_id, actor="pytest")

    assert load_projection(live_postgres, project_id=project_id) is None
    assert _view_rows(live_postgres, project_id) == []

    live_postgres.rollback()


def test_a_draft_hierarchy_is_invisible_until_it_is_published(live_postgres):
    """A draft is a proposal. The cascade must never compose money from one."""

    from core import country_registry as registry_service
    from core import master_data

    project_id = _new_project(live_postgres)
    vocabulary = registry_service.import_country_vocabulary(
        conn=live_postgres,
        actor="pytest",
        source_version="ISO-3166-1:2026-01",
        effective_date=date(2026, 1, 1),
    )
    presets = registry_service.seed_country_presets(live_postgres)
    france = next(item for item in presets if "france" in str(item["preset_key"]))
    registry = registry_service.ensure_country_registry(
        live_postgres, org_id=ORG, project_id=project_id, actor="pytest"
    )
    draft = registry_service.materialize_preset(
        live_postgres,
        org_id=ORG,
        project_id=project_id,
        registry_id=str(registry["id"]),
        preset_version_id=str(france["id"]),
        actor="pytest",
        vocabulary_version_id=str(vocabulary["id"]),
    )

    assert _view_rows(live_postgres, project_id) == []

    master_data.publish_version(
        live_postgres, project_id=project_id, version_id=str(draft["id"]), actor="pytest"
    )
    assert _view_rows(live_postgres, project_id), "publishing must make the meaning readable"

    live_postgres.rollback()


# ---------------------------------------------------------------------------
# 2. A used-by guard with real input.
# ---------------------------------------------------------------------------


def _money_policy_version(conn, project_id: str) -> dict[str, str]:
    """The pin the Tax ladder profile REQUIRES, built rather than fabricated.

    A ladder that does not pin a Money Policy has no defined arithmetic, so
    `draft_version` refuses without it. Fabricating an id would prove the wiring works
    against data the product cannot produce.
    """
    from core.currency_vocabulary import import_currency_vocabulary
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version
    from core.money_policy import PROFILE_MONEY

    currency = import_currency_vocabulary(
        conn, actor="pytest", source_version="ISO-4217:2026-01", effective_date="2026-01-01"
    )
    head = ensure_rule_set(
        conn,
        org_id=ORG,
        project_id=project_id,
        family="money_policy",
        name="project_money_policy",
        label="Money policy",
        actor="pytest",
    )
    draft = draft_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        profile=PROFILE_MONEY,
        label="v1",
        payload={
            "reporting_currency": "EUR",
            "rounding": "half_even",
            "max_staleness_days": 7,
            "rate_source_priority": ["ecb"],
        },
        requires=[
            {
                "kind": "currency_vocabulary_version",
                "object_id": str(currency["vocabulary_key"]),
                "version_id": str(currency["id"]),
            }
        ],
        actor="pytest",
    )
    published = publish_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        version_id=str(draft["id"]),
        actor="pytest",
    )
    return {"object_id": str(head["id"]), "version_id": str(published["id"])}


def _ladder_rule(*, jurisdiction: dict | None = None, conditions: dict | None = None) -> dict:
    rule = {
        "rule_key": "vat_standard",
        "label": "VAT",
        "scope_kind": "project",
        "scope_ref": None,
        "category": "SALES_TAX",
        "form": "PERCENTAGE",
        "rate": "0.20",
        "base_target": "NET_MEDIA",
        "cascade_phase": 5,
        "sequence_order": 10,
        "effective_from": "2026-01-01",
        "authority_kind": "statutory_reference",
        "source_evidence": {
            "issuer": "Example tax authority",
            "reference": "Example VAT code, article 1",
            "reference_version": "2026-01",
            "authoritative_url": "https://example.com/vat",
        },
    }
    if jurisdiction is not None:
        rule["jurisdiction"] = jurisdiction
        # A geography-dependent rule must declare both postures: silence is not one.
        rule["rest_of_world_posture"] = "exclude"
        rule["unknown_posture"] = "exclude"
    if conditions is not None:
        rule["conditions"] = conditions
        rule.setdefault("rest_of_world_posture", "exclude")
        rule.setdefault("unknown_posture", "exclude")
    return rule


def _publish_ladder(conn, project_id: str, rules: list[dict], money: dict[str, str]) -> str:
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version
    from core.tax_fee_rule_set import FAMILY_TAX_FEE, LADDER_NAME, PROFILE_TAX_FEE

    head = ensure_rule_set(
        conn,
        org_id=ORG,
        project_id=project_id,
        family=FAMILY_TAX_FEE,
        name=LADDER_NAME,
        label="Tax and fee ladder",
        actor="pytest",
    )
    draft = draft_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        profile=PROFILE_TAX_FEE,
        label="ladder",
        payload={"rounding": "half_even", "default_money_basis": "native_source"},
        ordered_rules=rules,
        requires=[
            {
                "kind": "money_policy_version",
                "object_id": money["object_id"],
                "version_id": money["version_id"],
            }
        ],
        actor="pytest",
    )
    published = publish_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        version_id=str(draft["id"]),
        actor="pytest",
    )
    return str(published["id"])


@pytest.fixture()
def governed_project(live_postgres):
    """A Project with a published France hierarchy and a published Money Policy."""

    project_id = _new_project(live_postgres)
    identities = _publish_france(live_postgres, project_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT market_id, market_label FROM app.country_market_projection_v
            WHERE project_id = %s LIMIT 1
            """,
            (project_id,),
        )
        market_id, market_label = cur.fetchone()
    return {
        "project_id": project_id,
        "market_id": str(market_id),
        "market_label": str(market_label),
        "hierarchy_version_id": identities["version_id"],
        "registry_id": identities["registry_id"],
        "money": _money_policy_version(live_postgres, project_id),
    }


def test_publishing_a_ladder_declares_the_markets_its_rules_pin(live_postgres, governed_project):
    """The used-by store stops being empty, which is what makes the guard mean anything."""

    from core.master_data import fetch_used_by
    from core.tax_fee_rule_set import PROFILE_TAX_FEE

    project_id = governed_project["project_id"]
    assert fetch_used_by(
        live_postgres,
        project_id=project_id,
        registry_id=governed_project["registry_id"],
    ) == (), "nothing depends on this market yet, and the guard must say so honestly"

    version_id = _publish_ladder(
        live_postgres,
        project_id,
        [
            _ladder_rule(
                jurisdiction={
                    "kind": "market",
                    "id": governed_project["market_id"],
                    "hierarchy_version_id": governed_project["hierarchy_version_id"],
                    "label": governed_project["market_label"],
                }
            )
        ],
        governed_project["money"],
    )

    references = fetch_used_by(
        live_postgres,
        project_id=project_id,
        registry_id=governed_project["registry_id"],
    )
    assert len(references) == 1
    reference = references[0]
    assert reference.node_id == governed_project["market_id"]
    assert reference.consumer_kind == PROFILE_TAX_FEE
    assert reference.consumer_version_id == version_id
    # The pin is what makes the dependency diffable: without it, republishing the
    # hierarchy changes what the rule matched with no version anywhere to compare.
    assert reference.hierarchy_version_id == governed_project["hierarchy_version_id"]

    live_postgres.rollback()


def test_a_condition_on_a_market_is_a_dependency_too(live_postgres, governed_project):
    """A rule that FIRES on a market depends on what that market contains.

    Only the jurisdiction pins a hierarchy version, so a condition-only dependency
    carries a null pin -- a weaker claim than a wrong version id, and an honest one.
    """

    from core.master_data import fetch_used_by

    project_id = governed_project["project_id"]
    _publish_ladder(
        live_postgres,
        project_id,
        [_ladder_rule(conditions={"market": [governed_project["market_id"]]})],
        governed_project["money"],
    )

    references = fetch_used_by(
        live_postgres,
        project_id=project_id,
        registry_id=governed_project["registry_id"],
    )
    assert [item.node_id for item in references] == [governed_project["market_id"]]
    assert references[0].hierarchy_version_id is None

    live_postgres.rollback()


def test_a_country_jurisdiction_declares_no_node_dependency(live_postgres, governed_project):
    """A country is a `child_value`, never a node: there is nothing to depend ON.

    Recording one would put a value in a table whose whole contract is governed
    identities. The membership change still reaches the operator -- moving a country out
    of a market is assessed against every dependent of that MARKET.
    """

    from core.master_data import fetch_used_by

    project_id = governed_project["project_id"]
    _publish_ladder(
        live_postgres,
        project_id,
        [
            _ladder_rule(
                jurisdiction={
                    "kind": "country",
                    "id": "FR",
                    "hierarchy_version_id": governed_project["hierarchy_version_id"],
                }
            )
        ],
        governed_project["money"],
    )

    assert fetch_used_by(
        live_postgres,
        project_id=project_id,
        registry_id=governed_project["registry_id"],
    ) == ()

    live_postgres.rollback()


def test_a_superseded_ladder_stops_depending_on_the_market(live_postgres, governed_project):
    """A released dependency is what keeps the operator's impact list TRUE.

    Without the release, a market that only a superseded ladder ever pinned would block
    changes forever -- and the list an operator is shown would name a version nothing
    composes from. The release is scoped to the superseded version precisely because the
    NEW version routinely pins the same market.
    """

    from core.master_data import fetch_used_by

    project_id = governed_project["project_id"]
    market_id = governed_project["market_id"]

    first = _publish_ladder(
        live_postgres,
        project_id,
        [
            _ladder_rule(
                jurisdiction={
                    "kind": "market",
                    "id": market_id,
                    "hierarchy_version_id": governed_project["hierarchy_version_id"],
                    "label": governed_project["market_label"],
                }
            )
        ],
        governed_project["money"],
    )
    second = _publish_ladder(
        live_postgres,
        project_id,
        [_ladder_rule()],  # jurisdiction-independent: depends on no market
        governed_project["money"],
    )
    assert first != second

    assert fetch_used_by(
        live_postgres,
        project_id=project_id,
        registry_id=governed_project["registry_id"],
    ) == (), "the superseded version's dependency must be released, not kept alive"

    live_postgres.rollback()


def test_a_republished_ladder_keeps_the_dependency_it_just_declared(
    live_postgres, governed_project
):
    """The ORDER of register-then-release is what this proves.

    Both versions name the same market. Releasing by consumer -- or releasing before
    registering -- would mark the dependency that was just declared as released, and the
    guard would then authorize a change to a market a LIVE ladder composes from.
    """

    from core.master_data import fetch_used_by

    project_id = governed_project["project_id"]
    market_id = governed_project["market_id"]
    jurisdiction = {
        "kind": "market",
        "id": market_id,
        "hierarchy_version_id": governed_project["hierarchy_version_id"],
        "label": governed_project["market_label"],
    }

    _publish_ladder(
        live_postgres,
        project_id,
        [_ladder_rule(jurisdiction=jurisdiction)],
        governed_project["money"],
    )
    second = _publish_ladder(
        live_postgres,
        project_id,
        [
            _ladder_rule(jurisdiction=jurisdiction),
            _ladder_rule(
                jurisdiction=jurisdiction,
            )
            | {"rule_key": "vat_reduced", "rate": "0.055", "sequence_order": 20},
        ],
        governed_project["money"],
    )

    references = fetch_used_by(
        live_postgres,
        project_id=project_id,
        registry_id=governed_project["registry_id"],
    )
    assert [item.node_id for item in references] == [market_id]
    assert references[0].consumer_version_id == second

    live_postgres.rollback()


def test_the_country_workspace_now_has_something_to_refuse(live_postgres, governed_project):
    """The end of the chain: a declared dependency makes the publish guard fire.

    `changed_bound_nodes` is the function the Country workspace publish path calls, and
    it is the one that returned an empty impact list for every change because nothing
    ever wrote a reference. Feeding it the reference a published ladder now declares is
    what turns the refusal from decoration into a decision.
    """

    from core.country_workspace import changed_bound_nodes
    from core.master_data import fetch_memberships, fetch_used_by

    project_id = governed_project["project_id"]
    market_id = governed_project["market_id"]

    _publish_ladder(
        live_postgres,
        project_id,
        [
            _ladder_rule(
                jurisdiction={
                    "kind": "market",
                    "id": market_id,
                    "hierarchy_version_id": governed_project["hierarchy_version_id"],
                    "label": governed_project["market_label"],
                }
            )
        ],
        governed_project["money"],
    )
    used_by = fetch_used_by(
        live_postgres,
        project_id=project_id,
        registry_id=governed_project["registry_id"],
    )
    assert used_by, "the rest of this test would pass vacuously without a reference"

    previous = fetch_memberships(
        live_postgres,
        project_id=project_id,
        version_id=governed_project["hierarchy_version_id"],
    )
    # The change under assessment: every country leaves the bound market. That is the
    # move that silently re-means an already-published invoice figure while the
    # jurisdiction id still resolves and looks healthy.
    emptied = tuple(edge for edge in previous if edge.parent_node_id != market_id)
    assert len(emptied) < len(previous), "the fixture must actually remove a membership"

    impacts = changed_bound_nodes(previous, emptied, used_by)
    assert impacts, "removing every member of a bound market must be an impact"
    assert any(str(item.get("node_id")) == market_id for item in impacts)

    # And an UNCHANGED hierarchy is not an impact: a guard that refused everything
    # would be worked around rather than read.
    assert changed_bound_nodes(previous, previous, used_by) == []

    live_postgres.rollback()


# ---------------------------------------------------------------------------
# 3. Context Hub: a geographic node is a knowledge-graph endpoint.
# ---------------------------------------------------------------------------


def test_a_governed_market_can_be_linked_to_knowledge_and_to_a_skill(live_postgres):
    """`country.md`: "Geographic nodes linkable to ... knowledge, Skills ...".

    Until migration 272 the `app.context_graph` node-type enum was
    `topic | procedure | schema_doc | target_field`, so a Market could not be an
    endpoint of ANY edge and `context_search`'s one-hop walk -- the corpus an agent
    actually reads -- could never reach one. Nothing was broken: the link had never
    existed.

    A topic is knowledge; a procedure is a Skill (Skill Pack format, Story 11.3). Both
    directions are exercised because both are how an operator would express it.
    """

    from core.context_store import create_graph_edge, create_procedure, create_topic

    project_id = _new_project(live_postgres)
    _publish_france(live_postgres, project_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT market_id FROM app.country_market_projection_v "
            "WHERE project_id = %s LIMIT 1",
            (project_id,),
        )
        market_id = str(cur.fetchone()[0])

    topic = create_topic(
        live_postgres,
        project_id=project_id,
        title="How we report France",
        body_md="Overseas territories roll up to the France market.",
        created_by="pytest",
    )
    procedure = create_procedure(
        live_postgres,
        project_id=project_id,
        frontmatter_yaml=(
            "name: reconcile_france_split\n"
            "description: Check the France market adds up.\n"
        ),
        body_md="1. Read the published hierarchy version.\n",
        created_by="pytest",
    )

    knowledge_edge = create_graph_edge(
        live_postgres,
        project_id=project_id,
        from_id=str(topic["id"]),
        from_type="topic",
        to_id=market_id,
        to_type="master_data_node",
        edge_type="explains",
        created_by="pytest",
    )
    assert knowledge_edge["to_id"] == market_id
    assert knowledge_edge["to_type"] == "master_data_node"

    skill_edge = create_graph_edge(
        live_postgres,
        project_id=project_id,
        from_id=market_id,
        from_type="master_data_node",
        to_id=str(procedure["id"]),
        to_type="procedure",
        edge_type="relates_to",
        created_by="pytest",
    )
    assert skill_edge["from_id"] == market_id

    live_postgres.rollback()


def test_an_archived_node_is_not_a_fresh_endpoint(live_postgres):
    """History outlives visibility, but a retired identity may not gain new edges.

    Nothing is deleted in Master Data -- an old Result pins a version and must stay
    reproducible -- so `archived_at` is the only thing that retires an identity, and it
    is what the existence check reads. Same rule migration 117 wrote for a soft-deleted
    target_field.
    """

    from core.context_store import create_graph_edge, create_topic

    project_id = _new_project(live_postgres)
    _publish_france(live_postgres, project_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT market_id FROM app.country_market_projection_v "
            "WHERE project_id = %s LIMIT 1",
            (project_id,),
        )
        market_id = str(cur.fetchone()[0])
        cur.execute(
            "UPDATE app.master_data_nodes SET archived_at = NOW() WHERE id = %s",
            (market_id,),
        )

    topic = create_topic(
        live_postgres,
        project_id=project_id,
        title="Stale note",
        body_md="Written before the market was retired.",
        created_by="pytest",
    )
    with pytest.raises(ValueError, match="is not archived, or restore it first"):
        create_graph_edge(
            live_postgres,
            project_id=project_id,
            from_id=str(topic["id"]),
            from_type="topic",
            to_id=market_id,
            to_type="master_data_node",
            edge_type="explains",
            created_by="pytest",
        )

    live_postgres.rollback()


def test_a_country_code_is_refused_as_a_graph_endpoint(live_postgres):
    """An ISO code is a `child_value`, never a node -- the used-by rule, restated.

    Accepting one would put a VALUE in a column pair whose whole contract is governed
    identities, and a country's meaning is reachable through its market anyway.
    """

    from core.context_store import create_graph_edge, create_topic

    project_id = _new_project(live_postgres)
    _publish_france(live_postgres, project_id)

    topic = create_topic(
        live_postgres,
        project_id=project_id,
        title="France",
        body_md="A note about one country.",
        created_by="pytest",
    )
    with pytest.raises(ValueError, match="is not archived, or restore it first"):
        create_graph_edge(
            live_postgres,
            project_id=project_id,
            from_id=str(topic["id"]),
            from_type="topic",
            to_id="FR",
            to_type="master_data_node",
            edge_type="explains",
            created_by="pytest",
        )

    live_postgres.rollback()


# ---------------------------------------------------------------------------
# 4. Every OTHER reader of the retired posture (Story 37.9, second pass).
# ---------------------------------------------------------------------------


def _enable_country(conn, project_id: str, state: str = "ready") -> None:
    """Set the Country capability state, respecting its OWN contract.

    `availability` is NOT NULL and `project_capabilities_check1` pins Country to
    `optional` -- it is the capability that must stay off by default, which is the
    whole reason `governed_posture` reads this row rather than assuming.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.project_capabilities
                (project_id, capability_key, availability, state)
            VALUES (%s, 'country', 'optional', %s)
            ON CONFLICT (project_id, capability_key) DO UPDATE SET state = EXCLUDED.state
            """,
            (project_id, state),
        )


def test_the_governed_posture_and_the_retired_one_really_disagreed(live_postgres):
    """The divergence, MEASURED, on one Project at one moment.

    Moving the Tax cascade off `project_preferences` left three live readers on it:
    the country-conformance DQ monitor, `flows.upsert_flow` and `report_mcp`. Each
    failed the same silent way, and this test is why: for a Project whose Country
    meaning is published and active, the two readers answer differently. The retired
    one says Global -- so the monitor returns early and never raises evidence, and a
    plan saved through the flow surface compiles Global geography.
    """

    from core.country_activation import governed_posture
    from core.geographic_reporting import fetch_project_geographic_posture

    project_id = _new_project(live_postgres)
    _publish_france(live_postgres, project_id)
    _enable_country(live_postgres, project_id)

    retired = fetch_project_geographic_posture(project_id, live_postgres)
    governed = governed_posture(live_postgres, project_id=project_id)

    assert retired.mode == "global", (
        "the preference row is empty for a governed Project -- if this ever becomes "
        "local_markets, something started writing a column the capability replaced"
    )
    assert governed.mode == "local_markets"
    assert governed.country_codes == ("FR",)
    assert [market.country_codes for market in governed.markets] == [("FR",)]

    live_postgres.rollback()


def test_a_disabled_capability_reads_as_global_and_that_is_the_answer(live_postgres):
    """Deactivation must return reports to consolidated behaviour (`country.md`).

    Global here is not a fallback for a failed read: it is what a Project with Country
    off MEANS. The published version is still there and still reproducible; nothing
    groups by it.
    """

    from core.country_activation import governed_posture

    project_id = _new_project(live_postgres)
    _publish_france(live_postgres, project_id)

    # No capability row at all: the state a Project sits in before enabling Country.
    assert governed_posture(live_postgres, project_id=project_id).mode == "global"

    _enable_country(live_postgres, project_id, state="disabled")
    assert governed_posture(live_postgres, project_id=project_id).mode == "global"

    _enable_country(live_postgres, project_id, state="ready")
    assert governed_posture(live_postgres, project_id=project_id).mode == "local_markets"

    live_postgres.rollback()


def test_a_degraded_capability_still_carries_its_published_meaning(live_postgres):
    """`degraded` is not `disabled`, and collapsing them would silently un-group.

    It means the evidence behind a published meaning has a gap the operator has been
    told about -- not that the meaning stopped applying. The same two states
    `reports._load_geography_projection` accepts, and deliberately not a third list.
    """

    from core.country_activation import GOVERNED_CAPABILITY_STATES, governed_posture

    assert GOVERNED_CAPABILITY_STATES == {"ready", "degraded"}

    project_id = _new_project(live_postgres)
    _publish_france(live_postgres, project_id)
    _enable_country(live_postgres, project_id, state="degraded")

    assert governed_posture(live_postgres, project_id=project_id).mode == "local_markets"

    live_postgres.rollback()


def test_an_active_capability_with_no_published_version_reads_as_global(live_postgres):
    """Enabled and nothing published yet is a real state, and it must not raise.

    `normalize_geographic_posture` refuses an empty `local_markets` posture outright,
    so inventing one here would turn a legitimate mid-onboarding Project into an
    exception thrown from a DQ monitor.
    """

    from core.country_activation import governed_posture
    from core.country_registry import ensure_country_registry

    project_id = _new_project(live_postgres)
    ensure_country_registry(live_postgres, org_id=ORG, project_id=project_id, actor="pytest")
    _enable_country(live_postgres, project_id)

    assert governed_posture(live_postgres, project_id=project_id).mode == "global"

    live_postgres.rollback()


def test_the_governed_posture_excludes_the_rest_of_world_catch_all(live_postgres):
    """The tracked set must match the Tax cascade's, member for member.

    Both engines drop Rest of World from the tracked set. If this reader kept it, the
    single-country inference the cascade draws and the one a DQ monitor draws would
    disagree on the same Project -- two answers to "which countries does this Project
    track", which is the whole defect Story 37.9 exists to remove.
    """

    from core.country_activation import governed_posture
    from core.country_registry import MARKET, load_projection

    project_id = _new_project(live_postgres)
    _publish_france(live_postgres, project_id)
    _enable_country(live_postgres, project_id)

    projection = load_projection(live_postgres, project_id=project_id)
    tracked = {
        code
        for code, market_id in projection.market_of_value.items()
        if projection.kinds.get(str(market_id)) == MARKET
    }
    posture = governed_posture(live_postgres, project_id=project_id)

    assert set(posture.country_codes) == tracked
    assert projection.rest_of_world_id is not None, (
        "the France preset must mint a Rest of World node, or this test proves nothing"
    )
    assert projection.rest_of_world_id not in {market.id for market in posture.markets}

    live_postgres.rollback()
