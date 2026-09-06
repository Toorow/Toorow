"""A governed write that actually WRITES, against a real database.

WHY THIS FILE EXISTS, and it is the whole lesson of the Epic 52 review. The seam
suite for these routes asserted 401, 400 and 422 -- it proved the door refuses,
and never once that it opens. Under that green suite, every write route answered
HTTP 500 for the life of the epic:

  * `host_context={"surface": "console"}` is refused by
    `operations._HOST_KEYS`, so `prepare_operation` raised before any mutation;
  * and nothing called `conn.commit()`, so repairing only the first half would
    have answered 201 while persisting nothing -- which looks like it worked.

Neither could be caught by a mock: a mocked cursor accepts any host context and a
mocked connection has nothing to commit. So this file uses the real thing, and
asserts the two properties the acceptance criteria actually claim -- the row is
there afterwards, and the governance ledger recorded it.

It runs as the ordinary `connector` role. `live_postgres` rolls back on teardown,
so each test builds its own chain and leaves nothing behind.
"""

from __future__ import annotations

import pytest
from ulid import ULID

psycopg = pytest.importorskip("psycopg")

from core import answerable_topics as topics  # noqa: E402


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


@pytest.fixture()
def project(live_postgres):
    """An org + project inside the test transaction."""
    org_id, project_id = _uid("org"), _uid("proj")
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "Epic 52 write", org_id.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, "Epic 52 write", project_id.lower(), "tester"),
        )
    return org_id, project_id


#: The host context the REST layer passes. Written here as a literal on purpose:
#: if someone invents a key again, this file fails where the seam suite could not.
_HOST_CONTEXT = {"host": "rest"}


def test_creating_a_topic_persists_it_and_records_the_operation(live_postgres, project):
    org_id, project_id = project
    conn = live_postgres

    result = topics.create_topic(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="weekly-pacing",
        payload={
            "title": "Weekly pacing",
            "answers_question": "Are we pacing to plan this week?",
            "base_template_id": "kpi",
        },
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )

    assert result["topic_key"] == "weekly-pacing"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT lifecycle_state, origin FROM app.answerable_topics "
            "WHERE org_id = %s AND project_id = %s AND topic_key = %s",
            (org_id, project_id, "weekly-pacing"),
        )
        head = cur.fetchone()
        assert head == ("active", "project"), "the head is not there: the write did nothing"

        cur.execute(
            "SELECT title FROM app.answerable_topic_versions WHERE id = %s",
            (result["version_id"],),
        )
        assert cur.fetchone() == ("Weekly pacing",)

        # AC6 claims audit and outbox commit WITH the change. That claim was empty
        # for the whole epic; this is what makes it true.
        cur.execute(
            "SELECT count(*) FROM app.operations "
            "WHERE effective_org_id = %s AND command_type = 'answerable_topic.created'",
            (org_id,),
        )
        assert cur.fetchone()[0] == 1, "no operation row: the governance ledger is empty"

        # `app.audit_log` scopes by `effective_org_id`, not `org_id` -- the column
        # name differs from every other table here, which is exactly the kind of
        # thing a mocked cursor never tells you.
        cur.execute(
            "SELECT action, outcome FROM app.audit_log WHERE effective_org_id = %s", (org_id,)
        )
        audit = cur.fetchall()
        assert audit, "no audit row: the write happened outside the governance ledger"
        assert audit[0] == ("answerable_topic.created", "succeeded")

        cur.execute("SELECT count(*) FROM app.operation_outbox")
        assert cur.fetchone()[0] >= 1, "no outbox entry"


def test_an_invented_host_context_key_is_refused_before_any_mutation(live_postgres, project):
    """The exact shape of the 500 this epic shipped, pinned so it cannot return."""
    org_id, project_id = project

    with pytest.raises(Exception) as exc, live_postgres.transaction():
        topics.create_topic(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="invented-context",
            payload={"title": "T", "answers_question": "Q?", "base_template_id": "kpi"},
            idempotency_key=_uid("idem"),
            host_context={"surface": "console"},
        )
    assert "host_context" in str(exc.value), (
        "the refusal must NAME the field: this one cost the epic seven silent 500s"
    )


