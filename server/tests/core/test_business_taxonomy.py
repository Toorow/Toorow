"""Focused proofs for Story 45.1 business taxonomy and path governance.

Since 2026-08-25 the four IDENTITY writers refuse -- see
`TestLegacyIdentityWritersRefuse` at the bottom, which names the eight tests it
replaces and why keeping them would have hidden the cutover behind a green run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from core.business_taxonomy import (
    BUSINESS_TARGET_TYPES,
    TaxonomyCycleError,
    ensure_no_cycle,
    graph_projection,
    normalize_slug,
    taxonomy_hierarchy_edges,
    validate_target_reference,
)

ROOT = Path(__file__).resolve().parents[3]


import core.business_taxonomy as taxonomy  # noqa: E402


def _mock_conn():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_slug_is_client_extensible_not_a_closed_business_enum():
    assert normalize_slug("Customer Success & Expansion") == "customer-success-expansion"
    assert normalize_slug("  Produit / Offre  ") == "produit-offre"
    with pytest.raises(ValueError, match="slug"):
        normalize_slug("---")


def test_cycle_guard_rejects_self_and_descendant_parent():
    with pytest.raises(TaxonomyCycleError):
        ensure_no_cycle("class_a", "class_a", set())
    with pytest.raises(TaxonomyCycleError):
        ensure_no_cycle("class_a", "class_child", {"class_child", "class_leaf"})
    ensure_no_cycle("class_a", "class_other", {"class_child"})
    ensure_no_cycle("class_a", None, {"class_child"})


def test_taxonomy_hierarchy_edges_are_deterministic_and_read_only():
    domains = [{"id": "bdm_retail"}, {"id": "bdm_product"}]
    classifications = [
        {
            "id": "bcl_lifecycle",
            "domain_id": "bdm_retail",
            "parent_id": "bcl_subscriptions",
            "created_by": "owner@example.com",
            "created_at": "2026-07-28T10:00:00Z",
        },
        {
            "id": "bcl_subscriptions",
            "domain_id": "bdm_retail",
            "parent_id": None,
            "created_by": "owner@example.com",
            "created_at": "2026-07-28T09:00:00Z",
        },        {
            "id": "bcl_orphan",
            "domain_id": "bdm_retail",
            "parent_id": "bcl_archived_parent",
            "created_by": "owner@example.com",
            "created_at": "2026-07-28T08:00:00Z",
        },
    ]

    edges = taxonomy_hierarchy_edges(domains, classifications, project_id="p1")

    assert [(edge["from_id"], edge["to_id"]) for edge in edges] == [
        ("bcl_subscriptions", "bcl_lifecycle"),
        ("bdm_retail", "bcl_subscriptions"),
    ]
    assert {edge["edge_type"] for edge in edges} == {"contains"}
    assert {edge["link_origin"] for edge in edges} == {"derived"}
    assert {edge["project_id"] for edge in edges} == {"p1"}

def test_graph_projection_includes_taxonomy_hierarchy(monkeypatch):
    taxonomy = {
        "domains": [
            {
                "id": "bdm_retail", "name": "Retail", "description": "Commerce",
                "owner": None, "created_by": "owner", "version_number": 1,
                "status": "active", "slug": "retail",
            }
        ],
        "classifications": [
            {
                "id": "bcl_subscriptions", "domain_id": "bdm_retail", "parent_id": None,
                "name": "Subscriptions", "description": "Recurring offers", "owner": None,
                "created_by": "owner", "created_at": "2026-07-28T10:00:00Z",
                "updated_at": "2026-07-28T10:00:00Z", "version_number": 1,
                "status": "active", "slug": "subscriptions",
                "classification_type": "revenue_model",
            }
        ],
    }
    monkeypatch.setattr("core.business_taxonomy.list_taxonomy", lambda *args, **kwargs: taxonomy)
    monkeypatch.setattr("core.business_taxonomy.list_links", lambda *args, **kwargs: [])

    projection = graph_projection(None, org_id="org_1", project_id="p1")

    assert projection["edges"] == [
        {
            "id": "taxonomy_contains:bdm_retail:bcl_subscriptions",
            "from_id": "bdm_retail",
            "from_type": "business_domain",
            "to_id": "bcl_subscriptions",
            "to_type": "business_classification",
            "edge_type": "contains",
            "project_id": "p1",
            "created_by": "owner",
            "created_at": "2026-07-28T10:00:00Z",
            "link_origin": "derived",
        }
    ]


def test_migration_130_declares_seed_data_hierarchy_and_immutable_history():
    sql = (ROOT / "infra/nango/migrations/130_business_taxonomy_context_paths.sql").read_text(
        encoding="utf-8"
    )
    for seed in ("Sales", "Marketing", "Product", "Engineering", "Finance", "Legal"):
        assert f"'{seed}'" in sql
    assert "app.mdm_business_domain_templates" in sql
    assert "app.mdm_business_domains" in sql
    assert "app.mdm_business_classifications" in sql
    assert "app.mdm_business_domain_versions" in sql
    assert "app.mdm_business_classification_versions" in sql
    assert "trg_mdm_business_classifications_no_cycle" in sql
    assert "append-only" in sql
    assert "CHECK (slug IN" not in sql


def test_datastream_is_a_canonical_project_scoped_business_link_target() -> None:
    conn, cur = _mock_conn()
    cur.fetchone.return_value = (1,)

    validate_target_reference(
        conn,
        project_id="proj_01",
        target_type="datastream",
        target_id="ds_01",
    )

    assert "datastream" in BUSINESS_TARGET_TYPES
    sql, params = cur.execute.call_args.args
    assert "FROM app.datastreams" in sql
    assert "project_id = %s" in sql
    assert params == ("ds_01", "proj_01")


def test_a_refusal_names_the_field_it_refused_not_always_the_slug() -> None:
    """Live finding F3: `normalize_slug` normalizes three different fields but
    hard-coded "slug" into every message, so a console omitting
    `classification_type` was told "slug is required" and the operator went
    looking at a field that was fine. A validation message naming another field
    is worse than no message."""
    for field in ("classification_type", "relation_type"):
        with pytest.raises(ValueError, match=f"{field} is required"):
            normalize_slug(None, field)
        with pytest.raises(ValueError, match=f"{field} must contain letters or digits"):
            normalize_slug("!!!", field)
        with pytest.raises(ValueError, match=f"{field} must be 96 characters or fewer"):
            normalize_slug("x" * 97, field)

    # The default is unchanged: the slug still speaks of itself.
    with pytest.raises(ValueError, match="slug is required"):
        normalize_slug("")


# ---------------------------------------------------------------------------
# THE FOUR IDENTITY WRITERS REFUSE -- story 49.3, the acceptance schedule Jean
# ratified on 2026-08-25 in `docs/product-architecture/governance.md`.
#
# WHAT THIS BLOCK REPLACES, named so the cutover is not hidden by a green suite.
# Eight tests proved the writes, and they proved a behaviour the product no
# longer has:
#
#   test_create_domain_appends_version_and_transactional_audit
#   test_update_domain_archive_and_restore_append_distinct_versions  (x2 params)
#   test_create_classification_refusal_points_at_classification_type
#   test_archiving_is_refused_while_a_live_version_still_references_the_domain
#   test_history_never_blocks_archiving_because_history_never_comes_back
#   test_a_draft_blocks_too_because_somebody_can_publish_it_tomorrow
#   test_an_edit_that_is_not_an_archive_never_pays_for_the_impact_read
#
# The last four proved `update_domain`'s outbound archive guard. That guard did
# not move to the authority with the rows: `master_data_commands` reads
# `app.master_data_used_by`, and no writer registers a Semantic Model version's
# Business Domain reference there yet. It is written into the *Incomplete if* of
# `governance.md` rather than left in a test of a function that cannot run.
#
# TWO ATTACKS, BECAUSE ONE IS NOT ENOUGH -- the shape story 49.3 used on the
# eight doors it closed the same day. The first fixes the CONTRACT: the refusal,
# and a sentence that names the two gestures rather than a store. The second
# reads this module's own SOURCE and refuses the INSERT: a body restored by hand
# would write again, and the contract test alone would only go red once somebody
# called it -- the source test goes red in the same edit.
# ---------------------------------------------------------------------------


_IDENTITY_WRITERS = (
    ("create_domain", {"org_id": "org_01", "name": "N", "slug": "n"}),
    ("update_domain", {"org_id": "org_01", "domain_id": "bdm_01", "patch": {"name": "N"}}),
    (
        "create_classification",
        {
            "org_id": "org_01",
            "domain_id": "bdm_01",
            "parent_id": None,
            "classification_type": "segment",
            "slug": "n",
            "name": "N",
        },
    ),
    (
        "update_classification",
        {"org_id": "org_01", "classification_id": "bcl_01", "patch": {"name": "N"}},
    ),
)


class TestLegacyIdentityWritersRefuse:
    @pytest.mark.parametrize(("writer", "kwargs"), _IDENTITY_WRITERS)
    def test_every_identity_writer_refuses(self, writer, kwargs):
        conn, _cur = _mock_conn()

        with pytest.raises(taxonomy.LegacyTaxonomyWriteRefused):
            getattr(taxonomy, writer)(
                conn, actor="actor@example.com", reason="proof", **kwargs
            )

    @pytest.mark.parametrize(("writer", "kwargs"), _IDENTITY_WRITERS)
    def test_the_refusal_never_reaches_the_store(self, writer, kwargs):
        """Refused BEFORE anything is read or written, not rolled back after."""
        conn, cur = _mock_conn()

        with pytest.raises(taxonomy.LegacyTaxonomyWriteRefused):
            getattr(taxonomy, writer)(
                conn, actor="actor@example.com", reason="proof", **kwargs
            )

        assert not cur.execute.called, cur.execute.call_args_list
        assert not conn.commit.called

    @pytest.mark.parametrize(("writer", "kwargs"), _IDENTITY_WRITERS)
    def test_the_refusal_names_the_two_gestures_and_never_a_store(self, writer, kwargs):
        """A message names what to do, in the order it has to happen."""
        conn, _cur = _mock_conn()

        with pytest.raises(taxonomy.LegacyTaxonomyWriteRefused) as excinfo:
            getattr(taxonomy, writer)(
                conn, actor="actor@example.com", reason="proof", **kwargs
            )

        message = str(excinfo.value)
        assert "Converge" in message, message
        assert "Master Data" in message, message
        for db_word in ("mdm_", "app.", "table", "column", "row"):
            assert db_word not in message, (db_word, message)

    def test_the_refusal_is_not_swallowed_by_an_existing_except(self):
        """It is NOT a `BusinessTaxonomyError`, and four product paths depend on that.

        `context_seed._ensure_domain_link` catches `BusinessTaxonomyError` and
        logs; so does the review rail. A cutover caught by one of those would read
        as "nothing to write" and the store would quietly stay the authority.
        """
        assert not issubclass(
            taxonomy.LegacyTaxonomyWriteRefused, taxonomy.BusinessTaxonomyError
        )
        assert not issubclass(taxonomy.LegacyTaxonomyWriteRefused, ValueError)

    def test_the_module_no_longer_writes_a_business_identity(self):
        """The permanent attack: a restored body goes red HERE, in the same edit.

        The module's STRING CONSTANTS are read from its AST -- SQL lives nowhere
        else in this module -- and any INSERT or UPDATE against the two identity
        stores or their two version tables fails the test. Reading the raw file
        instead would trip on the comments and docstrings that name those stores
        on purpose, to say what stopped writing them; whitespace is normalized
        because every statement here is a multi-line triple-quoted string.

        The f-string FRAGMENTS are read too, since story 49.2: every read here is
        now an f-string that interpolates `core.business_identity_catalogue`'s
        source, and a guard that only looked at plain constants would stop seeing
        a restored INSERT the day someone wrote it with a `{table}` in it.
        """
        import ast  # noqa: PLC0415
        import inspect  # noqa: PLC0415

        tree = ast.parse(inspect.getsource(taxonomy))
        statements: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                statements.append(" ".join(node.value.split()))
            elif isinstance(node, ast.JoinedStr):
                statements.append(
                    " ".join(
                        " ".join(part.value.split())
                        for part in node.values
                        if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    )
                )
        # Not vacuous, and the anchor MOVED with story 49.2. This module no longer
        # names the superseded tables at all: every read resolves through
        # `core.business_identity_catalogue`, which answers from
        # `app.master_data_nodes` first and holds the legacy row as the layer
        # below. So what proves the reads are still here is that the resolver is
        # named, and that the resolver still carries that layer -- anchoring on a
        # `FROM app.mdm_business_domains` that is deliberately gone would make
        # this guard demand the very dependency 49.2 removed.
        from core import business_identity_catalogue  # noqa: PLC0415

        assert "business_identity_catalogue" in inspect.getsource(taxonomy), (
            "no read of the business taxonomy found -- the guard reads nothing"
        )
        assert (
            "FROM app.mdm_business_domains" in business_identity_catalogue.DOMAIN_SOURCE
        ), "the resolver stopped holding the superseded layer below"

        writes = [
            sql
            for sql in statements
            for store in (
                "app.mdm_business_domains",
                "app.mdm_business_classifications",
                "app.mdm_business_domain_versions",
                "app.mdm_business_classification_versions",
            )
            if f"INSERT INTO {store}" in sql or f"UPDATE {store}" in sql
        ]
        assert not writes, writes

    def test_that_guard_can_actually_fail(self):
        """Controle negatif. A guard that cannot go red proves nothing.

        The predicate above is applied to the module's source WITH a restored
        INSERT appended. If this comes back empty, the test above is a green
        light wired to nothing -- which is how three guards in this repository
        were found to be measuring their own copy.
        """
        import ast  # noqa: PLC0415
        import inspect  # noqa: PLC0415

        rewired = "\n".join(
            [
                "",
                "",
                "def _rewired(cur):",
                '    cur.execute(',
                '        """',
                "        INSERT INTO app.mdm_business_domains (id, org_id)",
                "        VALUES (%s, %s)",
                '        """',
                "    )",
                "",
            ]
        )
        statements = [
            " ".join(node.value.split())
            for node in ast.walk(ast.parse(inspect.getsource(taxonomy) + rewired))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]

        assert [
            sql for sql in statements if "INSERT INTO app.mdm_business_domains" in sql
        ], "the guard does not see a writer that came back"

    def test_the_link_writers_stayed_and_that_is_the_ratified_line(self):
        """`create_link` is NOT cut, and the amendment says why in its own words.

        A link is not an identity -- the convergence deliberately does not move
        `app.mdm_business_links` -- so there is no Master Data gesture a refusal
        could name. And the amendment's own *Incomplete if* forbids the state
        cutting it would create: *"a withdrawn link cannot be made again, so one
        wrong click has no repair"*.
        """
        conn, cur = _mock_conn()
        cur.fetchone.return_value = (1,)

        # It gets past the refusal and into its own validation -- proof that the
        # cut did not take it. An unknown relation type is refused by `create_link`
        # itself, with a `BusinessTaxonomyError`, exactly as before.
        with pytest.raises(taxonomy.BusinessTaxonomyError, match="relation_type"):
            taxonomy.create_link(
                conn,
                org_id="org_01",
                project_id="proj_01",
                taxonomy_type="business_domain",
                taxonomy_id="bdm_01",
                target_type="datastream",
                target_id="ds_01",
                relation_type="invents-a-vocabulary",
                actor="actor@example.com",
                reason="proof",
            )

        assert callable(taxonomy.retire_link)
