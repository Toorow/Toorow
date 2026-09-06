"""Story 75-5, replayed against a live PostgreSQL: a topic names its Semantic
Views and the join paths it allows.

WHY IT IS PG-GATED AND NOT MOCKED. Every claim this story makes is a REFUSAL by
the model or by the database -- an unknown relation, a relation borrowed from
another View version, a draft version, a foreign Project. A mocked cursor refuses
nothing, so an offline suite here would assert that Python raises where the
Project's ratified relation graph was never consulted.

THE TWO TRAPS `test_answerable_topics_pg.py` already paid for are encoded again:
a refused statement aborts the whole transaction in psycopg 3, so every refusal
runs inside `conn.transaction()` (a savepoint); and `live_postgres` rolls back on
teardown, so each test builds its own chain and depends on no other.

WHERE THE DRIFT GUARD LIVES. The equality of the two `PINNABLE_VIEW_STATUSES`
declarations is proved by
`tests/conformance/test_pinnable_view_statuses_do_not_drift.py` and not here: it
needs no database, and the module-level `importorskip` below made it silent on
every machine without the driver -- a guard that skips is not a guard.

WHAT THE LAST TEST IS FOR. `verified_query_pairs` is the payload the agent
surface serves through `metric_verified_queries`. Story 75-5 adds ONE sibling key
to it, and adds it ONLY for a topic that declares a View: the unbound case below
snapshots the exact key set a pair carried before this story, so the day someone
"helpfully" emits `views: []` for everyone, this file says so.
"""

from __future__ import annotations

import pytest
from ulid import ULID

psycopg = pytest.importorskip("psycopg")

from core import answerable_topics as topics  # noqa: E402

#: The host context the REST layer passes. A literal, for the reason
#: `test_answerable_topics_write_pg.py` states: an invented key made every write
#: route answer 500 for the life of epic 52.
_HOST_CONTEXT = {"host": "rest"}

#: The key set a verified-query pair carried BEFORE story 75-5. Written out
#: rather than derived, because "the payload did not change" is the acceptance
#: criterion and a derived expectation would move with the code it grades.
_PAIR_KEYS_BEFORE_75_5 = {
    "id",
    "question",
    "surface",
    "tags",
    "topic_key",
    "role",
    "position",
    "query_spec_id",
    "query_spec_version_id",
    "query_spec_version_number",
    "semantic_view_id",
    "semantic_view_version_id",
}


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _svv() -> str:
    """A Semantic View version id: `ck_semantic_view_versions_id` demands the shape."""
    return f"svv_{ULID()}"


def _sv() -> str:
    return f"sv_{ULID()}"


@pytest.fixture()
def project(live_postgres):
    org_id, project_id = _uid("org"), _uid("proj")
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "Story 75-5", org_id.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, "Story 75-5", project_id.lower(), "tester"),
        )
    return org_id, project_id


