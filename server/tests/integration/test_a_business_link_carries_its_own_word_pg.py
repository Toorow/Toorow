"""A governed business link travels with the WORD of the taxonomy it hangs off.

`app.mdm_business_links` stores the id of a Business Domain or classification and
nothing readable, so every console reading links had to resolve the word itself.
Two did -- the Knowledge Library and the Skill editor -- against whichever
catalogue the page happened to have loaded, and printed `bdom_<ULID>` on a pill
whenever that catalogue did not carry the node. That is the console standing up
as a second authority on the vocabulary, which
`docs/product-architecture/visualization-and-rendering.md` refuses in as many
words: *"The name is resolved on the server, where the vocabulary lives. A
console that resolved names itself would be a second authority on the
vocabulary, and two authorities eventually disagree."* Its `Incomplete if` closes
the same loop -- incomplete *"if a label is composed in the browser"*.

WHY A LIVE DATABASE. The name is read off `business_identity_catalogue`'s UNION
of the authority store (`app.master_data_nodes` + the current object version) and
the superseded legacy row. A mock connection would assert that the code runs the
statement written beside it, which is the statement asserting itself; only a real
schema says whether the UNION resolves a node held by either store, and whether
an id that resolves to nothing comes back as an absence rather than as itself.
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
        reason="naming proof",
    )
    live_postgres.commit()
    return {
        "org_id": org_id,
        "project_id": project_id,
        "topic_id": topic_id,
        "domain": domain,
        "link": link,
    }


def test_a_served_link_carries_the_domain_s_name_not_only_its_id(live_postgres, linked):
    from core import business_taxonomy

    links = business_taxonomy.list_links(
        live_postgres, org_id=linked["org_id"], project_id=linked["project_id"]
    )

    assert len(links) == 1
    served = links[0]
    # The word, and it is the catalogue's own -- not a slug, not a re-derivation.
    assert served["taxonomy_name"] == linked["domain"]["name"]
    # And the id is still there: the two are different facts and a screen needs
    # both -- one to read, one to address.
    assert served["taxonomy_id"] == str(linked["domain"]["id"])


def test_an_unresolvable_taxonomy_serves_an_absence_not_the_identifier(
    live_postgres, linked
):
    """THE PART THAT MATTERS. A name that cannot be read is `None`.

    The tempting fallback is `names.get(id) or id`, and it is the whole defect
    moved one layer down: the console would then receive an identifier in a field
    called `taxonomy_name`, believe it was a word, and print it -- with no way
    left for any surface to tell the two apart. `None` is what lets the pill say
    the business key is no longer readable.

    MEASURED WHILE WRITING THIS, AND WORTH SAYING: a STORED link cannot reach
    this state through any supported door. Re-pointing one at an absent node is
    refused by `app.validate_business_link_scope`; removing the node under it is
    refused twice over, by `mdm_business_domain_versions_domain_id_fkey` and then
    by `app.reject_business_taxonomy_version_mutation` ("Business taxonomy
    history is append-only: DELETE blocked"). So the absence this asserts is a
    DEFENSIVE state, not a live one -- and it is asserted where the decision is
    actually made, on the resolution itself, rather than through a door the
    schema keeps shut.
    """
    from core.business_taxonomy import _taxonomy_names

    absent = f"bdm_{uuid.uuid4().hex[:20]}"
    names = _taxonomy_names(
        live_postgres,
        org_id=linked["org_id"],
        links=[
            {"taxonomy_type": "business_domain", "taxonomy_id": str(linked["domain"]["id"])},
            {"taxonomy_type": "business_domain", "taxonomy_id": absent},
        ],
    )

    # The one that resolves carries its word; the one that does not carries
    # nothing at all -- never itself.
    assert names[str(linked["domain"]["id"])] == linked["domain"]["name"]
    assert absent not in names


def test_list_links_serves_the_absence_it_was_handed_and_never_the_id(
    live_postgres, linked, monkeypatch
):
    """The other half of the line above: what `list_links` DOES with an absence.

    The test before it proves the resolution answers nothing for an id it cannot
    read. This one proves the assembly does not then quietly repair that with the
    id -- the exact edit that would make every screen believe it had been served a
    word. The schema refuses every door to the real state (see the docstring
    above), so the absence is handed in rather than staged, and the assertion is
    on the field the console reads.
    """
    from core import business_taxonomy

    monkeypatch.setattr(business_taxonomy, "_taxonomy_names", lambda *a, **k: {})

    links = business_taxonomy.list_links(
        live_postgres, org_id=linked["org_id"], project_id=linked["project_id"]
    )

    assert len(links) == 1
    assert links[0]["taxonomy_name"] is None
    assert links[0]["taxonomy_id"] == str(linked["domain"]["id"])


def test_the_name_is_read_once_per_kind_never_once_per_link(live_postgres, linked):
    """A read that scales with the number of links is a read that gets removed.

    Not a micro-optimisation: `list_links` is called by the Context Hub layout,
    the MCP surface and the graph projection, and an N+1 here would have made
    serving the word cost more than composing it in the browser -- which is how a
    repair like this gets reverted six weeks later.
    """
    from core import business_taxonomy

    topic_ids = []
    for index in range(4):
        topic_id = f"top_{uuid.uuid4().hex[:10]}"
        topic_ids.append(topic_id)
        with live_postgres.cursor() as cur:
            cur.execute(
                "INSERT INTO app.context_topics (id, project_id, title, body_md, created_by) "
                "VALUES (%s,%s,%s,%s,%s)",
                (topic_id, linked["project_id"], f"Topic {index}", "body", ACTOR),
            )
        business_taxonomy.create_link(
            live_postgres,
            org_id=linked["org_id"],
            project_id=linked["project_id"],
            taxonomy_type="business_domain",
            taxonomy_id=str(linked["domain"]["id"]),
            target_type="topic",
            target_id=topic_id,
            relation_type="explains",
            actor=ACTOR,
            reason="naming proof",
        )
    live_postgres.commit()

    executed: list[str] = []
    real_cursor = live_postgres.cursor

    class _CountingCursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, *args, **kwargs):
            executed.append(str(statement))
            return self._inner.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    live_postgres.cursor = lambda *a, **k: _CountingCursor(real_cursor(*a, **k))
    try:
        links = business_taxonomy.list_links(
            live_postgres, org_id=linked["org_id"], project_id=linked["project_id"]
        )
    finally:
        live_postgres.cursor = real_cursor

    assert len(links) == 5
    assert all(link["taxonomy_name"] == linked["domain"]["name"] for link in links)
    # One statement for the links, one for the only taxonomy kind they use.
    # Never one per link, however many there are.
    assert len(executed) == 2
