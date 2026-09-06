"""Story 49.1 — the source-agnostic Governance read model.

What these tests actually hold in place:

* the server contract table is EQUAL to the client route registry, slug for slug;
* an undelivered owner is `unavailable`, never an empty success;
* an unknown lens does not fall back to the default one;
* an exact version belongs to its object or it is not found — it is never
  replaced by the current one.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.governance_read_model import (
    GOVERNANCE_SECTIONS,
    MAX_COLLECTION_ITEMS,
    GovernanceObjectNotFound,
    GovernanceUnknownRoute,
    compose_governance_collection,
    compose_governance_object,
    compose_governance_object_version,
)

from tests.support.navigation_source import navigation_source

REPO_ROOT = Path(__file__).resolve().parents[3]
NAVIGATION = REPO_ROOT / "ui" / "admin" / "src" / "shell" / "navigation.ts"

ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
MOMENT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


class _Cursor:
    """A cursor that answers by matching the table each query reads from."""

    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables
        self._rows: list[dict] = []
        self._columns: list[str] = []
        self.seen: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    #: Queries that AGGREGATE over a table rather than selecting its rows. They
    #: mention the same table names, so a plain substring match would hand them
    #: the row fixtures and produce a column set that does not exist. They answer
    #: nothing unless a test seeds them by their own marker.
    _AGGREGATE_MARKERS = (
        "BOOL_OR(",
        "AS anchor_id",
        "COUNT(*) AS total",
        "DISTINCT ON (record_id)",
    )

    def execute(self, query: str, params=None):
        self.seen.append((query, dict(params or {})))
        # Longest marker first, so a specific fixture key wins over the table
        # name it happens to contain.
        for table, rows in sorted(self._tables.items(), key=lambda item: -len(item[0])):
            if table in query:
                self._rows = rows
                break
        else:
            self._rows = []
        if self._rows and any(marker in query for marker in self._AGGREGATE_MARKERS):
            matched = any(
                table in query
                for table in self._tables
                if any(marker in table for marker in self._AGGREGATE_MARKERS)
            )
            if not matched:
                self._rows = []
        self._columns = list(self._rows[0].keys()) if self._rows else ["id"]

    @property
    def description(self):
        return [(name,) for name in self._columns]

    def fetchall(self):
        return [tuple(row[name] for name in self._columns) for row in self._rows]


def _conn(**tables: list[dict]) -> MagicMock:
    named = {
        {
            "domains": "app.mdm_business_domains",
            "classifications": "app.mdm_business_classifications",
            # Story 49.3 replaced the mutable canonical-field registry with the
            # versioned Semantic Model. The old key is kept pointing at the old
            # table so a test that still seeds it fails loudly instead of
            # silently matching nothing.
            "canonical_fields": "app.mdm_canonical_fields",
            "concepts": "app.semantic_concepts",
            "concept_versions": "app.semantic_concept_versions",
            "views": "app.semantic_views",
            "view_versions": "app.semantic_view_versions",
            "datastreams": "app.datastreams",
            "proposals": "app.mapping_proposals",
            "groups": "app.overlap_groups",
            "capability": "app.project_configuration_owner_references",
            "lineage": "app.datastream_mapping_publication_log",
            "confirmations": "app.publication_confirmations",
            "audit": "app.audit_log",
            # Story 49.5: the three Evidence lenses read the immutable reference
            # index, not three unrelated owner tables.
            "evidence_records": "app.evidence_records",
            "evidence_links": "app.evidence_links",
            "evidence_correlations": "app.evidence_correlations",
            "watermarks": "app.evidence_index_watermarks",
            # Story 60.1: the client's own value mapping tables.
            "value_tables": "app.value_mapping_tables",
            # Story 60.3: the cleanup rules, applied at READ.
            "cleanup_rules": "app.cleanup_rules",
            # Story 60.5, migration 242: the two immutable ledgers. Their names
            # do not contain the names of their parents, so the matcher above
            # cannot hand one the other's fixture.
            "value_table_versions": "app.value_mapping_table_versions",
            "cleanup_rule_versions": "app.cleanup_rule_versions",
            # Story 49.2, the Products and Activities collections. The marker is
            # a COLUMN ALIAS and not a table name on purpose: the instance query
            # reads `app.master_data_registries` and `app.master_data_nodes` in
            # the same statement, so any table-name key would also match the
            # registries lens' own query and hand one the other's fixture.
            "md_instances": "declared_object_kind",
        }[key]: value
        for key, value in tables.items()
    }
    cursor = _Cursor(named)
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.governance_cursor = cursor
    return connection


def _evidence_record_row(record_id: str, **overrides) -> dict:
    """One row of `app.evidence_records`, in the exact column set the read selects."""
    row = {
        "id": record_id,
        "record_kind": "evidence_trace",
        "producer": "data_execution",
        "owner_workspace": "data",
        "owner_object_type": "datastream-execution",
        "owner_object_id": "audit_1",
        "owner_version_id": None,
        "is_anchor": True,
        "occurred_at": MOMENT,
        "observed_at": MOMENT,
        "indexed_at": MOMENT,
        "integrity_hash": "c" * 64,
        "redaction_class": "reference_only",
    }
    row.update(overrides)
    return row


def _domain_row(object_id: str, **overrides) -> dict:
    row = {
        "id": object_id,
        "slug": "commerce",
        "name": "Commerce",
        "description": "Sales and revenue",
        "owner": "owner@example.com",
        "status": "active",
        "created_at": MOMENT,
        "updated_at": MOMENT,
        "archived_at": None,
        "version_count": 3,
        "version_refs": _domain_version_refs(object_id, (3, 2, 1)),
        "latest_version": 3,
        "latest_changed_at": MOMENT,
        "classification_count": 4,
        "used_by_count": 2,
    }
    row.update(overrides)
    return row


def _object_instance_row(node_id: str | None, object_kind: str = "product", **overrides) -> dict:
    """One row of the Story 49.2 instance query, in the exact column set it selects.

    `node_id=None` is the row a DECLARED registry with no instance produces: the
    query LEFT JOINs its nodes precisely so that "never declared" and "declared
    and holding nothing" stay two different answers.
    """
    row = {
        "registry_id": "mdreg_1",
        "declared_object_kind": object_kind,
        "registry_label": "Products",
        "registry_state": "active",
        "version_scope": "node",
        "registry_scope": "project",
        "live_source_count": 1,
        "registry_version_count": 0,
        "node_id": node_id,
        "node_label": "Signature Blend 250g",
        "node_kind": object_kind,
        "node_created_by": "owner@example.com",
        "node_updated_at": MOMENT,
        "current_version_id": "mdver_1",
        "current_version_number": 2,
        "current_published_at": MOMENT,
        "latest_version_status": "current",
        "node_version_count": 2,
        "used_by_count": 3,
        "alias_count": 4,
    }
    row.update(overrides)
    return row


def _unreadable_conn() -> MagicMock:
    """A connection whose every read raises. An unreadable store is `unavailable`,
    and it is the ONLY thing that legitimately is."""

    class _Broken:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("master data store is unreachable")

    connection = MagicMock()
    connection.cursor.return_value = _Broken()
    return connection


def _domain_version_refs(object_id: str, numbers: tuple[int, ...] = (3, 2)) -> list[dict]:
    """The refs `_BUSINESS_DOMAINS` aggregates in its own LATERAL, newest first.

    A row is served WITH them: a version count the lens could compose and a refs
    list it could not is the impossible state `_counted_facet` now refuses, so a
    fixture that omitted this column was describing a query that does not exist.
    """
    return [
        {
            "object_type": "business-domain-version",
            "id": f"{object_id}:{number}",
            "version": number,
            "state": "active" if number == max(numbers) else "previous",
            "recorded_at": f"2026-07-{27 + number:02d}T09:00:00Z",
            "recorded_by": "person@example.com",
        }
        for number in numbers
    ]


def _capability_row(object_id: str, version_id: str, **overrides) -> dict:
    row = {
        "object_id": object_id,
        "object_type": "registry",
        "version_id": version_id,
        "capability_key": "country",
        "owner_reference": {},
        "evidence_hash": "a" * 64,
        "configuration_version_id": "pcv_1",
        "carried_forward": False,
        "created_at": MOMENT,
        "capability_state": "ready",
        "availability": "optional",
        "active_version_id": "pcv_1",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Contract parity with the client route registry
# ---------------------------------------------------------------------------


def _section_lens_block(rest: str) -> str:
    """Everything up to the END of this `section(...)` call.

    THE CUT TOKEN USED TO BE `]),` ALONE, and that reads a section which declares
    no query state. `section()` takes a sixth argument, so a section that
    declares one ends `], EVIDENCE_QUERY),` — and the cut then ran past the end
    of the call and swallowed the NEXT section's lenses (story 60.4 measured it:
    `semantic-model` came back with the four `controls-quality` lenses appended).
    Evidence escaped it only because it is declared last in the block, which is
    luck rather than a property.

    Both endings are read and the earliest one wins. Nothing else changes: the
    assertions below are untouched, and they still refuse a lens, an object type
    or a source this repository did not declare on purpose.
    """
    ends = [found.start() for found in re.finditer(r"\]\)|\],\s*[A-Z_]+\)", rest)]
    return rest[: ends[0]] if ends else rest


def _client_governance_registry() -> dict[str, dict]:
    """Read the four Governance sections straight out of `navigation.ts`.

    Parsing the real file rather than restating it is the point: a table copied
    into a test drifts silently, which is exactly the failure mode a parity test
    is supposed to catch.
    """
    source = navigation_source()
    alias = {
        "MASTER_DATA_TABS": re.findall(
            r'"([a-z-]+)"',
            re.search(r"const MASTER_DATA_TABS = \[([^\]]*)\]", source).group(1),
        )
    }
    # AD-42 : chaque espace declare ses sections dans son fichier. Trancher le
    # texte joint entre deux cles ne marche plus -- et l ordre alphabetique des
    # fichiers rendait meme cette tranche VIDE, donc verte par accident.
    block = (
        REPO_ROOT / "ui" / "admin" / "src" / "shell" / "navigation" / "governance.ts"
    ).read_text(encoding="utf-8")
    sections: dict[str, dict] = {}
    for match in re.finditer(r'section\("([a-z-]+)", "([^"]+)", \[', block):
        slug = match.group(1)
        body = block[match.end() :]
        depth, cut = 1, 0
        while depth:
            char = body[cut]
            depth += {"[": 1, "]": -1}.get(char, 0)
            cut += 1
        objects_src, rest = body[: cut - 1], body[cut:]
        sections[slug] = {
            "label": match.group(2),
            "objects": [
                {
                    "type": found.group(1),
                    "tabs": alias.get(found.group(2), re.findall(r'"([a-z-]+)"', found.group(2))),
                    "default_tab": found.group(3),
                }
                # THE THREE KEYS NEED NOT BE ADJACENT. This pattern demanded
                # `type`, `tabs` and `defaultTab` with nothing but whitespace
                # between them, so declaring a fourth key — or writing a comment
                # between two of them — made the whole contract invisible and
                # dropped the object from the parity check silently. Measured
                # 2026-09-01 when `tracked-entity` declared its displayed
                # `label`. `[^{}]` is what bounds the reach: object contracts are
                # separated by braces, so the walk cannot run into its neighbour
                # and pair one object's `type` with the next one's `tabs`.
                for found in re.finditer(
                    r'type:\s*"([a-z-]+)",[^{}]*?tabs:\s*(MASTER_DATA_TABS|\[[^\]]*\]),'
                    r'[^{}]*?defaultTab:\s*"([a-z-]+)"',
                    objects_src,
                )
            ],
            "lenses": [
                slug_match
                for slug_match in re.findall(
                    r'\{ slug: "([a-z-]+)", label: "[^"]+" \}',
                    _section_lens_block(rest),
                )
            ],
        }
    return sections


def test_server_contracts_match_the_client_route_registry_exactly():
    client = _client_governance_registry()

    assert (
        list(client)
        == list(GOVERNANCE_SECTIONS)
        == [
            "master-data",
            "semantic-model",
            "controls-quality",
            "evidence",
        ]
    )
    for slug, contract in GOVERNANCE_SECTIONS.items():
        assert client[slug]["label"] == contract.label
        assert client[slug]["lenses"] == list(contract.lenses)
        for declared in client[slug]["objects"]:
            server = contract.object_contract(declared["type"])
            assert server is not None, declared["type"]
            assert declared["tabs"], (
                f"{declared['type']} parsed with no tab — the parity check is blind"
            )
            assert list(server.tabs) == declared["tabs"], declared["type"]
            assert server.default_tab == declared["default_tab"]
            assert server.default_tab in server.tabs
        assert len(client[slug]["objects"]) == len(contract.objects)

    # 20 lenses and 16 object types: Story 48.5 added the conditional
    # `competitor-registry` lens and its `tracked-entity` object to Master Data,
    # Story 60.1 added `value-tables` with its `value-mapping-table` object to the
    # Semantic Model, Story 60.3 added `cleanup-rules` with its `cleanup-rule`
    # object beside it, lot A1 of issue #68 added `canonical-fields` with its
    # `canonical-field` object -- the vocabulary six production modules validate
    # bindings against, which until then no screen anywhere listed -- and
    # 2026-08-16 added `metric-definitions` with its `metric-definition` object:
    # `metric_definition_upsert` (MCP) writes `app.metric_definitions` and
    # `resolve_declared_additivity` reads it on every render, so it decides
    # whether a metric may be summed across two days, and no lens listed it.
    assert sum(len(section.lenses) for section in GOVERNANCE_SECTIONS.values()) == 20
    assert sum(len(section.objects) for section in GOVERNANCE_SECTIONS.values()) == 16


def test_every_registered_lens_has_an_adapter_and_every_type_has_a_source():
    from core.governance_read_model import _LENS_ADAPTERS, _OBJECT_SOURCES

    for slug, contract in GOVERNANCE_SECTIONS.items():
        for lens in contract.lenses:
            assert (slug, lens) in _LENS_ADAPTERS, f"{slug}/{lens} has no adapter"
        for obj in contract.objects:
            sources = _OBJECT_SOURCES[(slug, obj.object_type)]
            assert sources, obj.object_type
            assert all(source in contract.lenses for source in sources)


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


def test_default_lens_is_used_when_none_is_named():
    envelope = compose_governance_collection(
        PROJECT, "master-data", _conn(domains=[_domain_row("bd_1")]), org_id=ORG
    )
    assert envelope["lens"] == "business-domains"
    assert envelope["default_lens"] == "business-domains"
    assert envelope["available_lenses"] == list(GOVERNANCE_SECTIONS["master-data"].lenses)
    assert envelope["schema_version"] == "governance-collection.v1"
    assert envelope["project_ref"]["id"] == PROJECT
    assert envelope["organization_ref"]["id"] == ORG


def test_unknown_lens_and_section_never_fall_back_to_the_default():
    with pytest.raises(GovernanceUnknownRoute):
        compose_governance_collection(PROJECT, "master-data", _conn(), lens="concepts", org_id=ORG)
    with pytest.raises(GovernanceUnknownRoute):
        compose_governance_collection(PROJECT, "master-data", _conn(), lens="not-real", org_id=ORG)
    with pytest.raises(GovernanceUnknownRoute):
        compose_governance_collection(PROJECT, "mapping", _conn(), org_id=ORG)


def test_business_domain_carries_identity_owner_status_versions_and_used_by():
    envelope = compose_governance_collection(
        PROJECT, "master-data", _conn(domains=[_domain_row("bd_1")]), org_id=ORG
    )
    item = envelope["items"][0]

    assert item["object_ref"] == {
        "type": "business-domain",
        "id": "bd_1",
        "label": "Commerce",
        "owner_href": {
            "surface": "project",
            "workspace": "governance",
            "section": "master-data",
            "global_surface": None,
            "global_section": None,
            "object_type": "business-domain",
            "object_id": "bd_1",
            "tab": None,
            "action": None,
            "version_id": None,
            "evidence_id": None,
        },
    }
    assert item["scope"] == "organization"
    assert item["lifecycle_status"] == "active"
    # THE COLLECTION COUNTS; IT DOES NOT LIST (49-2, 2026-09-01). A lens row that
    # knows how many consumers there are and cannot name them is `unavailable`
    # WITH its count -- never `available` with an empty list, which the workbench
    # read as "nothing depends on this object". The list is composed for the
    # object a person opens (`_master_data_used_by`).
    assert item["used_by"]["state"] == "unavailable"
    assert item["used_by"]["count"] == 2
    assert item["used_by"]["refs"] == []
    assert item["used_by"]["reason"]["code"] == "facet_refs_not_composed"
    assert item["versions"]["state"] == "available"
    assert item["versions"]["count"] == 3
    assert item["available_tabs"] == [
        "overview",
        "hierarchy",
        "mappings-aliases",
        "used-by",
        "versions",
    ]
    assert item["default_tab"] == "overview"
    assert envelope["evidence_as_of"] == "2026-07-30T09:00:00Z"


def test_an_unreadable_owner_is_unavailable_and_never_an_empty_success():
    """The property this test has always held, re-aimed at what still carries it.

    It used to iterate `products` and `activities` and assert that each named
    Story 49.2 as its owner. Both are living lenses now, and nothing in this
    module pends any more -- so the property moved to the only thing that still
    legitimately produces `unavailable`: a store that could not be read. An empty
    list there would claim "we looked, this Project has none", which is the false
    statement the whole module exists to refuse.
    """
    for section, lens, code in [
        ("master-data", "products", "products_store_unreadable"),
        ("master-data", "activities", "activities_store_unreadable"),
        ("semantic-model", "value-tables", "value_tables_store_unreadable"),
    ]:
        envelope = compose_governance_collection(
            PROJECT, section, _unreadable_conn(), lens=lens, org_id=ORG
        )
        assert envelope["coverage"]["state"] == "unavailable", (section, lens)
        assert envelope["items"] == []
        assert len(envelope["unavailable_reasons"]) == 1
        reason = envelope["unavailable_reasons"][0]
        assert reason["code"] == code
        assert "not a count of zero" in reason["message"]


def test_no_governance_reason_ever_names_a_release_state():
    """The class the two Story 49.2 stubs belonged to, closed for every lens.

    A Governance reason is read by someone who can act on it. A story number, a
    sprint, "not delivered" -- none of those is a gesture, and none of them is
    even true of the Project being looked at. This walks every registered lens
    with an empty store and holds the whole surface to that rule at once, so the
    next lens that reaches for the old sentence fails here rather than on screen.
    """
    forbidden = ("story ", "not been delivered", "not delivered", "undelivered", "sprint")
    seen = 0
    for section, contract in GOVERNANCE_SECTIONS.items():
        for lens in contract.lenses:
            envelope = compose_governance_collection(
                PROJECT, section, _conn(), lens=lens, org_id=ORG
            )
            for reason in envelope["unavailable_reasons"]:
                seen += 1
                message = reason["message"].casefold()
                for word in forbidden:
                    assert word not in message, (section, lens, reason["code"], word)
    # A gate that measured nothing is a gate that passes forever.
    assert seen > 0


def test_a_project_that_never_declared_the_object_kind_is_told_the_gesture():
    """An empty list says WHY, and names what fills it. Never a release state."""
    envelope = compose_governance_collection(
        PROJECT, "master-data", _conn(), lens="products", org_id=ORG
    )
    assert envelope["coverage"]["state"] == "empty"
    assert envelope["items"] == []
    reason = envelope["unavailable_reasons"][0]
    assert reason["code"] == "products_object_kind_not_declared"
    assert "Declare the object kind 'product'" in reason["message"]
    assert "Datastream" in reason["message"]

    envelope = compose_governance_collection(
        PROJECT, "master-data", _conn(), lens="activities", org_id=ORG
    )
    assert envelope["unavailable_reasons"][0]["code"] == "activities_object_kind_not_declared"
    assert "Declare the object kind 'activity'" in envelope["unavailable_reasons"][0]["message"]


def test_a_declared_registry_holding_nothing_is_not_the_same_sentence():
    """"Never declared" and "declared, fed, nothing identified yet" are two facts.

    Collapsing them sends an operator to declare a kind that already exists --
    the confusion `object_kind_registry.describe_object_kind` names when it
    answers `no_live_source_binding`.
    """
    fed = compose_governance_collection(
        PROJECT,
        "master-data",
        _conn(md_instances=[_object_instance_row(None)]),
        lens="products",
        org_id=ORG,
    )
    assert fed["coverage"]["state"] == "empty"
    reason = fed["unavailable_reasons"][0]
    assert reason["code"] == "products_registry_holds_no_instance"
    assert "no Product has been identified yet" in reason["message"]

    released = compose_governance_collection(
        PROJECT,
        "master-data",
        _conn(md_instances=[_object_instance_row(None, live_source_count=0)]),
        lens="products",
        org_id=ORG,
    )
    assert released["unavailable_reasons"][0]["message"] != reason["message"]
    assert "no source feeds it any more" in released["unavailable_reasons"][0]["message"]


def test_a_product_is_a_master_data_object_with_its_own_history():
    """Migration 143: Products version PER OBJECT, so the object is the instance
    and the `registry` stays what the `registries` lens already lists."""
    envelope = compose_governance_collection(
        PROJECT,
        "master-data",
        _conn(md_instances=[_object_instance_row("mdnode_1")]),
        lens="products",
        org_id=ORG,
    )
    item = envelope["items"][0]

    assert envelope["coverage"]["state"] == "available"
    assert envelope["unavailable_reasons"] == []
    assert item["object_ref"]["type"] == "master-data-object"
    assert item["object_ref"]["id"] == "mdnode_1"
    assert item["object_ref"]["label"] == "Signature Blend 250g"
    assert item["scope"] == "project"
    assert item["lifecycle_status"] == "active"
    assert item["active_version_ref"] == {
        "object_type": "master-data-object-version",
        "id": "mdver_1",
        "version": 2,
        "state": "active",
    }
    # Counted by the lens, listed by the workbench -- see the Business Domain
    # assertion above for the rule.
    assert (item["used_by"]["state"], item["used_by"]["count"]) == ("unavailable", 3)
    assert item["versions"]["count"] == 2
    assert item["summary"]["object_kind"] == "product"
    assert item["summary"]["registry_id"] == "mdreg_1"
    assert item["owner"]["parent_href"]["object_type"] == "registry"
    # The five Master Data tabs, unsplit: a Product answers the same questions a
    # classification does, which is why it is the same object type.
    assert item["available_tabs"] == [
        "overview",
        "hierarchy",
        "mappings-aliases",
        "used-by",
        "versions",
    ]


def test_a_product_with_no_version_of_its_own_says_declared_not_draft():
    """No draft exists, so the screen does not send a reader looking for one."""
    envelope = compose_governance_collection(
        PROJECT,
        "master-data",
        _conn(
            md_instances=[
                _object_instance_row(
                    "mdnode_2",
                    current_version_id=None,
                    current_version_number=None,
                    current_published_at=None,
                    latest_version_status=None,
                    node_version_count=0,
                    used_by_count=None,
                )
            ]
        ),
        lens="products",
        org_id=ORG,
    )
    item = envelope["items"][0]
    assert item["lifecycle_status"] == "declared"
    assert item["active_version_ref"] is None
    assert item["versions"]["state"] == "empty"
    # An absent count is never a zero: nobody asked, and the screen says so.
    assert item["used_by"]["state"] == "unavailable"


def test_a_product_resolves_at_level_three_through_its_own_lens():
    """`_load_object` scans the lens that owns the type. A Product absent from
    `products` would be openable by nobody, which is how a governed object gets
    declared over MCP and then reaches no screen."""
    envelope = compose_governance_object(
        PROJECT,
        "master-data",
        "master-data-object",
        "mdnode_1",
        _conn(md_instances=[_object_instance_row("mdnode_1")]),
        org_id=ORG,
    )
    assert envelope["object"]["object_ref"]["id"] == "mdnode_1"
    assert envelope["object"]["summary"]["object_kind"] == "product"
    # The base a rename must state travels for a Product too (review of d1fbdbb6,
    # 2026-08-30): without it every Product / Activity rename sent `null` and
    # was refused `version_conflict` with a reload that could never help.
    assert envelope["object"]["summary"]["current_version_id"] == "mdver_1"


def test_a_delivered_owner_with_nothing_to_show_is_empty_not_unavailable():
    envelope = compose_governance_collection(
        PROJECT, "controls-quality", _conn(), lens="conflicts", org_id=ORG
    )
    assert envelope["coverage"]["state"] == "empty"
    assert envelope["unavailable_reasons"] == []


def test_an_evidence_lens_that_never_indexed_says_so_instead_of_reading_as_empty():
    """Story 49.5 — the difference between "none" and "not looked at yet".

    A Project whose Evidence index has never run has no watermark. Reporting
    `empty` alone would claim nothing was ever audited here, which is a
    statement nobody made. The lens is empty AND the coverage says why.
    """
    envelope = compose_governance_collection(
        PROJECT, "evidence", _conn(), lens="audit-activity", org_id=ORG
    )
    assert envelope["coverage"]["state"] == "empty"
    assert envelope["coverage"]["index_state"] == "backfilling"
    assert any(
        reason["code"] == "index_backfilling" for reason in envelope["unavailable_reasons"]
    )
    assert envelope["next_cursor"] is None


@pytest.mark.parametrize(
    ("lens", "record_kind"),
    [
        ("lineage-provenance", "evidence_trace"),
        ("versions-approvals", "object_version"),
        ("audit-activity", "audit_event"),
    ],
)
def test_every_evidence_lens_declares_the_vocabulary_its_filters_are_validated_against(
    lens, record_kind
):
    """The Evidence filter bar had to retype three server lists to draw itself.

    `normalize_filters` refuses an unknown `owner_workspace`, an unknown
    `correlation_kind` and a `record_kind` belonging to another lens — and the
    envelope declared none of them, so `EvidenceCollection.tsx` kept a browser
    copy of all three. The copy is only ever as fresh as its last edit.
    """
    from core.evidence_index import CORRELATION_KINDS, OWNER_WORKSPACES

    envelope = compose_governance_collection(PROJECT, "evidence", _conn(), lens=lens, org_id=ORG)

    options = envelope["filter_options"]
    # IDENTITY with the validator's own tuples, not a list repeated here: a
    # second copy in this file would pass while the wire went stale.
    assert options["owner_workspaces"] == list(OWNER_WORKSPACES)
    assert options["correlation_kinds"] == list(CORRELATION_KINDS)
    # One kind, this lens's — the index raises for the other two.
    assert options["record_kinds"] == [record_kind]
    # Slugs, and nothing but: the English a person reads is the console's half.
    assert all(isinstance(value, str) for value in options["correlation_kinds"])


def test_the_evidence_vocabulary_is_declared_even_when_the_index_answered_nothing():
    """The bar must be able to UNDO the narrowing that emptied the screen.

    This Project has no watermark, so the lens reads `backfilling` and returns
    no row. A declaration that waited for rows would take the controls away at
    exactly the moment a person needs them to widen the question.
    """
    envelope = compose_governance_collection(
        PROJECT, "evidence", _conn(), lens="audit-activity", org_id=ORG
    )

    assert envelope["coverage"]["index_state"] == "backfilling"
    assert envelope["items"] == []
    assert envelope["filter_options"]["owner_workspaces"]
    assert envelope["filter_options"]["record_kinds"] == ["audit_event"]


def test_master_data_scopes_are_still_the_only_options_master_data_declares():
    """The Evidence vocabulary is not served to a section that never asked for
    it: Master Data declares the scopes it LOADED, which is a measurement."""
    envelope = compose_governance_collection(
        PROJECT, "master-data", _conn(domains=[_domain_row("bd_1")]), org_id=ORG
    )

    assert set(envelope["filter_options"]) == {"scopes"}


def test_collections_are_bounded_and_the_bound_is_reported_not_hidden():
    rows = [_domain_row(f"bd_{index}") for index in range(MAX_COLLECTION_ITEMS + 7)]
    envelope = compose_governance_collection(
        PROJECT, "master-data", _conn(domains=rows), org_id=ORG
    )

    assert len(envelope["items"]) == MAX_COLLECTION_ITEMS
    assert envelope["coverage"] == {
        "state": "available",
        "returned": MAX_COLLECTION_ITEMS,
        "total": MAX_COLLECTION_ITEMS + 7,
        "bound": MAX_COLLECTION_ITEMS,
    }
    assert envelope["next_cursor"] == str(MAX_COLLECTION_ITEMS)
    assert not any(
        reason["code"] == "collection_truncated" for reason in envelope["unavailable_reasons"]
    )


def test_master_data_filters_and_pages_on_the_server_owned_collection():
    rows = [
        _domain_row("bd_alpha_1", name="Alpha one"),
        _domain_row("bd_beta", name="Beta"),
        _domain_row("bd_alpha_2", name="Alpha two"),
    ]
    envelope = compose_governance_collection(
        PROJECT,
        "master-data",
        _conn(domains=rows),
        org_id=ORG,
        query={"q": "alpha", "cursor": "1", "limit": "1"},
    )

    assert [item["object_ref"]["id"] for item in envelope["items"]] == ["bd_alpha_2"]
    assert envelope["coverage"] == {
        "state": "available",
        "returned": 1,
        "total": 2,
        "bound": 1,
    }
    assert envelope["next_cursor"] is None
    assert envelope["applied_filters"] == {"q": "alpha"}
    assert envelope["filter_options"] == {"scopes": ["organization"]}
    assert envelope["unavailable_reasons"] == []


def test_master_data_rejects_a_stale_cursor():
    with pytest.raises(ValueError, match="cursor does not reference"):
        compose_governance_collection(
            PROJECT,
            "master-data",
            _conn(domains=[_domain_row("bd_only")]),
            org_id=ORG,
            query={"cursor": "50", "limit": "50"},
        )


def test_the_display_bound_never_becomes_an_existence_verdict():
    rows = [_domain_row(f"bd_{index}") for index in range(MAX_COLLECTION_ITEMS + 7)]
    envelope = compose_governance_object(
        PROJECT, "master-data", "business-domain", "bd_205", _conn(domains=rows), org_id=ORG
    )
    assert envelope["object"]["object_ref"]["id"] == "bd_205"


def test_audit_activity_exposes_no_provider_account_or_connection_reference():
    """Not filtered downstream — never selected, in either of the two queries.

    The indexing query (`evidence_index._AUDIT_SQL`) and the per-page detail
    query (`_AUDIT_SHAPE`) are the only two places audit columns are named. A
    later edit that widened either projection is what this asserts against.
    """
    from core.evidence_index import _AUDIT_DETAIL_SQL, _AUDIT_SQL
    from core.governance_read_model import _AUDIT_SHAPE

    for query in (_AUDIT_SQL, _AUDIT_SHAPE, _AUDIT_DETAIL_SQL):
        assert "provider_account" not in query
        assert "connection_ref" not in query
        assert "idempotency_key_hash" not in query
        assert "confirmation_reference_hash" not in query
        assert "a.metadata" not in query.replace("a.metadata->>'project_id'", "")

    envelope = compose_governance_collection(
        PROJECT,
        "evidence",
        _conn(
            evidence_records=[_evidence_record_row("evr_a1", record_kind="audit_event")],
            audit=[
                {
                    "id": "audit_1",
                    "identity": "person@example.com",
                    "action": "datastream.published",
                    "outcome": "success",
                    "resource_path": ["organization:org_EXAMPLE"],
                    "trace_id": "a" * 32,
                    "operation_id": "op_1",
                    "created_at": MOMENT,
                }
            ],
        ),
        lens="audit-activity",
        org_id=ORG,
    )
    body = json.dumps(envelope)
    assert "provider_account" not in body
    assert "connection_ref" not in body
    assert envelope["items"][0]["object_ref"]["type"] == "audit-event"
    # Activity is not provenance and not a version: only its own facet is filled.
    assert envelope["items"][0]["versions"]["state"] == "unavailable"
    assert envelope["items"][0]["evidence"]["state"] == "available"


def test_a_disabled_capability_contributes_no_registry_and_no_route():
    disabled = _capability_row("reg_1", "pcv_9", capability_state="disabled")
    connection = _conn(capability=[disabled])
    envelope = compose_governance_collection(
        PROJECT, "master-data", connection, lens="registries", org_id=ORG
    )
    assert envelope["items"] == []
    assert envelope["coverage"]["state"] == "empty"

    with pytest.raises(GovernanceObjectNotFound):
        compose_governance_object(
            PROJECT, "master-data", "registry", "reg_1", _conn(capability=[disabled]), org_id=ORG
        )


def test_reconciliation_reads_only_this_projects_scope():
    """It used to pin `_RECONCILIATION_RULE_SETS`, which nobody ran any more.

    Story 49.4 moved the reconciliation lens onto the governed Rule Sets and left
    the old `app.overlap_groups` query in the module. Measured 2026-08-16: this
    test was its ONLY remaining reader. It went green on a string, and said
    nothing about the query the lens actually serves -- the worst shape a guard
    can take, because it reads as proof. It reads the live one now.
    """
    import inspect

    from core.governance_read_model import _GOVERNED_RULE_SETS, _reconciliation_lens

    assert "PLATFORM" not in _GOVERNED_RULE_SETS
    assert "s.project_id = %(project_id)s" in _GOVERNED_RULE_SETS
    # And it is THIS query the lens runs -- otherwise the assertions above would
    # drift away from the served path exactly as the previous ones did.
    assert "_GOVERNED_RULE_SETS" in inspect.getsource(_reconciliation_lens)


# ---------------------------------------------------------------------------
# Objects and exact versions
# ---------------------------------------------------------------------------


def test_a_missing_object_is_not_found_rather_than_a_neighbour():
    with pytest.raises(GovernanceObjectNotFound):
        compose_governance_object(
            PROJECT,
            "master-data",
            "business-domain",
            "bd_absent",
            _conn(domains=[_domain_row("bd_1")]),
            org_id=ORG,
        )


def test_an_unregistered_object_type_is_an_unknown_route():
    with pytest.raises(GovernanceUnknownRoute):
        compose_governance_object(PROJECT, "master-data", "semantic-view", "x", _conn(), org_id=ORG)


def test_a_dq_monitor_that_does_not_exist_is_not_found_not_unavailable():
    """Story 49.4 changed which of two answers is correct here.

    While the DQ owner was undelivered, asking for a monitor had to answer
    `unavailable`: nothing could say whether it existed. Now that
    `app.dq_monitors` exists, a monitor this Project does not have is genuinely
    ABSENT, and saying "unavailable" would keep claiming ignorance the system no
    longer has. The distinction is the whole point of the unavailable state.
    """
    from core.governance_read_model import GovernanceObjectNotFound

    with pytest.raises(GovernanceObjectNotFound):
        compose_governance_object(
            PROJECT, "controls-quality", "dq-monitor", "dq_freshness", _conn(), org_id=ORG
        )


def test_a_business_domain_opens_an_exact_immutable_owner_version():
    refs = _domain_version_refs("bd_1")
    envelope = compose_governance_object_version(
        PROJECT,
        "master-data",
        "business-domain",
        "bd_1",
        "bd_1:2",
        _conn(domains=[_domain_row("bd_1", version_count=2, version_refs=refs)]),
        org_id=ORG,
    )
    assert envelope["schema_version"] == "governance-version.v1"
    assert envelope["requested_version_id"] == "bd_1:2"
    assert envelope["version_state"] == "stale"
    assert envelope["object"]["selected_version_ref"] == refs[1]
    assert envelope["object"]["active_version_ref"]["id"] == "bd_1:3"


def test_business_domain_exact_version_api_executes_the_real_read_model():
    from core.main import build_asgi_app
    from core.project_access import AccessDecision
    from starlette.testclient import TestClient

    conn = _conn(
        domains=[
            _domain_row(
                "bd_1",
                version_count=2,
                version_refs=_domain_version_refs("bd_1"),
            )
        ]
    )

    @contextmanager
    def scoped_connection():
        yield conn

    with (
        patch(
            "core.admin_api._check_auth",
            new=AsyncMock(return_value=(True, "person@example.com")),
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(True, "explicit_grant", "view", ORG),
        ),
        patch("core.db.get_connection", side_effect=scoped_connection),
    ):
        response = TestClient(build_asgi_app(), raise_server_exceptions=False).get(
            f"/api/projects/{PROJECT}/governance/master-data/objects/"
            "business-domain/bd_1/versions/bd_1:2"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["requested_version_id"] == "bd_1:2"
    assert body["object"]["selected_version_ref"]["id"] == "bd_1:2"
    assert body["version_state"] == "stale"


def test_an_exact_capability_version_resolves_and_names_its_own_state():
    rows = [
        _capability_row("reg_1", "pcv_3"),
        _capability_row("reg_1", "pcv_2"),
        _capability_row("reg_1", "pcv_1"),
    ]
    current = compose_governance_object_version(
        PROJECT, "master-data", "registry", "reg_1", "pcv_3", _conn(capability=rows), org_id=ORG
    )
    assert current["schema_version"] == "governance-version.v1"
    assert current["version_state"] == "current"
    assert current["object"]["selected_version_ref"]["id"] == "pcv_3"

    previous = compose_governance_object_version(
        PROJECT, "master-data", "registry", "reg_1", "pcv_1", _conn(capability=rows), org_id=ORG
    )
    assert previous["version_state"] == "stale"
    # Stale means "this exact one, and it is superseded" — not "here is the
    # current one instead".
    assert previous["object"]["selected_version_ref"]["id"] == "pcv_1"
    assert previous["object"]["active_version_ref"]["id"] == "pcv_3"


def test_a_version_of_another_object_is_not_found_and_nothing_is_substituted():
    rows = [_capability_row("reg_1", "pcv_3"), _capability_row("reg_2", "pcv_7")]
    with pytest.raises(GovernanceObjectNotFound):
        compose_governance_object_version(
            PROJECT, "master-data", "registry", "reg_1", "pcv_7", _conn(capability=rows), org_id=ORG
        )


def test_a_version_route_is_refused_for_a_type_that_declares_no_versions_tab():
    for section, object_type in [
        ("controls-quality", "control-case"),
        ("evidence", "evidence-trace"),
        ("evidence", "object-version"),
        ("evidence", "audit-event"),
    ]:
        with pytest.raises(GovernanceUnknownRoute):
            compose_governance_object_version(
                PROJECT, section, object_type, "obj_1", "ver_1", _conn(), org_id=ORG
            )


def test_governance_writes_nothing_and_creates_no_store_of_its_own():
    """Evidence is reference-only, and the read model is a reader.

    The failure this guards against is the one the architecture document names:
    a Governance surface that quietly becomes a second writer, or a second
    evidence ledger with its own table.
    """
    from core import governance_read_model, governance_surface_api

    source = Path(governance_read_model.__file__).read_text(encoding="utf-8")
    statements = source.upper()
    for forbidden in ("INSERT INTO", "UPDATE APP.", "DELETE FROM", "CREATE TABLE", "COMMIT"):
        assert forbidden not in statements, forbidden

    # The surface used to be asserted POST-free. Story 49.2 gave it one write --
    # the guarded Master Data node command -- and the invariant this test exists
    # for survives it: Governance must not become a SECOND WRITER, which means
    # it must not issue SQL against an owner's tables. It delegates instead, to
    # `core.master_data` through the durable-operation wrapper, so the audit and
    # outbox are the platform's and the owner keeps its own state.
    api = Path(governance_surface_api.__file__).read_text(encoding="utf-8")
    assert '"PATCH"' not in api and '"DELETE"' not in api
    api_statements = api.upper()
    for forbidden in ("INSERT INTO", "UPDATE APP.", "DELETE FROM", "CREATE TABLE"):
        assert forbidden not in api_statements, forbidden
    assert "run_node_command" in api  # delegation, not a local write
    assert "execute_operation" not in api  # and not its own operation, either

    # And no migration accompanies this story: the objects already have owners.
    migrations = sorted((REPO_ROOT / "infra" / "nango" / "migrations").glob("*.sql"))
    assert not any("governance_read_model" in path.name for path in migrations)


def test_evidence_references_carry_a_kind_owner_time_and_availability():
    envelope = compose_governance_collection(
        PROJECT,
        "evidence",
        _conn(
            evidence_records=[
                _evidence_record_row(
                    "evr_v1",
                    record_kind="object_version",
                    owner_workspace="governance",
                    owner_object_type="rule-set-version",
                    owner_object_id="grs_1",
                    owner_version_id="grsv_2",
                )
            ]
        ),
        lens="versions-approvals",
        org_id=ORG,
    )
    item = envelope["items"][0]
    reference = item["evidence"]["refs"][0]
    assert set(reference) >= {"evidence_id", "kind", "state", "recorded_at", "integrity_ref"}
    assert reference["kind"] == "object_version"
    # A confirmation is NOT the version identity. The version id is the owner's.
    assert item["summary"]["owner_version_id"] == "grsv_2"
    assert item["object_ref"]["id"] == "evr_v1"


def test_an_object_version_record_never_borrows_the_confirmation_as_its_identity():
    """The exact defect Story 49.5 removes.

    The previous lens used `publication_confirmations.id` as the Evidence object
    id AND as the version. A confirmation approves a version; conflating them
    makes the address unresolvable the moment a version is approved twice, or
    not at all.
    """
    from core.governance_read_model import _VERSION_SHAPE, _evidence_label

    row = _evidence_record_row(
        "evr_v9",
        record_kind="object_version",
        owner_object_type="rule-set-version",
        owner_object_id="grs_1",
        owner_version_id="grsv_9",
    )
    assert _evidence_label(row) == "rule set version grsv_9"
    # Approval is an EDGE on the version, resolved from the link table.
    assert "approved_by" in _VERSION_SHAPE


def test_every_query_is_scoped_by_the_authenticated_project_or_organization():
    from core.governance_read_model import _LENS_ADAPTERS

    # The Evidence lenses paginate server-side, so they bind three more
    # parameters. Every one of them is server-minted: a record kind decided by
    # the lens, a page bound, and the ids of the page just read. None of them
    # arrives from the caller unvalidated.
    # `object_kind` joins them for the same reason: the Products and Activities
    # lenses bind the literal their own module declares (`PRODUCT_OBJECT_KIND`),
    # never a kind the caller named. A kind that arrived from the request would
    # let an address choose which registry of the Project to read.
    bound = {"project_id", "org_id", "object_type", "object_kind", "record_kind", "limit", "ids"}

    for (section, lens), adapter in _LENS_ADAPTERS.items():
        connection = _conn()
        if section == "evidence":
            adapter(connection, PROJECT, ORG, None, {})
        else:
            adapter(connection, PROJECT, ORG, None)
        for query, params in connection.governance_cursor.seen:
            assert (
                "%(project_id)s" in query or "%(org_id)s" in query or "%(ids)s" in query
            ), (section, lens)
            assert set(params).issubset(bound), (section, lens, set(params) - bound)
            assert params.get("project_id", PROJECT) == PROJECT
            assert params.get("org_id", ORG) == ORG


# ---------------------------------------------------------------------------
# Story 60.1 -- the `value-tables` lens
# ---------------------------------------------------------------------------


def _value_table_row(object_id: str, **overrides) -> dict:
    """One row of `app.value_mapping_tables` in the exact projection of the read."""
    row = {
        "id": object_id,
        "org_id": ORG,
        "project_id": PROJECT,
        "scope_level": "PROJECT",
        "name": "Product lines",
        "description": "What the team calls a product line.",
        "created_by": "owner@example.com",
        "created_at": MOMENT,
        "updated_at": MOMENT,
        "current_version_id": "vmtv_2",
        "entry_count": 50,
        "assignment_count": 7,
        "datastream_count": 6,
        "sample_source_field": "a_raw_column",
    }
    row.update(overrides)
    return row


def _rule_version_row(version_id: str, object_id: str, number: int, **overrides) -> dict:
    """One row of a Story 60.5 ledger, in the exact projection of the read."""
    row = {
        "id": version_id,
        "object_id": object_id,
        "version_number": number,
        "status": "published",
        "content_hash": "d" * 64,
        "created_at": MOMENT,
        "created_by": "owner@example.com",
    }
    row.update(overrides)
    return row


def test_the_value_tables_lens_is_declared_in_the_three_places_at_once():
    """A lens declared in two of the three is unreachable, and looks delivered.

    `GovernanceCollection.tsx:538-546` records exactly that failure for
    `country-market`: rendered, never declared, so no tab could reach it.
    """
    from core.governance_read_model import _LENS_ADAPTERS, _OBJECT_SOURCES

    client = _client_governance_registry()
    assert "value-tables" in client["semantic-model"]["lenses"]
    assert "value-tables" in GOVERNANCE_SECTIONS["semantic-model"].lenses
    assert ("semantic-model", "value-tables") in _LENS_ADAPTERS
    assert _OBJECT_SOURCES[("semantic-model", "value-mapping-table")] == ("value-tables",)


def test_the_value_tables_lens_renders_one_item_per_table_with_its_real_counts():
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(value_tables=[_value_table_row("vmt_1")]),
        lens="value-tables",
        org_id=ORG,
    )
    assert envelope["coverage"]["state"] == "available"
    item = envelope["items"][0]
    assert item["object_ref"]["type"] == "value-mapping-table"
    assert item["object_ref"]["label"] == "Product lines"
    assert item["scope"] == "project"
    # The count of affected Datastreams is MEASURED, and it is the count of
    # Datastreams rather than of assignment rows.
    assert item["used_by"]["count"] == 6
    assert item["summary"]["entry_count"] == 50
    assert item["summary"]["assignment_count"] == 7
    # What it translates is DERIVED from the assignment, never typed twice.
    assert item["summary"]["translates"] == "a_raw_column -> Product lines"


def test_a_table_assigned_nowhere_says_none_rather_than_unavailable():
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(
            value_tables=[
                _value_table_row(
                    "vmt_1", assignment_count=0, datastream_count=0, sample_source_field=None
                )
            ]
        ),
        lens="value-tables",
        org_id=ORG,
    )
    item = envelope["items"][0]
    assert item["used_by"]["state"] == "empty"
    assert item["used_by"]["count"] == 0
    # `translates` is not invented from a table with no assignment.
    assert item["summary"]["translates"] is None


def test_a_project_with_no_value_table_is_EMPTY_and_the_lens_still_answered():
    envelope = compose_governance_collection(
        PROJECT, "semantic-model", _conn(value_tables=[]), lens="value-tables", org_id=ORG
    )
    assert envelope["coverage"]["state"] == "empty"
    assert envelope["items"] == []
    assert envelope["unavailable_reasons"] == []


def test_an_unreadable_store_is_UNAVAILABLE_and_never_an_empty_success():
    """« Vide » and « Cassé » are two different sentences (`_pending_story`)."""

    class _Exploding:
        def cursor(self):
            raise RuntimeError("the transformations library is unreachable")

    envelope = compose_governance_collection(
        PROJECT, "semantic-model", _Exploding(), lens="value-tables", org_id=ORG
    )
    assert envelope["coverage"]["state"] == "unavailable"
    assert envelope["items"] == []
    reason = envelope["unavailable_reasons"][0]
    assert reason["code"] == "value_tables_store_unreadable"
    assert "not a count of zero" in reason["message"]


def test_a_value_table_opens_as_its_own_object_without_a_concept_binding():
    """The object route resolves, and no mapping coverage is attached to it.

    A value table binds no Concept: decorating it with a `source_bindings`
    report would show a measurement about something else entirely.
    """
    envelope = compose_governance_object(
        PROJECT,
        "semantic-model",
        "value-mapping-table",
        "vmt_1",
        _conn(value_tables=[_value_table_row("vmt_1")]),
        org_id=ORG,
    )
    assert envelope["state"] == "available"
    assert envelope["object"]["object_ref"]["id"] == "vmt_1"
    assert envelope["object"]["available_tabs"] == ["overview", "used-by", "versions"]
    assert "source_bindings" not in envelope["object"]["summary"]


def test_a_value_table_opens_one_exact_version_of_its_own_history():
    """Story 60.5. The `versions` tab opens on rows migration 242 writes.

    It was withheld while 60.1 shipped no ledger, for the reason the withheld
    comment gave: a contracted tab over a history nothing writes opens on
    nothing. That reason expired the day the ledger landed.
    """
    contract = GOVERNANCE_SECTIONS["semantic-model"].object_contract("value-mapping-table")
    assert contract.supports_versions is True

    conn = _conn(
        value_tables=[_value_table_row("vmt_1")],
        value_table_versions=[
            _rule_version_row("vmtv_2", "vmt_1", 2),
            _rule_version_row("vmtv_1", "vmt_1", 1, content_hash="e" * 64),
        ],
    )
    envelope = compose_governance_object_version(
        PROJECT, "semantic-model", "value-mapping-table", "vmt_1", "vmtv_1", conn, org_id=ORG
    )
    assert envelope["requested_version_id"] == "vmtv_1"
    # An exact PREVIOUS version is readable, and it is NOT current. Saying so is
    # the whole point of pinning it.
    assert envelope["version_state"] == "stale"
    assert envelope["object"]["versions"]["count"] == 2
    assert envelope["object"]["selected_version_ref"]["evidence_hash"] == "e" * 64


def test_a_version_that_belongs_to_another_table_is_not_found():
    conn = _conn(
        value_tables=[_value_table_row("vmt_1")],
        value_table_versions=[_rule_version_row("vmtv_2", "vmt_1", 2)],
    )
    with pytest.raises(GovernanceObjectNotFound):
        compose_governance_object_version(
            PROJECT,
            "semantic-model",
            "value-mapping-table",
            "vmt_1",
            "vmtv_99",
            conn,
            org_id=ORG,
        )


def test_a_table_with_no_recorded_version_is_EMPTY_and_an_unreadable_ledger_is_not():
    """« No version has been recorded yet » and « the history could not be read »
    are two sentences, so they are two states here."""
    empty = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(value_tables=[_value_table_row("vmt_1", current_version_id=None)]),
        lens="value-tables",
        org_id=ORG,
    )
    assert empty["items"][0]["versions"]["state"] == "empty"
    assert empty["items"][0]["versions"]["count"] == 0

    from core.governance_read_model import _versions_facet

    unreadable = _versions_facet(None, "value-mapping-table", "vmt_1", None)
    assert unreadable["state"] == "unavailable"
    assert unreadable["refs"] == []


def test_an_unmeasured_assignment_count_is_unavailable_and_never_empty():
    """Arbitrage 5 at the PROJECTION, which is where it was reopened.

    The lens wrote `int(datastream_count or 0)`, so a count that came back NULL
    rendered as `used_by: empty, count 0` — "nothing depends on this" about a
    question nobody answered. The store under it already refuses that
    substitution; a projection that reopens it is the one a screen reads.
    """
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(value_tables=[_value_table_row("vmt_1", datastream_count=None)]),
        lens="value-tables",
        org_id=ORG,
    )
    item = envelope["items"][0]
    assert item["used_by"]["state"] == "unavailable"
    assert item["summary"]["datastream_count"] is None
    assert item["summary"]["impact_state"] == "unknown"
    # And the lens itself still answered: one unmeasured count is not an outage.
    assert envelope["coverage"]["state"] == "available"


def test_a_measured_zero_stays_a_measured_zero():
    """The other half of the same distinction, or the guard would be vacuous."""
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(value_tables=[_value_table_row("vmt_1", datastream_count=0)]),
        lens="value-tables",
        org_id=ORG,
    )
    item = envelope["items"][0]
    assert item["used_by"]["state"] == "empty"
    assert item["used_by"]["count"] == 0
    assert item["summary"]["impact_state"] == "known"


# ---------------------------------------------------------------------------
# Story 60.3 -- the `cleanup-rules` lens
# ---------------------------------------------------------------------------


def _cleanup_rule_row(object_id: str, **overrides) -> dict:
    """One row of `app.cleanup_rules` in the exact projection of the read."""
    row = {
        "id": object_id,
        "org_id": ORG,
        "project_id": PROJECT,
        "datastream_id": None,
        "name": "Drop the test campaigns",
        "source_field": "campaign_name",
        "rule_kind": "exclude_row",
        "pattern": "_TEST_",
        "enabled": True,
        "dry_run_state": "passed",
        "dry_run_detail": None,
        "created_by": "owner@example.com",
        "created_at": MOMENT,
        "updated_at": MOMENT,
        "current_version_id": "crlv_1",
        "datastream_count": 6,
    }
    row.update(overrides)
    return row


def test_the_cleanup_rules_lens_is_declared_in_the_three_places_at_once():
    """A lens declared in two of the three is unreachable, and looks delivered.

    `GovernanceCollection.tsx:538-546` records exactly that failure for
    `country-market`: rendered, never declared, so no tab could reach it.
    """
    from core.governance_read_model import _LENS_ADAPTERS, _OBJECT_SOURCES

    client = _client_governance_registry()
    assert "cleanup-rules" in client["semantic-model"]["lenses"]
    assert "cleanup-rules" in GOVERNANCE_SECTIONS["semantic-model"].lenses
    assert ("semantic-model", "cleanup-rules") in _LENS_ADAPTERS
    assert _OBJECT_SOURCES[("semantic-model", "cleanup-rule")] == ("cleanup-rules",)


def test_a_cleanup_rule_shows_its_condition_in_words_and_its_real_reach():
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(cleanup_rules=[_cleanup_rule_row("crule_1")]),
        lens="cleanup-rules",
        org_id=ORG,
    )
    assert envelope["coverage"]["state"] == "available"
    item = envelope["items"][0]
    assert item["object_ref"]["type"] == "cleanup-rule"
    assert item["object_ref"]["label"] == "Drop the test campaigns"
    # The condition is a SENTENCE, composed by the store from the same three
    # columns, so this list and the editing surface cannot disagree.
    assert item["summary"]["condition"] == (
        "Keeps a row only when campaign_name does not match `_TEST_`."
    )
    assert item["summary"]["source_field"] == "campaign_name"
    assert item["summary"]["reach"] == "project"
    assert item["used_by"]["count"] == 6
    # The measured effect is NOT here: it is a warehouse question, and a number
    # invented in a Governance list would be read as the rows this rule removes.
    assert "affected_rows" not in item["summary"]


def test_a_disabled_rule_says_so_rather_than_disappearing_from_the_lens():
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(cleanup_rules=[_cleanup_rule_row("crule_1", enabled=False)]),
        lens="cleanup-rules",
        org_id=ORG,
    )
    item = envelope["items"][0]
    assert item["lifecycle_status"] == "disabled"
    assert item["summary"]["enabled"] is False


def test_a_rule_bound_to_one_datastream_says_datastream_and_counts_one():
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(
            cleanup_rules=[
                _cleanup_rule_row("crule_1", datastream_id="ds_1", datastream_count=1)
            ]
        ),
        lens="cleanup-rules",
        org_id=ORG,
    )
    item = envelope["items"][0]
    assert item["summary"]["reach"] == "datastream"
    assert item["used_by"]["count"] == 1


def test_an_unmeasured_reach_is_unavailable_and_never_a_zero():
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(cleanup_rules=[_cleanup_rule_row("crule_1", datastream_count=None)]),
        lens="cleanup-rules",
        org_id=ORG,
    )
    item = envelope["items"][0]
    assert item["used_by"]["state"] == "unavailable"
    assert item["summary"]["datastream_count"] is None
    assert item["summary"]["impact_state"] == "unknown"


def test_a_project_with_no_cleanup_rule_is_EMPTY_and_the_lens_still_answered():
    envelope = compose_governance_collection(
        PROJECT, "semantic-model", _conn(cleanup_rules=[]), lens="cleanup-rules", org_id=ORG
    )
    assert envelope["coverage"]["state"] == "empty"
    assert envelope["items"] == []
    assert envelope["unavailable_reasons"] == []


def test_an_unreadable_cleanup_store_is_UNAVAILABLE_and_never_an_empty_success():
    """« Vide » and « Cassé » are two different sentences, and a lens without an
    owner answers `unavailable` (`governance_read_model.py:_pending_story`)."""

    class _Exploding:
        def cursor(self):
            raise RuntimeError("the transformation library is unreachable")

    envelope = compose_governance_collection(
        PROJECT, "semantic-model", _Exploding(), lens="cleanup-rules", org_id=ORG
    )
    assert envelope["coverage"]["state"] == "unavailable"
    assert envelope["items"] == []
    reason = envelope["unavailable_reasons"][0]
    assert reason["code"] == "cleanup_rules_store_unreadable"
    assert "not a count of zero" in reason["message"]


def test_a_cleanup_rule_opens_as_its_own_object_with_its_version_history():
    conn = _conn(
        cleanup_rules=[_cleanup_rule_row("crule_1")],
        cleanup_rule_versions=[
            _rule_version_row("crlv_2", "crule_1", 2),
            _rule_version_row("crlv_1", "crule_1", 1, content_hash="f" * 64),
        ],
    )
    envelope = compose_governance_object(
        PROJECT, "semantic-model", "cleanup-rule", "crule_1", conn, org_id=ORG
    )
    assert envelope["state"] == "available"
    assert envelope["object"]["object_ref"]["id"] == "crule_1"
    assert envelope["object"]["available_tabs"] == ["overview", "used-by", "versions"]
    contract = GOVERNANCE_SECTIONS["semantic-model"].object_contract("cleanup-rule")
    assert contract.supports_versions is True
    versions = envelope["object"]["versions"]
    assert versions["state"] == "available"
    assert versions["count"] == 2
    # The CURRENT one is read from the pointer, not from the position: a rule
    # edited back to a body it already carried points at an older version.
    assert [ref["state"] for ref in versions["refs"]] == ["previous", "active"]
    assert versions["refs"][1]["id"] == "crlv_1"


# ===========================================================================
# Canonical Fields -- lot A1 of issue #68
#
# The vocabulary six production modules validate every mdm-bound binding
# against. It held ZERO rows at both scopes and no lens listed it, so the
# closed enumeration a Template validates against was closed on nothing.
# ===========================================================================


def _canonical_field_row(field_id: str, **overrides) -> dict:
    """One row in the exact column order `list_visible_canonical_fields` selects."""
    row = {
        "id": field_id,
        "project_id": None,
        "canonical_name": "cost",
        "concept_kind": "metric",
        "value_type": "money",
        "aggregation": "sum",
        "non_additive": False,
        "unit": None,
        "object_kind": None,
        "description": None,
        "dictionary_field_name": None,
        "status": "active",
    }
    row.update(overrides)
    return row


def test_the_canonical_fields_lens_is_registered_on_both_sides_and_has_an_adapter():
    """Declared in the route registry AND served, or it is a lens nothing reaches.

    The failure mode this asserts against is the one `GovernanceCollection.tsx`
    records for `country-market`: a lens rendered by a screen and declared
    nowhere, which no tab could reach. Here the risk runs the other way -- a
    contract declared on one side only -- and the parity test above catches the
    registry halves while this one catches the adapter.
    """
    from core.governance_read_model import _LENS_ADAPTERS, _OBJECT_SOURCES

    client = _client_governance_registry()
    assert "canonical-fields" in client["semantic-model"]["lenses"]
    assert "canonical-fields" in GOVERNANCE_SECTIONS["semantic-model"].lenses
    assert ("semantic-model", "canonical-fields") in _LENS_ADAPTERS
    assert _OBJECT_SOURCES[("semantic-model", "canonical-field")] == ("canonical-fields",)


def test_a_canonical_field_carries_the_scope_that_decides_who_may_change_it():
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(
            canonical_fields=[
                _canonical_field_row("mdm_PLATFORM"),
                _canonical_field_row(
                    "mdm_PROJECT",
                    project_id=PROJECT,
                    canonical_name="recipe",
                    concept_kind="dimension",
                    value_type="string",
                    aggregation=None,
                    object_kind="video",
                ),
            ]
        ),
        lens="canonical-fields",
        org_id=ORG,
    )
    assert envelope["coverage"]["state"] == "available"
    scopes = {item["object_ref"]["id"]: item["scope"] for item in envelope["items"]}
    assert scopes == {"mdm_PLATFORM": "platform", "mdm_PROJECT": "project"}

    project_field = next(
        item for item in envelope["items"] if item["object_ref"]["id"] == "mdm_PROJECT"
    )
    assert project_field["object_ref"]["label"] == "recipe"
    assert project_field["summary"]["concept_kind"] == "dimension"
    assert project_field["summary"]["value_type"] == "string"
    assert project_field["summary"]["object_kind"] == "video"
    # NOTHING is writable from this lens. An `allowed_actions` entry would offer
    # a gesture whose only writer refuses a platform scope by name.
    assert project_field["allowed_actions"] == []


def test_a_canonical_field_reports_its_three_facets_as_unavailable_not_as_zero():
    """There is no version ledger, no used-by count and no evidence adapter.

    `empty` would answer "the count is none", which was never observed: nothing
    records which module used which field id, and the table is
    mutable-with-audit rather than versioned.
    """
    envelope = compose_governance_collection(
        PROJECT,
        "semantic-model",
        _conn(canonical_fields=[_canonical_field_row("mdm_ONE")]),
        lens="canonical-fields",
        org_id=ORG,
    )
    item = envelope["items"][0]
    assert item["used_by"]["state"] == "unavailable"
    assert item["versions"]["state"] == "unavailable"
    assert item["evidence"]["state"] == "unavailable"
    assert item["active_version_ref"] is None


def test_an_empty_vocabulary_is_empty_and_an_unreadable_one_is_unavailable():
    """The two sentences this screen exists to keep apart.

    Zero rows is what every Project has today, so "nobody has declared a field"
    must never be rendered with the words of "I could not read the vocabulary".
    """
    empty = compose_governance_collection(
        PROJECT, "semantic-model", _conn(canonical_fields=[]), lens="canonical-fields", org_id=ORG
    )
    assert empty["coverage"]["state"] == "empty"
    assert empty["items"] == []
    assert empty["unavailable_reasons"] == []

    class _Exploding:
        def cursor(self):
            raise RuntimeError("the canonical registry is unreachable")

    broken = compose_governance_collection(
        PROJECT, "semantic-model", _Exploding(), lens="canonical-fields", org_id=ORG
    )
    assert broken["coverage"]["state"] == "unavailable"
    assert broken["items"] == []
    reason = broken["unavailable_reasons"][0]
    assert reason["code"] == "canonical_fields_unreadable"
    assert "not a count of zero" in reason["message"]


def test_a_canonical_field_opens_as_its_own_object_on_its_honest_tabs():
    """`definition` and `lineage` -- and still no `versions`, still no `used-by`.

    A `versions` tab would promise a history nothing writes -- the defect story
    60.5 repaired for the two transformation families -- and a `used-by` tab
    would promise a count nothing records: the modules that validate a binding
    against a field id record nothing about which ones they used.

    `lineage` joined them on 2026-08-17 because the OPPOSITE direction is
    recorded and was already served: `dimension_lineage.get_fed_by` composes,
    from the connector manifests and the project's plan versions, which source
    columns FEED a canonical dimension. The audit of that day measured the
    defect this repairs -- the route shipped and no screen anywhere called it.
    The rule is unchanged, and this test is what applies it: a tab is contracted
    when an owner answers its question, and not before.
    """
    envelope = compose_governance_object(
        PROJECT,
        "semantic-model",
        "canonical-field",
        "mdm_ONE",
        _conn(canonical_fields=[_canonical_field_row("mdm_ONE")]),
        org_id=ORG,
    )
    assert envelope["state"] == "available"
    assert envelope["object"]["object_ref"]["id"] == "mdm_ONE"
    assert envelope["object"]["available_tabs"] == ["definition", "lineage"]
    assert envelope["object"]["default_tab"] == "definition"
    contract = GOVERNANCE_SECTIONS["semantic-model"].object_contract("canonical-field")
    assert contract.supports_versions is False
    # The stable identifier travels beside the label: `lineage` asks `get_fed_by`
    # for a canonical DIMENSION, and keying that read on a display label would
    # break the moment a client renames the field.
    assert envelope["object"]["summary"]["canonical_name"] is not None


# ===========================================================================
# The lower declaring store, visible at last (2026-08-16)
# ===========================================================================


def _metric_definitions(monkeypatch, *, resolved, concept_names=(), raises=False):
    """Serve the two readers this lens composes, and nothing else."""
    from core.governance_read_model import _metric_definitions_lens

    # Both readers take the caller's connection since 2026-08-30 (the lens used
    # to read through a connection of its own and could not see what its
    # caller had not committed); the doubles accept it like the real ones.
    def _resolve(project_id, conn=None):
        if raises:
            raise RuntimeError("the definition store did not answer")
        return resolved

    monkeypatch.setattr("core.metric_semantics.resolve_metric_definitions", _resolve)
    monkeypatch.setattr(
        "core.metric_semantics._load_declared_additivity_rows",
        lambda project_id, conn=None: [(name, "additive") for name in concept_names],
    )
    return _metric_definitions_lens(object(), "proj_EXAMPLE", "org_EXAMPLE", None)


_ROLLUP = {
    "id": "metdef_EXAMPLE",
    "canonical_name": "revenue",
    "display_name": "Revenue",
    "aggregation_type": "sum",
    "additive": True,
    "non_additive_dimensions": [],
    "scope_level": "PROJECT",
    "project_id": "proj_EXAMPLE",
    "certified": True,
}


def test_a_curated_definition_is_listed_where_governance_can_see_it(monkeypatch):
    """THE DEFECT THIS LENS EXISTS FOR.

    `metric_definition_upsert` (MCP) writes `app.metric_definitions`, and
    `resolve_declared_additivity` reads it on every render -- it decides whether a
    metric may be summed across two days. No lens listed it, so a person reading
    Governance believed they saw everything that governs an aggregation.
    """
    result = _metric_definitions(monkeypatch, resolved={"revenue": _ROLLUP})

    assert result.state == "available"
    assert len(result.items) == 1
    item = result.items[0]
    assert item["object_ref"]["type"] == "metric-definition"
    assert item["summary"]["aggregation_type"] == "sum"
    assert item["summary"]["governs"] is True


def test_a_definition_a_published_concept_shadows_says_it_governs_nothing(monkeypatch):
    """Listing it without saying so would be the same half-truth as hiding it.

    `resolve_declared_additivity` gives the Semantic Model precedence, so a
    definition whose metric a published Concept already carries is READ PAST. A
    person seeing it in a list would think it still decides how the metric sums.
    """
    result = _metric_definitions(
        monkeypatch, resolved={"revenue": _ROLLUP}, concept_names=("revenue",)
    )

    assert result.items[0]["summary"]["governs"] is False
    assert result.items[0]["summary"]["shadowed_by_concept"] is True


def test_it_is_never_presented_as_a_versioned_concept(monkeypatch):
    """A mutable row with no ledger and no formula is not a Concept.

    Every version-shaped facet answers `unavailable` -- "no owner answers this" --
    and never `empty`, which would claim the answer is none.
    """
    result = _metric_definitions(monkeypatch, resolved={"revenue": _ROLLUP})
    item = result.items[0]

    assert item["active_version_ref"] is None
    assert item["versions"]["state"] == "unavailable"
    assert item["used_by"]["state"] == "unavailable"
    assert item["evidence"]["state"] == "unavailable"


def test_an_unreadable_store_is_unavailable_and_never_an_empty_list(monkeypatch):
    """"Nobody curated a definition" and "I could not read the store" are opposite."""
    result = _metric_definitions(monkeypatch, resolved={}, raises=True)

    assert result.state == "unavailable"
    assert result.reason["code"] == "metric_definitions_unreadable"
    assert "not a count of zero" in result.reason["message"]


def test_the_cascade_is_the_readers_so_an_overridden_row_is_not_listed_twice(monkeypatch):
    """`resolve_metric_definitions` already applies PROJECT > ORG > PLATFORM.

    Listing a PLATFORM row a PROJECT row overrides would show a definition that
    decides nothing -- and re-implementing the cascade here would be a second
    answer to "what is in force".
    """
    result = _metric_definitions(monkeypatch, resolved={"revenue": _ROLLUP})
    assert [item["summary"]["canonical_name"] for item in result.items] == ["revenue"]


def test_every_name_the_module_logs_through_is_defined():
    """`logger` was used and never defined. Found by ruff F821 on 2026-08-16.

    Present at HEAD, not introduced: the `except Exception` around the concept
    shadow called `logger.warning` on a name the module did not have. A read
    failure therefore left the soft path and became a `NameError` -- raised from
    inside the handler written to stop exactly that. The lens crashed on the one
    case it had a plan for.

    Asserted on the SOURCE rather than on one call site, so a second handler
    added tomorrow is covered without anyone remembering this file.
    """
    import ast
    import inspect
    import logging

    from core import governance_read_model as grm

    assert isinstance(grm.logger, logging.Logger)
    assert grm.logger.name == "core.governance_read_model"

    tree = ast.parse(inspect.getsource(grm))
    logged_through = {
        node.value.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
    } | {
        node.func.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.attr in ("debug", "info", "warning", "error", "exception")
    }
    undefined = sorted(name for name in logged_through if not hasattr(grm, name))
    assert not undefined, (
        f"{undefined} is logged through and the module does not define it -- a "
        "NameError raised from inside the handler meant to soften a failure"
    )


# ---------------------------------------------------------------------------
# No governed object declares its fan-out unknowable -- governance.md [1]
# ---------------------------------------------------------------------------


def test_a_capability_object_counts_the_datastreams_it_reaches():
    """It answered `unavailable` about a question that has an owner and an answer.

    `_semantic_view_used_by` closed this class for one type on its own, and its
    docstring names the defect: `used_by` hardcoded to `_facet("unavailable")`
    while eight other governed objects in the module computed it. The Epic 48
    capability objects were the type it did not reach --
    `app.datastream_capability_proposals` records which Datastream a capability
    was compiled into, project-scoped, and nobody was asking.
    """
    from core.governance_read_model import _capability_object

    rows = [
        {
            "object_id": "obj_1",
            "version_id": "ver_1",
            "capability_key": "country",
            "configuration_version_id": "cfg_1",
            "capability_state": "enabled",
        }
    ]
    item = _capability_object(
        rows, section="master-data", object_type="registry", project_id=PROJECT, used_by_count=3
    )
    # The count is real and the consumers are Datastreams this composer does not
    # enumerate, so the facet says both: three, and not listed here.
    assert (item["used_by"]["state"], item["used_by"]["count"]) == ("unavailable", 3)
    assert item["used_by"]["reason"]["code"] == "facet_refs_not_composed"


def test_a_capability_reached_by_no_datastream_is_empty_and_not_unavailable():
    """Zero is the store answering. It is not the store failing to answer."""
    from core.governance_read_model import _capability_object

    rows = [
        {
            "object_id": "obj_1",
            "version_id": "ver_1",
            "capability_key": "country",
            "configuration_version_id": "cfg_1",
            "capability_state": "enabled",
        }
    ]
    item = _capability_object(
        rows, section="master-data", object_type="registry", project_id=PROJECT, used_by_count=0
    )
    assert item["used_by"]["state"] == "empty"
    assert item["used_by"]["count"] == 0

    # And only a failed read keeps the old sentence.
    unreadable = _capability_object(
        rows, section="master-data", object_type="registry", project_id=PROJECT, used_by_count=None
    )
    assert unreadable["used_by"]["state"] == "unavailable"


#: The object types whose `used_by` is `unavailable` ON PURPOSE, each because the
#: product has no owner for the question -- not because nobody asked. Both say so
#: in their own docstring, and both are mutable-with-audit rows rather than
#: versioned objects: a canonical field (migration 032) and a curated metric
#: definition. `unavailable` means "no owner answers this"; `empty` would mean
#: "the answer is none", which was never observed for either.
_DELIBERATELY_UNANSWERED = ("_canonical_field", "_metric_definition")


def test_only_the_two_documented_objects_declare_their_fan_out_unanswerable():
    """A hardcoded `unavailable` used-by is how this defect keeps coming back.

    Twice already: `_semantic_view_used_by` closed it for Semantic Views, and the
    Epic 48 capability objects were still declaring unknowable a question
    `app.datastream_capability_proposals` answers. Both now go through
    `_counted_facet`, so a measured zero and an unreadable count cannot collapse
    into one sentence.

    The two below are NOT that defect and must not be "fixed" into a lie: their
    objects genuinely have no used-by owner, and each says why where it is. A
    THIRD one appearing is the regression this guard exists for -- it means
    someone declared a question unanswerable instead of asking it.
    """
    import ast
    import inspect

    from core import governance_read_model as grm

    tree = ast.parse(inspect.getsource(grm))
    declaring: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
                continue
            if call.func.id != "_facet" or not call.args:
                continue
            first = call.args[0]
            if not (isinstance(first, ast.Constant) and first.value == "unavailable"):
                continue
            parent_kw = [
                kw
                for kw in ast.walk(node)
                if isinstance(kw, ast.keyword) and kw.arg == "used_by" and kw.value is call
            ]
            if parent_kw:
                declaring.append(node.name)

    assert sorted(set(declaring)) == sorted(_DELIBERATELY_UNANSWERED), (
        "a governed object declares its used-by unanswerable. If the question has "
        "an owner, count it through _counted_facet; if it genuinely has none, say "
        f"why in the function and add it here: {sorted(set(declaring))}"
    )


# ---------------------------------------------------------------------------
# The Semantic Model declares the gestures it can honour — 2026-08-18
#
# Before this section, `allowed_actions` on these two types was `["explore-data"]`
# for a compiled View and `[]` for everything else, while the change set had
# accepted `edit_concept`, `edit_view` and `archive_object` since it existed. The
# console therefore rendered no way to correct a published Concept or retire a
# View, and `governance.md` (amendment of 2026-08-18) states the rule these tests
# hold: the OWNER declares, and it declares only what would not be refused on
# sight.
# ---------------------------------------------------------------------------


def _concept_row(**overrides) -> dict:
    row = {
        "id": "sc_EXAMPLE",
        "project_id": PROJECT,
        "kind": "metric",
        "name": "gross_revenue",
        "label": "Gross revenue",
        "lifecycle_status": "published",
        "current_version_id": "scv_EXAMPLE",
        "version_number": 3,
        "value_type": "money",
        "additivity_class": "additive",
        "aggregation": {"function": "sum"},
        "version_count": 3,
        "used_by_view_count": 1,
        "business_domain_refs": [],
        "provenance": {},
    }
    row.update(overrides)
    return row


def _view_row(**overrides) -> dict:
    row = {
        "id": "sv_EXAMPLE",
        "project_id": PROJECT,
        "name": "monthly_sales",
        "label": "Monthly sales",
        "lifecycle_status": "published",
        "current_version_id": "svv_EXAMPLE",
        "version_number": 2,
        "version_count": 2,
        "business_scope": "sales",
        "description": "Spend against sales.",
        "business_domain_refs": [],
        "master_data_refs": [{"object_type": "registry", "version_id": "mdv_1"}],
        "evidence_refs": [{"evidence_id": "ev_1", "kind": "compilation"}],
        "query_policy": {"max_rows": 5000},
        "queryability_matrix": {"summary": {"accepted": 4, "refused": 0}},
    }
    row.update(overrides)
    return row


def test_a_published_concept_declares_the_two_gestures_its_owner_can_honour():
    from core.governance_read_model import _semantic_concept

    item = _semantic_concept(_concept_row(), project_id=PROJECT)
    assert item["allowed_actions"] == ["edit_concept", "archive_object"]
    # And the identity every published version writes is ON THE WIRE. Without it
    # the console could not compose an edit at all: `label` is composed from
    # `label or name`, so an object with a display label arrived with its machine
    # name nowhere, and the dialog had to refuse rather than guess it.
    assert item["summary"]["name"] == "gross_revenue"


def test_an_object_with_no_published_version_declares_no_gesture():
    """`create_change_set` refuses `missing_exact_base` for both, so declaring
    either would offer a command that is refused the moment it is sent."""
    from core.governance_read_model import _semantic_concept, _semantic_view

    assert _semantic_concept(
        _concept_row(current_version_id=None, lifecycle_status="draft"), project_id=PROJECT
    )["allowed_actions"] == []
    assert _semantic_view(
        _view_row(current_version_id=None, lifecycle_status="draft"), project_id=PROJECT
    )["allowed_actions"] == []


def test_an_archived_object_declares_no_gesture_and_stays_readable():
    from core.governance_read_model import _semantic_concept

    item = _semantic_concept(_concept_row(lifecycle_status="archived"), project_id=PROJECT)
    assert item["allowed_actions"] == []
    # Retired is not deleted: the object still says what it was, and the version
    # it carried is still named.
    assert item["lifecycle_status"] == "archived"
    assert item["active_version_ref"]["id"] == "scv_EXAMPLE"


def test_a_platform_concept_is_editable_and_never_retirable_from_one_project():
    """`_apply_archive` refuses `platform_scope_archive_refused`: retiring a
    Concept every Project reads, from one Project, is not this surface's to do."""
    from core.governance_read_model import _semantic_concept

    item = _semantic_concept(_concept_row(project_id=None), project_id=PROJECT)
    assert item["scope"] == "platform"
    assert item["allowed_actions"] == ["edit_concept"]