def _topic(conn, org_id: str, project_id: str, *, key: str = "pacing") -> str:
    """A topic head with its version 1, written directly -- this file grades bindings."""
    topic_id = _uid("atp")
    version_id = _uid("atv")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topics
                (id, org_id, project_id, topic_key, lifecycle_state,
                 base_template_id, origin, created_by)
            VALUES (%s, %s, %s, %s, 'active', 'kpi', 'project', 'tester')
            """,
            (topic_id, org_id, project_id, key),
        )
        cur.execute(
            """
            INSERT INTO app.answerable_topic_versions
                (id, answerable_topic_id, org_id, project_id, version_number, title,
                 answers_question, kind, fallback_rank, base_template_id,
                 content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, 'Pacing', 'Are we pacing to plan?', 'kpi', 0,
                    'kpi', %s, 'tester')
            """,
            (version_id, topic_id, org_id, project_id, "a" * 64),
        )
        # The head must POINT at its version: `_fetch_stored` LEFT JOINs on
        # `current_version_id`, so a head that names none carries no wording and
        # `verified_query_pairs` drops its pair -- which reads as an unreadable
        # store rather than as a fixture that forgot a column.
        cur.execute(
            "UPDATE app.answerable_topics SET current_version_id = %s WHERE id = %s",
            (version_id, topic_id),
        )
    return topic_id


def _view(
    conn,
    project_id: str,
    *,
    name: str = "spend",
    status: str = "published",
    relationships: tuple[tuple[str, str, str], ...] = (),
) -> tuple[str, str]:
    """A Semantic View + one version + its ratified relationships.

    `relationships` entries are `(name, from_dataset, to_dataset)`; the
    cardinality and fan-out policy are fixed here because what this file grades
    is which relations a path may NAME, not how the compiler weighs them.
    """
    view_id, version_id = _sv(), _svv()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by)
            VALUES (%s, %s, %s, %s, 'tester')
            """,
            (view_id, project_id, name, status),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, 'tester')
            """,
            (version_id, view_id, project_id, status, name, name.title(),
             "b" * 64, "c" * 64),
        )
        for ordinal, (rel_name, src, dst) in enumerate(relationships):
            cur.execute(
                """
                INSERT INTO app.semantic_view_version_relationships
                    (view_version_id, ordinal, name, from_dataset, to_dataset,
                     from_columns, to_columns, cardinality_type, fan_out_policy)
                VALUES (%s, %s, %s, %s, %s, ARRAY['id'], ARRAY['id'],
                        'many_to_one', 'forbid')
                """,
                (version_id, ordinal, rel_name, src, dst),
            )
    return view_id, version_id


# ---------------------------------------------------------------------------
# The declaration itself.
# ---------------------------------------------------------------------------


def test_a_binding_pins_the_exact_version_and_resolves_its_paths(live_postgres, project):
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    view_id, version_id = _view(
        conn, project_id, relationships=(("campaign_to_account", "campaigns", "accounts"),)
    )

    result = topics.bind_view(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="pacing",
        semantic_view_version_id=version_id,
        allowed_paths=[{"relation_ids": ["campaign_to_account"]}],
        note="pacing reads spend by account",
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )
    assert result["semantic_view_version_id"] == version_id
    assert result["semantic_view_id"] == view_id, "the head is derived, never supplied"
    assert result["topic_key"] == "pacing"

    resolved = topics.resolve_views(project_id, conn, topic_key="pacing")
    assert len(resolved) == 1
    binding = resolved[0]
    # THE PIN IS THE EXACT VERSION, not the head's current one.
    assert binding["view_version_id"] == version_id
    assert binding["stale"] is False
    assert binding["status"] == "published"
    path = binding["paths"][0]
    assert path["relation_ids"] == ["campaign_to_account"]
    assert path["resolved"] is True
    # Derived at READ time from the model -- none of these is stored.
    assert path["from"] == "campaigns"
    assert path["to"] == "accounts"
    assert path["cardinality"] == "many_to_one"
    assert path["fan_out_policy"] == "forbid"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.operations WHERE effective_org_id = %s "
            "AND command_type = 'answerable_topic.view_bound'",
            (org_id,),
        )
        assert cur.fetchone()[0] == 1, "no operation row: the write escaped the ledger"
        cur.execute(
            "SELECT action, outcome FROM app.audit_log WHERE effective_org_id = %s",
            (org_id,),
        )
        assert ("answerable_topic.view_bound", "succeeded") in cur.fetchall()


def test_an_unknown_relation_is_refused_by_name(live_postgres, project):
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(
        conn, project_id, relationships=(("campaign_to_account", "campaigns", "accounts"),)
    )

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_view(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="pacing",
            semantic_view_version_id=version_id,
            allowed_paths=[{"relation_ids": ["invented_join"]}],
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "unknown_relation"
    assert "invented_join" in exc.value.message, (
        "a refusal that does not NAME the relation tells the author nothing to fix"
    )

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.answerable_topic_view_bindings")
        assert cur.fetchone()[0] == 0, "the refusal must land before anything is stored"


def test_a_relation_of_another_view_is_refused_by_name(live_postgres, project):
    """A real, ratified relation borrowed from elsewhere is NOT an unknown one.

    Answering both with one code would hide the only fact the author can act on:
    the relation exists, and it does not touch the View this topic bound.
    """
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _bound_id, bound_version = _view(
        conn, project_id, name="spend",
        relationships=(("campaign_to_account", "campaigns", "accounts"),),
    )
    _view(
        conn, project_id, name="audience",
        relationships=(("account_to_market", "accounts", "markets"),),
    )

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_view(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="pacing",
            semantic_view_version_id=bound_version,
            allowed_paths=[{"relation_ids": ["account_to_market"]}],
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "relation_not_in_view"
    assert "account_to_market" in exc.value.message
    assert bound_version in exc.value.message, "the refusal names the View version too"


def test_a_draft_view_version_cannot_be_pinned(live_postgres, project):
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(conn, project_id, name="draft_view", status="draft")

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_view(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="pacing",
            semantic_view_version_id=version_id,
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "version_not_pinnable"
    assert "draft" in exc.value.message


def test_a_moving_pin_is_refused_before_the_database_is_touched(live_postgres, project):
    org_id, project_id = project
    with pytest.raises(topics.AnswerableTopicRefused) as exc:
        topics.bind_view(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="pacing",
            semantic_view_version_id="latest",
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "pin_is_not_exact"


def test_a_view_of_another_project_is_refused_like_an_invented_one(live_postgres, project):
    """Scope is part of the LOOKUP, so the refusal cannot reveal that it exists."""
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)

    other_org, other_project = _uid("org"), _uid("proj")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (other_org, "Neighbour", other_org.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (other_project, other_org, "Neighbour", other_project.lower(), "tester"),
        )
    _foreign_view, foreign_version = _view(conn, other_project, name="foreign_spend")

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_view(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="pacing",
            semantic_view_version_id=foreign_version,
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "unknown_semantic_view_version", (
        "a foreign version must answer exactly like an invented one"
    )


def test_unbinding_removes_it_from_the_resolution(live_postgres, project):
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(
        conn, project_id, relationships=(("campaign_to_account", "campaigns", "accounts"),)
    )
    bound = topics.bind_view(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="pacing",
        semantic_view_version_id=version_id,
        allowed_paths=[{"relation_ids": ["campaign_to_account"]}],
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )
    assert topics.resolve_views(project_id, conn, topic_key="pacing")

    topics.unbind_view(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        binding_id=bound["binding_id"],
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )
    assert topics.resolve_views(project_id, conn, topic_key="pacing") == []

    with conn.cursor() as cur:
        # The View, its version and its relationships are never touched.
        cur.execute(
            "SELECT count(*) FROM app.semantic_view_version_relationships "
            "WHERE view_version_id = %s",
            (version_id,),
        )
        assert cur.fetchone()[0] == 1


def test_a_binding_whose_version_was_archived_is_stale_never_dropped(live_postgres, project):
    """Fail-soft on read. "This View moved on" is not "this topic declared nothing"."""
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(
        conn, project_id, relationships=(("campaign_to_account", "campaigns", "accounts"),)
    )
    topics.bind_view(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="pacing",
        semantic_view_version_id=version_id,
        allowed_paths=[{"relation_ids": ["campaign_to_account"]}],
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )
    with conn.cursor() as cur:
        # `app.reject_semantic_version_mutation` (migration 142) ALLOWS
        # `published -> archived` and refuses everything else: this is the one
        # move a live model makes, replayed rather than faked.
        cur.execute(
            "UPDATE app.semantic_view_versions SET status = 'archived' WHERE id = %s",
            (version_id,),
        )

    resolved = topics.resolve_views(project_id, conn, topic_key="pacing")
    assert len(resolved) == 1, "a stale declaration is information, never litter"
    assert resolved[0]["stale"] is True
    assert resolved[0]["status"] == "archived"


# ---------------------------------------------------------------------------
# A path is a WALK. Its legs chain, and it crosses each relation once.
# ---------------------------------------------------------------------------


def test_a_chained_path_reports_the_ends_of_the_walk(live_postgres, project):
    """The accepted case the two refusals below exist to protect."""
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(
        conn,
        project_id,
        relationships=(
            ("campaign_to_account", "campaigns", "accounts"),
            ("account_to_market", "accounts", "markets"),
        ),
    )

    topics.bind_view(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="pacing",
        semantic_view_version_id=version_id,
        allowed_paths=[{"relation_ids": ["campaign_to_account", "account_to_market"]}],
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )

    path = topics.resolve_views(project_id, conn, topic_key="pacing")[0]["paths"][0]
    assert path["resolved"] is True
    assert path["from"] == "campaigns", "the walk starts where its first leg starts"
    assert path["to"] == "markets", "and arrives where its last leg arrives"


def test_a_path_whose_legs_do_not_chain_is_refused_by_name(live_postgres, project):
    """`["campaign_to_account", "market_to_region"]` was STORED before this rule.

    Resolved, it would have been served as "campaigns -> regions" -- a route that
    crosses nothing between accounts and markets and that no relationship of this
    View authorises. That is a join the model never ratified, handed to an agent
    as if it had.
    """
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(
        conn,
        project_id,
        relationships=(
            ("campaign_to_account", "campaigns", "accounts"),
            ("market_to_region", "markets", "regions"),
        ),
    )

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_view(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="pacing",
            semantic_view_version_id=version_id,
            allowed_paths=[
                {"relation_ids": ["campaign_to_account", "market_to_region"]}
            ],
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "path_not_chained"
    # The refusal names BOTH relations and BOTH datasets: an author told "bad
    # path" has to guess which leg to move.
    assert "campaign_to_account" in exc.value.message
    assert "market_to_region" in exc.value.message
    assert "accounts" in exc.value.message and "markets" in exc.value.message

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.answerable_topic_view_bindings")
        assert cur.fetchone()[0] == 0


def test_a_path_crossing_one_relation_twice_is_refused_by_name(live_postgres, project):
    """A cycle re-enters a dataset it already left, which is how a join multiplies."""
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(
        conn, project_id, relationships=(("campaign_to_account", "campaigns", "accounts"),)
    )

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_view(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="pacing",
            semantic_view_version_id=version_id,
            allowed_paths=[
                {"relation_ids": ["campaign_to_account", "campaign_to_account"]}
            ],
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "duplicate_relation"
    assert "campaign_to_account" in exc.value.message

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.answerable_topic_view_bindings")
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The three refusals the console can meet, each proved on the database.
# ---------------------------------------------------------------------------


def test_an_ambiguous_relation_name_is_refused_by_name(live_postgres, project):
    """Two relationships, one name: choosing either would pick a join nobody named.

    `app.semantic_view_version_relationships` is keyed by `(view_version_id,
    ordinal)` and NOT by name (migration 142:430), so a version published before
    this refusal existed can genuinely carry the same name twice.
    """
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(
        conn,
        project_id,
        relationships=(
            ("campaign_to_account", "campaigns", "accounts"),
            ("campaign_to_account", "campaigns", "advertisers"),
        ),
    )

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_view(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="pacing",
            semantic_view_version_id=version_id,
            allowed_paths=[{"relation_ids": ["campaign_to_account"]}],
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "ambiguous_relation"
    assert "campaign_to_account" in exc.value.message

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.answerable_topic_view_bindings")
        assert cur.fetchone()[0] == 0


def test_binding_the_same_view_version_twice_is_refused_by_name(live_postgres, project):
    """STATED, never absorbed: a caller that believes it declared a View is told.

    The second call carries its OWN idempotency key, so it is a second intent and
    not a replay -- the replay path is the operation ledger's and is not what
    `uq_atvb_version` guards.
    """
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(
        conn, project_id, relationships=(("campaign_to_account", "campaigns", "accounts"),)
    )
    common = {
        "org_id": org_id,
        "project_id": project_id,
        "actor": "tester",
        "topic_key": "pacing",
        "semantic_view_version_id": version_id,
        "host_context": _HOST_CONTEXT,
    }
    topics.bind_view(conn, idempotency_key=_uid("idem"), **common)

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_view(conn, idempotency_key=_uid("idem"), **common)
    assert exc.value.code == "binding_conflict"

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.answerable_topic_view_bindings")
        assert cur.fetchone()[0] == 1, "the first declaration survives the second"


def test_a_malformed_path_is_refused_before_the_database_is_touched(
    live_postgres, project
):
    """`invalid_path` is a SHAPE refusal, and it lands before the ledger opens.

    Proved here rather than offline because the claim is not "Python raises": it
    is that the whole governed write -- operation row included -- never starts.
    """
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id)
    _view_id, version_id = _view(conn, project_id)

    for bad in ("not-an-array", [{"relation_ids": []}], [{"relation_ids": ["  "]}], ["x"]):
        with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
            topics.bind_view(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor="tester",
                topic_key="pacing",
                semantic_view_version_id=version_id,
                allowed_paths=bad,
                idempotency_key=_uid("idem"),
                host_context=_HOST_CONTEXT,
            )
        assert exc.value.code == "invalid_path", bad

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.operations WHERE effective_org_id = %s", (org_id,)
        )
        assert cur.fetchone()[0] == 0, "a shape refusal must not open an operation"


# ---------------------------------------------------------------------------
# What the console binder is offered.
# ---------------------------------------------------------------------------


def test_the_choices_door_offers_only_pinnable_versions_and_their_relations(
    live_postgres, project
):
    """The picker lists exactly what `bind_view` will take, with its relations.

    Offering a `draft` would be offering a choice the write refuses; asking an
    operator to retype a relation name is what made `unknown_relation` the most
    likely outcome of using this screen.
    """
    _org_id, project_id = project
    conn = live_postgres
    _view_id, published = _view(
        conn,
        project_id,
        name="spend",
        relationships=(("campaign_to_account", "campaigns", "accounts"),),
    )
    _view(conn, project_id, name="wip", status="draft")

    choices, truncated = topics.resolve_pinnable_views(project_id, conn)

    assert truncated is False
    assert [c["view_version_id"] for c in choices] == [published], (
        "a draft version is not offered: the write would refuse it"
    )
    relation = choices[0]["relations"][0]
    assert relation["relation_id"] == "campaign_to_account"
    assert (relation["from"], relation["to"]) == ("campaigns", "accounts"), (
        "the binder composes a chain from these endpoints; without them it cannot"
    )


# ---------------------------------------------------------------------------
# What the model is served -- and what it is NOT served.
# ---------------------------------------------------------------------------


def _bind_a_query(
    conn, org_id: str, project_id: str, topic_key: str, view_id: str, view_version_id: str
) -> None:
    """A governed query on the topic, so `verified_query_pairs` emits a pair.

    A Query Spec pins a Semantic View version by construction (migration 151), so
    the caller hands one in. That View is NOT bound to the topic here: binding a
    View to a topic is what this story adds, and the two must stay separable.
    """
    spec_id, spec_version = _uid("qs"), _uid("qsv")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_specs
                (id, org_id, project_id, semantic_view_id, name, created_by)
            VALUES (%s, %s, %s, %s, %s, 'tester')
            """,
            (spec_id, org_id, project_id, view_id, f"spec {topic_key}"),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number,
                 semantic_view_id, semantic_view_version_id, spec,
                 content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'tester')
            """,
            (spec_version, spec_id, org_id, project_id, view_id, view_version_id,
             "d" * 64),
        )
    topics.bind_query(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key=topic_key,
        query_spec_version_id=spec_version,
        role="headline",
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )


def test_the_pair_of_an_unbound_topic_is_exactly_what_it_was(live_postgres, project):
    """AC: a topic with no View keeps its payload. NOT `views: []` -- no key at all."""
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id, key="pacing")
    query_view, query_version = _view(conn, project_id, name="query_only")
    _bind_a_query(conn, org_id, project_id, "pacing", query_view, query_version)

    pairs, reason = topics.verified_query_pairs(project_id, conn)
    assert reason is None
    assert len(pairs) == 1
    assert set(pairs[0]) == _PAIR_KEYS_BEFORE_75_5, (
        "the payload of an unbound topic changed; `views` must be absent, not empty"
    )


def test_the_pair_of_a_bound_topic_carries_its_views(live_postgres, project):
    org_id, project_id = project
    conn = live_postgres
    _topic(conn, org_id, project_id, key="pacing")
    view_id, version_id = _view(
        conn, project_id, relationships=(("campaign_to_account", "campaigns", "accounts"),)
    )
    _bind_a_query(conn, org_id, project_id, "pacing", view_id, version_id)
    topics.bind_view(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="pacing",
        semantic_view_version_id=version_id,
        allowed_paths=[{"relation_ids": ["campaign_to_account"]}],
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )

    pairs, reason = topics.verified_query_pairs(project_id, conn)
    assert reason is None
    assert set(pairs[0]) == _PAIR_KEYS_BEFORE_75_5 | {"views"}, (
        "exactly ONE sibling key is added, and only for a topic that declares a View"
    )
    views = pairs[0]["views"]
    assert len(views) == 1
    assert views[0]["view_id"] == view_id
    assert views[0]["view_version_id"] == version_id
    assert views[0]["stale"] is False
    assert views[0]["paths"][0]["relation_ids"] == ["campaign_to_account"]
    assert views[0]["paths"][0]["fan_out_policy"] == "forbid"