def test_rewording_appends_a_version_and_the_previous_one_survives(live_postgres, project):
    org_id, project_id = project
    conn = live_postgres

    first = topics.create_topic(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="pacing",
        payload={
            "title": "Pacing",
            "answers_question": "Are we pacing?",
            "base_template_id": "kpi",
        },
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )
    second = topics.append_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="pacing",
        payload={
            "title": "Pacing, restated",
            "answers_question": "Are we pacing to plan?",
            "base_template_id": "kpi",
        },
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )

    assert second["version_number"] == 2
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_number, title FROM app.answerable_topic_versions "
            "WHERE answerable_topic_id = %s ORDER BY version_number",
            (second["topic_id"],),
        )
        assert cur.fetchall() == [(1, "Pacing"), (2, "Pacing, restated")], (
            "rewording must APPEND: the first version is what the project used to answer"
        )
        cur.execute(
            "SELECT predecessor_version_id FROM app.answerable_topic_versions WHERE id = %s",
            (second["version_id"],),
        )
        assert cur.fetchone()[0] == first["version_id"], "the lineage is broken"


def test_binding_another_projects_knowledge_is_refused(live_postgres, project):
    """The cross-tenant hole the review found (C-2 / S-3), against a real database.

    Migration 173's foreign keys prove a knowledge version EXISTS; they cannot
    prove whose it is, because `context_topics_versions` carries no `org_id`. The
    check therefore has to be in the write path -- and it was missing while the
    equivalent check for Query Specs sat twenty lines above it.
    """
    org_id, project_id = project
    conn = live_postgres

    # A knowledge item that belongs to ANOTHER project.
    other_org, other_project = _uid("org"), _uid("proj")
    knowledge_id = _uid("ctx")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (other_org, "Neighbour", other_org.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) VALUES (%s,%s,%s,%s,%s)",
            (other_project, other_org, "Neighbour", other_project.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, status, created_by) "
            "VALUES (%s,%s,%s,%s,'active','tester')",
            (knowledge_id, other_project, "Their attribution policy", "secret"),
        )
        cur.execute(
            "INSERT INTO app.context_topics_versions "
            "(topic_id, project_id, title, body_md, status, created_by, created_at, updated_at, "
            " version_number, changed_by, changed_at) "
            "VALUES (%s,%s,%s,%s,'active','tester',now(),now(),1,'tester',now())",
            (knowledge_id, other_project, "Their attribution policy", "secret"),
        )

    topics.create_topic(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="mine",
        payload={"title": "Mine", "answers_question": "Mine?", "base_template_id": "kpi"},
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )

    with pytest.raises(topics.AnswerableTopicRefused) as exc, conn.transaction():
        topics.bind_knowledge(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor="tester",
            topic_key="mine",
            knowledge_kind="topic",
            knowledge_id=knowledge_id,
            knowledge_version=1,
            idempotency_key=_uid("idem"),
            host_context=_HOST_CONTEXT,
        )
    assert exc.value.code == "unknown_knowledge_version", (
        "another project's knowledge must not be pinnable, and the refusal must not "
        "disclose that it exists elsewhere"
    )


def test_a_pin_to_another_projects_knowledge_is_not_readable_either(live_postgres, project):
    """Belt and braces: even a row inserted behind the API must not be cited.

    The write check above is the door; this is the floor under it. A binding
    smuggled straight into the table -- by a migration, a script, or a bug --
    resolves to nothing, because the read joins on the project too.
    """
    org_id, project_id = project
    conn = live_postgres

    other_org, other_project = _uid("org"), _uid("proj")
    knowledge_id = _uid("ctx")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (other_org, "Neighbour", other_org.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) VALUES (%s,%s,%s,%s,%s)",
            (other_project, other_org, "Neighbour", other_project.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, status, created_by) "
            "VALUES (%s,%s,%s,%s,'active','tester')",
            (knowledge_id, other_project, "Their attribution policy", "secret"),
        )
        cur.execute(
            "INSERT INTO app.context_topics_versions "
            "(topic_id, project_id, title, body_md, status, created_by, created_at, updated_at, "
            " version_number, changed_by, changed_at) "
            "VALUES (%s,%s,%s,%s,'active','tester',now(),now(),1,'tester',now())",
            (knowledge_id, other_project, "Their attribution policy", "secret"),
        )

    created = topics.create_topic(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="mine",
        payload={"title": "Mine", "answers_question": "Mine?", "base_template_id": "kpi"},
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.answerable_topic_knowledge_bindings "
            "(id, answerable_topic_id, org_id, project_id, knowledge_kind, "
            " context_topic_id, context_topic_version, created_by) "
            "VALUES (%s,%s,%s,%s,'topic',%s,1,'tester')",
            (_uid("atk"), created["topic_id"], org_id, project_id, knowledge_id),
        )

    citations, status, _refused, _evaluated = topics.knowledge_citations(project_id, "mine", conn)

    assert citations == [], "another project's knowledge title was cited"
    assert status == topics.CONTEXT_MISSING_REASON, (
        "an unreadable pin is `context_missing`, never a silent empty answer"
    )