def test_a_compiled_view_keeps_explore_data_and_gains_its_change_set_gestures():
    from core.governance_read_model import _semantic_view

    item = _semantic_view(_view_row(), project_id=PROJECT)
    assert item["allowed_actions"] == ["explore-data", "edit_view", "archive_object"]
    # `explore-data` answers a different question -- is there a compiled,
    # queryable version -- so a View that cannot be explored is still editable.
    unqueryable = _semantic_view(
        _view_row(queryability_matrix={"summary": {"accepted": 0, "refused": 3}}),
        project_id=PROJECT,
    )
    assert unqueryable["allowed_actions"] == ["edit_view", "archive_object"]


def test_a_view_carries_what_an_edit_must_preserve():
    """`_apply_view` writes `name`, `master_data_refs`, `evidence_refs` and
    `query_policy` from the payload on EVERY version, so a new version composed
    without them retires what the published one pinned -- silently."""
    from core.governance_read_model import _semantic_view

    summary = _semantic_view(_view_row(), project_id=PROJECT)["summary"]
    assert summary["name"] == "monthly_sales"
    assert summary["master_data_refs"] == [{"object_type": "registry", "version_id": "mdv_1"}]
    assert summary["evidence_refs"] == [{"evidence_id": "ev_1", "kind": "compilation"}]
    assert summary["query_policy"] == {"max_rows": 5000}


def test_the_declared_actions_are_exactly_the_intents_the_change_set_accepts():
    """One vocabulary on the wire and on the screen. An action named here that
    `create_change_set` does not accept is a button that 422s on sight."""
    from core.governance_read_model import _semantic_concept, _semantic_view
    from core.semantic_model import _SUPPORTED_INTENTS

    declared = set(_semantic_concept(_concept_row(), project_id=PROJECT)["allowed_actions"])
    declared |= set(_semantic_view(_view_row(), project_id=PROJECT)["allowed_actions"])
    # `explore-data` is a handoff to Analyze, not a change set.
    assert declared - {"explore-data"} <= set(_SUPPORTED_INTENTS)
    assert {"edit_concept", "edit_view", "archive_object"} <= declared
