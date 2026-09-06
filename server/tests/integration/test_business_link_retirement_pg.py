"""A governed business link is retired, never destroyed (Story 49.2, AC1).

`business_taxonomy.delete_link` ran `DELETE FROM app.mdm_business_links`. The
rule it broke is already ratified one surface over -- *"Retirement is a
supersede, never a delete. An event that has been read is evidence"*
(`context-hub.md`, amendment of 2026-08-17) -- and a business link is read by
more surfaces than an event is.

WHY EVERY TEST HERE NEEDS A REAL DATABASE. The three properties that matter are
properties of the schema, not of the Python:

* the hard delete is refused by a TRIGGER, so removing the statement from one
  function is not what makes it impossible;
* a withdrawn link can be made again, which only works because migration 306
  replaced a table-wide UNIQUE constraint with a partial one;
* a link whose Business Domain has since been ARCHIVED can still be withdrawn,
  which only works because the scope trigger was re-declared on the columns it
  validates -- before that it fired on every UPDATE and refused the retirement
  with a message about the domain.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.pg_owner

ACTOR = "owner@example.com"


@pytest.fixture()
def linked(live_postgres):
    """One organization, one Project, one governed link on a Context topic."""
    from core import business_taxonomy

    suffix = uuid.uuid4().hex[:10]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    topic_id = f"top_{suffix}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, f"Org {suffix}", f"org-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, f"Project {suffix}", f"project-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (topic_id, project_id, f"Topic {suffix}", "body", ACTOR),
        )
    live_postgres.commit()

    domain = business_taxonomy.list_taxonomy(live_postgres, org_id=org_id)["domains"][0]
    link = business_taxonomy.create_link(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        taxonomy_type="business_domain",
        taxonomy_id=str(domain["id"]),
        target_type="topic",
        target_id=topic_id,
        relation_type="explains",
        actor=ACTOR,
        reason="retirement proof",
    )
    live_postgres.commit()
    return {
        "org_id": org_id,
        "project_id": project_id,
        "topic_id": topic_id,
        "domain": domain,
        "link": link,
    }


def _retire(conn, linked, *, reason="withdrawn by the operator"):
    from core import business_taxonomy

    return business_taxonomy.retire_link(
        conn,
        org_id=linked["org_id"],
        project_id=linked["project_id"],
        link_id=str(linked["link"]["id"]),
        actor=ACTOR,
        reason=reason,
    )


def test_a_retired_link_stops_being_served_and_stays_readable(live_postgres, linked):
    from core import business_taxonomy

    conn = live_postgres
    retired = _retire(conn, linked)
    assert retired["retired_at"] is not None
    assert retired["retired_by"] == ACTOR
    assert retired["retired_reason"] == "withdrawn by the operator"

    live = business_taxonomy.list_links(
        conn, org_id=linked["org_id"], project_id=linked["project_id"]
    )
    assert live == []

    # A list that claims to be complete never silently drops a row.
    everything = business_taxonomy.list_links(
        conn,
        org_id=linked["org_id"],
        project_id=linked["project_id"],
        include_retired=True,
    )
    assert [row["id"] for row in everything] == [str(linked["link"]["id"])]
    assert everything[0]["retired_reason"] == "withdrawn by the operator"


def test_the_row_is_still_there_and_a_hard_delete_is_refused_by_the_database(
    live_postgres, linked
):
    """The guard repairs the CLASS, not the one caller that used to delete.

    Removing the `DELETE` from `delete_link` would leave the next writer free to
    put it back. The trigger is what makes it impossible, and the erasure hatch
    is what keeps an RGPD erasure possible in spite of it.
    """
    import psycopg

    conn = live_postgres
    _retire(conn, linked)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.mdm_business_links WHERE id = %s",
            (str(linked["link"]["id"]),),
        )
        assert cur.fetchone()[0] == 1

    with pytest.raises(psycopg.errors.RaiseException) as refused:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.mdm_business_links WHERE id = %s",
                (str(linked["link"]["id"]),),
            )
    assert "retired, never deleted" in str(refused.value)
    conn.rollback()


def test_an_organization_erasure_still_passes_through_the_guard(live_postgres, linked):
    """Migration 099's rule: a protective DELETE inside the org tree yields.

    Without the `app.rgpd_erasure` clause, the first real right-to-erasure
    request on an organization holding one business link would fail -- and
    nobody would learn that until the request arrived.
    """
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute(
            "DELETE FROM app.mdm_business_links WHERE id = %s",
            (str(linked["link"]["id"]),),
        )
        assert cur.rowcount == 1
    conn.rollback()


def test_a_link_withdrawn_by_mistake_can_be_made_again(live_postgres, linked):
    """The repair path the context-hub amendment names: write it again.

    130's UNIQUE covered every row, retired ones included, so the second
    creation would have raised a duplicate and the only repair for one wrong
    click would have been to have no repair at all.
    """
    from core import business_taxonomy

    conn = live_postgres
    _retire(conn, linked)

    again = business_taxonomy.create_link(
        conn,
        org_id=linked["org_id"],
        project_id=linked["project_id"],
        taxonomy_type="business_domain",
        taxonomy_id=str(linked["domain"]["id"]),
        target_type="topic",
        target_id=linked["topic_id"],
        relation_type="explains",
        actor=ACTOR,
        reason="filed by mistake",
    )
    assert again["id"] != str(linked["link"]["id"])

    live = business_taxonomy.list_links(
        conn, org_id=linked["org_id"], project_id=linked["project_id"]
    )
    assert [row["id"] for row in live] == [str(again["id"])]


def test_a_second_retirement_is_refused_rather_than_overwriting_the_first(
    live_postgres, linked
):
    """Two retirements would mean two authors and two reasons for one act."""
    from core.business_taxonomy import BusinessTaxonomyError

    conn = live_postgres
    _retire(conn, linked, reason="the first reason")
    with pytest.raises(BusinessTaxonomyError, match="already withdrawn"):
        _retire(conn, linked, reason="a second reason")


def test_the_link_of_an_archived_domain_can_still_be_withdrawn(live_postgres, linked):
    """The scope trigger no longer re-opens a question settled at creation.

    `validate_business_link_scope` demands that the taxonomy source still be
    `status = 'active'`, and it fired on EVERY update. Archiving a domain is
    exactly when an operator wants to withdraw its links, and the refusal spoke
    about the domain while the caller was withdrawing the link.
    """
    conn = live_postgres
    # ARCHIVED BY SQL, and that is the honest fixture since 2026-08-25.
    # `update_domain` refuses -- archiving is a Master Data act now -- so this is
    # a row archived before the cutover, or one whose node the authority
    # archived. Both are states the retirement has to keep working through: the
    # link is read from this store either way, and withdrawing it is exactly what
    # an operator does after a domain goes.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_business_domains "
            "SET status = 'archived', archived_at = now() WHERE id = %s AND org_id = %s",
            (str(linked["domain"]["id"]), linked["org_id"]),
        )

    retired = _retire(conn, linked)
    assert retired["retired_at"] is not None


def test_the_retirement_is_audited_under_its_own_action(live_postgres, linked):
    """`business_link.deleted` named what used to happen. It no longer happens."""
    conn = live_postgres
    _retire(conn, linked)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT action FROM app.audit_log "
            "WHERE metadata->>'resource_id' = %s ORDER BY id DESC LIMIT 1",
            (str(linked["link"]["id"]),),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "business_link.retired"