def test_platform_knowledge_stays_bindable_and_citable(live_postgres, project):
    """`project_id IS NULL` is PLATFORM scope, readable by every project.

    The first repair of the cross-tenant hole used strict equality and closed this
    door too: platform knowledge became unpinnable, and any pin already stored
    against it reported `context_missing` -- a fix that invented a second defect
    while closing the first. `context_store.py:425-431` is the rule this follows.
    """
    org_id, project_id = project
    conn = live_postgres
    knowledge_id = _uid("ctx")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, status, created_by) "
            "VALUES (%s, NULL, %s, %s, 'active', 'tester')",
            (knowledge_id, "Platform attribution policy", "shared"),
        )
        cur.execute(
            "INSERT INTO app.context_topics_versions "
            "(topic_id, project_id, title, body_md, status, created_by, created_at, updated_at, "
            " version_number, changed_by, changed_at) "
            "VALUES (%s, NULL, %s, %s, 'active', 'tester', now(), now(), 1, 'tester', now())",
            (knowledge_id, "Platform attribution policy", "shared"),
        )

    topics.create_topic(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="mine",
        payload={"title": "Mine", "answers_question": "Mine?", "base_template_id": "kpi"},
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )
    topics.bind_knowledge(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="mine",
        knowledge_kind="topic",
        knowledge_id=knowledge_id,
        knowledge_version=1,
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )

    citations, status, _refused, _evaluated = topics.knowledge_citations(project_id, "mine", conn)
    assert status is None, "platform knowledge must be readable, not `context_missing`"
    assert [c["title"] for c in citations] == ["Platform attribution policy"]


def test_a_pin_whose_head_was_purged_is_context_missing_not_a_citation(live_postgres, project):
    """The scenario the story documents three times, reproduced for real.

    Production holds 41 knowledge VERSION rows and ZERO heads: versions are
    append-only and survive, heads are deletable and did not. Until this repair the
    read joined only `*_versions`, whose `title` is NOT NULL and whose row the
    migration-173 foreign key protects -- so a pin to purged knowledge resolved a
    title and was CITED. `context_missing` was unreachable at pin level while the
    story claimed it three times.

    A hand-built tuple cannot prove this: the shape it would need is one the schema
    cannot produce. So the head is inserted, pinned, and then deleted.
    """
    org_id, project_id = project
    conn = live_postgres
    knowledge_id = _uid("ctx")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, status, created_by) "
            "VALUES (%s,%s,%s,%s,'active','tester')",
            (knowledge_id, project_id, "Attribution policy", "body"),
        )
        cur.execute(
            "INSERT INTO app.context_topics_versions "
            "(topic_id, project_id, title, body_md, status, created_by, created_at, updated_at, "
            " version_number, changed_by, changed_at) "
            "VALUES (%s,%s,%s,%s,'active','tester',now(),now(),1,'tester',now())",
            (knowledge_id, project_id, "Attribution policy", "body"),
        )

    topics.create_topic(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="mine",
        payload={"title": "Mine", "answers_question": "Mine?", "base_template_id": "kpi"},
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )
    topics.bind_knowledge(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor="tester",
        topic_key="mine",
        knowledge_kind="topic",
        knowledge_id=knowledge_id,
        knowledge_version=1,
        idempotency_key=_uid("idem"),
        host_context=_HOST_CONTEXT,
    )

    # While the head exists, the pin is a legitimate citation.
    citations, status, _refused, _evaluated = topics.knowledge_citations(project_id, "mine", conn)
    assert status is None and [c["id"] for c in citations] == [knowledge_id]

    # The head is purged; the append-only version row survives it.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.context_topics WHERE id = %s", (knowledge_id,))
        cur.execute(
            "SELECT count(*) FROM app.context_topics_versions WHERE topic_id = %s",
            (knowledge_id,),
        )
        assert cur.fetchone()[0] == 1, "the version must survive its head, or this proves nothing"

    citations, status, _refused, _evaluated = topics.knowledge_citations(project_id, "mine", conn)
    assert citations == [], "knowledge whose head was purged must not be cited"
    assert status == topics.CONTEXT_MISSING_REASON
