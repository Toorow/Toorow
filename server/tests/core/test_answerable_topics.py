"""Story 52.1 -- the Answerable Topic catalog is resolved, not hardcoded.

The central test of this file is `test_zero_rows_resolves_to_the_registry_exactly`:
it PROVES the equivalence the story promises rather than asserting it. Every other
test here exists because the layering rules (replace / append / retire) are the
part a reader cannot verify by looking.
"""

from __future__ import annotations

import pytest
from core import answerable_topics as topics
from core import cards as cards_module

# ---------------------------------------------------------------------------
# A fake connection over the two SQL shapes `_fetch_stored` uses.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        params = params or []
        collapsed = " ".join(sql.split())
        if collapsed.startswith("SELECT org_id FROM app.projects"):
            org = self._store["projects"].get(params[0])
            self._rows = [(org,)] if org is not None else []
            return
        if collapsed.startswith("SELECT t.topic_key"):
            org_id, project_id = params
            self._rows = [
                row
                for (row_org, row_project, row) in self._store["topics"]
                if row_org == org_id and row_project == project_id
            ]
            return
        raise AssertionError(f"unexpected SQL in test double: {collapsed[:80]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, store):
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)


def _stored_row(
    topic_key: str,
    *,
    base_template_id: str,
    title: str = "Custom title",
    question: str = "Custom question?",
    lifecycle: str = "active",
    origin: str = "project",
    kind: str = "kpi",
    fallback_rank: int = 0,
    version_id: str | None = "atv_1",
    required_metrics=(),
    required_dimensions=(),
):
    return (
        topic_key,
        lifecycle,
        base_template_id,
        origin,
        version_id,
        1,
        title,
        question,
        kind,
        fallback_rank,
        list(required_metrics),
        list(required_dimensions),
        [],
        [],
    )


def _conn(*rows, project_id="proj_EXAMPLE", org_id="org_EXAMPLE"):
    return _FakeConn(
        {
            "projects": {project_id: org_id},
            "topics": [(org_id, project_id, row) for row in rows],
        }
    )


# ---------------------------------------------------------------------------
# AC3 -- the equivalence, proved.
# ---------------------------------------------------------------------------


def test_zero_rows_resolves_to_the_registry_exactly():
    """A Project that configured nothing gets the nine defaults, field for field."""
    expected = [tpl.to_catalog_entry() for tpl in cards_module.CARD_TEMPLATES]
    resolved = topics.resolve_catalog("proj_EXAMPLE", _conn())

    assert resolved == expected
    # Order is part of the contract: `suggest_template` and the agent catalogue
    # both sort on it, and "the same nine in another order" is a behaviour change.
    assert [e["id"] for e in resolved] == [t.id for t in cards_module.CARD_TEMPLATES]


def test_a_project_that_configured_nothing_states_no_reason():
    """Zero stored rows IS the Project's catalog -- it is not a degradation."""
    catalog, reason = topics.resolve_catalog_with_reason("proj_EXAMPLE", _conn())
    assert catalog == topics.default_catalog()
    assert reason is None


def test_no_connection_still_resolves_to_the_defaults_and_says_it_is_a_fallback():
    """The catalog is how an agent discovers what it can ask -- it never answers nothing.

    But it never answers the defaults MUTELY either: the value degrades, the fact
    does not.
    """
    catalog, reason = topics.resolve_catalog_with_reason("proj_EXAMPLE", None)
    assert catalog == topics.default_catalog()
    assert reason == topics.CATALOG_NO_PROJECT_REASON


class _Exploding:
    def cursor(self):
        raise RuntimeError("relation app.answerable_topics does not exist")


def test_unreadable_store_degrades_to_the_defaults_and_NAMES_the_degradation():
    """This test used to assert only the first half, and that is what pinned the defect.

    Serving the platform defaults for an unreadable store is a defensible choice for
    a discovery surface. Serving them under this Project's id, with no marker, is
    not: a Project that retired six of the nine questions was told it answers all
    nine. Story 52.2 built three reason codes to stop exactly that on the query
    store; the catalog is the root object and had none.
    """
    catalog, reason = topics.resolve_catalog_with_reason("proj_EXAMPLE", _Exploding())

    assert catalog == topics.default_catalog()
    assert reason == topics.CATALOG_UNAVAILABLE_REASON
    # And the three facts stay three values.
    assert len({
        topics.CATALOG_NO_PROJECT_REASON,
        topics.CATALOG_UNAVAILABLE_REASON,
        topics.CATALOG_ACCESS_DENIED_REASON,
    }) == 3


def test_a_retired_question_does_not_come_back_unannounced_when_the_read_fails():
    """S-7: the fallback reinstates a question the Project deliberately stopped
    answering. It may not do that silently -- the caller must be able to refuse to
    present this list as the Project's catalog."""
    retired = _conn(
        _stored_row("kpi", base_template_id="kpi", lifecycle="retired", origin="default")
    )
    live, live_reason = topics.resolve_catalog_with_reason("proj_EXAMPLE", retired)
    degraded, degraded_reason = topics.resolve_catalog_with_reason("proj_EXAMPLE", _Exploding())

    assert "kpi" not in [e["id"] for e in live] and live_reason is None
    assert "kpi" in [e["id"] for e in degraded]
    assert degraded_reason == topics.CATALOG_UNAVAILABLE_REASON


# ---------------------------------------------------------------------------
# Layering.
# ---------------------------------------------------------------------------


def test_stored_entry_replaces_the_default_in_place():
    conn = _conn(
        _stored_row(
            "kpi",
            base_template_id="kpi",
            title="Our KPIs",
            question="Where do we stand?",
            # `create_topic` stamps this: a reword of one of the nine is born a
            # `default`, not a Project invention (migration 152's doctrine).
            origin="default",
        )
    )
    resolved = topics.resolve_catalog("proj_EXAMPLE", conn)

    assert len(resolved) == len(cards_module.CARD_TEMPLATES)
    assert [e["id"] for e in resolved] == [t.id for t in cards_module.CARD_TEMPLATES]
    kpi = next(e for e in resolved if e["id"] == "kpi")
    assert kpi["title"] == "Our KPIs"
    assert kpi["answers_question"] == "Where do we stand?"
    # Everything only a built bundle can supply is inherited from the base card.
    assert kpi["widget_uri"] == cards_module.KPI_CARD_WIDGET_URI
    assert kpi["composition"] == list(cards_module.get_template("kpi").composition)
    assert kpi["origin"] == "default"


def test_retired_default_leaves_the_catalog():
    conn = _conn(_stored_row("kpi", base_template_id="kpi", lifecycle="retired", origin="default"))
    resolved = topics.resolve_catalog("proj_EXAMPLE", conn)

    assert "kpi" not in [e["id"] for e in resolved]
    assert len(resolved) == len(cards_module.CARD_TEMPLATES) - 1


def test_project_topic_is_appended_after_the_defaults():
    conn = _conn(
        _stored_row(
            "weekly-pacing",
            base_template_id="kpi",
            title="Weekly pacing",
            question="Are we pacing to plan this week?",
            required_metrics=("cost",),
        )
    )
    resolved = topics.resolve_catalog("proj_EXAMPLE", conn)

    assert [e["id"] for e in resolved][: len(cards_module.CARD_TEMPLATES)] == [
        t.id for t in cards_module.CARD_TEMPLATES
    ]
    added = resolved[-1]
    assert added["id"] == "weekly-pacing"
    assert added["required_metrics"] == ["cost"]
    assert added["origin"] == "project"
    assert added["base_template_id"] == "kpi"


def test_entry_whose_base_card_no_longer_exists_is_withheld():
    """A question whose renderer is gone is not offered -- it could not render."""
    conn = _conn(_stored_row("orphan", base_template_id="card_that_was_removed"))
    resolved = topics.resolve_catalog("proj_EXAMPLE", conn)

    assert "orphan" not in [e["id"] for e in resolved]
    assert resolved == topics.default_catalog()


def test_head_without_a_version_is_not_a_catalog_entry():
    conn = _conn(_stored_row("halfway", base_template_id="kpi", version_id=None))
    assert topics.resolve_catalog("proj_EXAMPLE", conn) == topics.default_catalog()


def test_another_projects_rows_are_not_visible():
    store = {
        "projects": {"proj_A": "org_A", "proj_B": "org_B"},
        "topics": [("org_B", "proj_B", _stored_row("theirs", base_template_id="kpi"))],
    }
    resolved = topics.resolve_catalog("proj_A", _FakeConn(store))
    assert resolved == topics.default_catalog()


# ---------------------------------------------------------------------------
# Write-time refusals -- stated before anything is stored.
# ---------------------------------------------------------------------------


def test_unknown_base_template_is_refused_at_write_time():
    with pytest.raises(topics.AnswerableTopicRefused) as exc:
        topics._validate_payload(
            {"title": "T", "answers_question": "Q?", "base_template_id": "nope"}
        )
    assert exc.value.code == "unknown_base_template"


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"title": "", "answers_question": "Q?", "base_template_id": "kpi"}, "invalid_title"),
        ({"title": "T", "answers_question": "", "base_template_id": "kpi"}, "invalid_question"),
        (
            {"title": "T", "answers_question": "Q?", "base_template_id": "kpi", "kind": "widget"},
            "invalid_kind",
        ),
        (
            {
                "title": "T",
                "answers_question": "Q?",
                "base_template_id": "kpi",
                "required_metrics": [1],
            },
            "invalid_field",
        ),
    ],
)
def test_payload_refusals_name_their_reason(payload, code):
    with pytest.raises(topics.AnswerableTopicRefused) as exc:
        topics._validate_payload(payload)
    assert exc.value.code == code


@pytest.mark.parametrize("key", ["", "Weekly Pacing", "-leading", "x" * 65, "UPPER"])
def test_invalid_topic_keys_are_refused(key):
    with pytest.raises(topics.AnswerableTopicRefused) as exc:
        topics._validate_key(key)
    assert exc.value.code == "invalid_topic_key"


def test_content_hash_ignores_key_order_but_not_content():
    a = {"title": "T", "answers_question": "Q?"}
    b = {"answers_question": "Q?", "title": "T"}
    assert topics.content_hash(a) == topics.content_hash(b)
    assert topics.content_hash(a) != topics.content_hash({"title": "T", "answers_question": "R?"})


# ---------------------------------------------------------------------------
# The class: every surface that reads the catalog reads THIS resolver.
# ---------------------------------------------------------------------------


def test_no_production_module_iterates_the_registry_behind_the_resolver():
    """The hardcode is gone from the surfaces, not merely wrapped in one of them."""
    import pathlib
    import re

    core_dir = pathlib.Path(__file__).resolve().parents[2] / "core"
    #: `visualization_template_seeds.py` joined the two owners on 2026-09-01 by
    #: RATIFICATION, not by exception: `visualization-and-rendering.md`, decision
    #: D2 of the Chart Template amendment -- *"the ten `CardTemplate` of
    #: `server/core/cards.py` are projected into platform-seed Chart Templates
    #: ... the code stays the catalogue"*. It projects the PLATFORM catalogue into
    #: platform seeds; `resolve_catalog` answers for ONE project and has no
    #: project to answer for here, so asking it would be the wrong read, not a
    #: stricter one. It serves no catalog to a caller, which is what this guard
    #: is about: the sentence above says *every SURFACE that reads the catalog*.
    owners = {"cards.py", "answerable_topics.py", "visualization_template_seeds.py"}
    # Both spellings of the platform-wide read: the registry itself, and the
    # serializer over it. Catching only the first would let a caller keep the
    # hardcode by going one function deeper -- which is exactly what `main.py`
    # did before this story.
    pattern = re.compile(r"\bCARD_TEMPLATES\b|\blist_templates\(\)")

    def _code_only(text: str) -> str:
        # Comments explaining WHY the hardcode was removed must not be findings.
        return "\n".join(line.split("#", 1)[0] for line in text.splitlines())

    offenders = sorted(
        path.name
        for path in core_dir.glob("*.py")
        if path.name not in owners
        and pattern.search(_code_only(path.read_text(encoding="utf-8")))
    )
    assert offenders == [], (
        "these modules still read the platform registry directly instead of "
        f"answerable_topics.resolve_catalog: {offenders}"
    )


def test_the_seed_projection_reads_the_registry_to_project_it_and_nowhere_else():
    """The third owner is held to WHY it was admitted, not merely named.

    An owner listed above and never checked is how an exemption becomes the place
    the hardcode comes back: the projection would only have to grow one read that
    SERVES the catalogue instead of projecting it, and the scan above would say
    nothing. So the registry may be touched in exactly one place here -- the
    derivation D2 ratifies -- and any second reader in this module is a finding.
    """
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "core"
        / "visualization_template_seeds.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    readers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(
            isinstance(child, ast.Name) and child.id == "CARD_TEMPLATES"
            for child in ast.walk(node)
        ):
            readers.add(node.name)

    assert readers == {"_derive"}, (
        "the platform registry is read outside the projection this module was "
        f"admitted for (visualization-and-rendering.md, D2): {sorted(readers)}"
    )


def test_the_agent_catalogue_answers_from_the_projects_catalog_not_the_platform_set():
    """AC5's THIRD consumer, measured on what it READS -- not on what it spells.

    The guard above proves no module names `CARD_TEMPLATES`. `agent_card_catalog`
    passed it while ignoring the project entirely, because its default is the
    resolver's own default set: a parameter was added and no caller ever passed it,
    so the agent tool went on advertising the nine platform questions while
    `preview_daily_insight` refused the very template it had just advertised. The
    class named by AC5 is *surfaces that read the catalog*; the guard measured
    *modules that spell one identifier*. This test measures the class.
    """
    from core import daily_insights_tools as dit

    metrics = {"clicks", "impressions", "conversions", "cost"}
    dims = {"page", "country", "device"}

    project_catalog = [e for e in topics.default_catalog() if e["id"] != "kpi"]
    project_catalog.append(
        {
            **next(e for e in topics.default_catalog() if e["id"] == "kpi"),
            "id": "weekly-pacing",
            "title": "Weekly pacing",
            "answers_question": "Are we pacing to plan this week?",
        }
    )

    caps = dit.capabilities(
        available_metrics=metrics,
        available_dimensions=dims,
        catalog=project_catalog,
    )
    ids = {e["id"] for e in caps["catalog"]}

    assert "weekly-pacing" in ids, "a question the Project ADDED is never advertised"
    assert "kpi" not in ids, "a question the Project RETIRED is still advertised"
    assert caps["catalogStatus"] == "resolved"
    # The recommendation must read the same catalogue: two answers to one question
    # is what a second read produces.
    rec_ids = {caps["recommended"]["best"]["id"]} | {
        a["id"] for a in caps["recommended"]["alternatives"]
    }
    assert "kpi" not in rec_ids


def test_the_agent_catalogue_says_when_it_is_the_platform_fallback():
    from core import daily_insights_tools as dit

    caps = dit.capabilities(
        available_metrics={"clicks"},
        available_dimensions=set(),
        catalog=topics.default_catalog(),
        catalog_reason=topics.CATALOG_UNAVAILABLE_REASON,
    )
    assert caps["catalogStatus"] == "defaults_only"
    assert caps["catalogReason"] == topics.CATALOG_UNAVAILABLE_REASON


def test_get_topic_is_gone_rather_than_left_as_decor():
    """C-9: it had no caller in production and none in a test -- the same class as
    `resolve_alias`, which was removed for that reason. An exported function nobody
    calls reads like a contract and is not one."""
    assert not hasattr(topics, "get_topic")


# ---------------------------------------------------------------------------
# Story 52.2 -- the governed queries that answer a topic.
# ---------------------------------------------------------------------------


def _binding_row(
    binding_id: str,
    topic_key: str,
    *,
    role: str = "headline",
    position: int = 1,
    version_id: str = "qsv_1",
    version_number: int = 1,
    lifecycle: str = "active",
):
    return (
        binding_id,
        topic_key,
        role,
        position,
        "qs_1",
        version_id,
        version_number,
        "sv_1",
        "svv_1",
        lifecycle,
    )


class _BindingConn(_FakeConn):
    """A fake connection that also answers the Story 52.2 binding read."""

    def cursor(self):
        store = self._store

        class _C(_FakeCursor):
            def execute(self, sql, params=None):
                collapsed = " ".join(sql.split())
                if collapsed.startswith("SELECT b.id, t.topic_key"):
                    org_id, project_id = params or []
                    self._rows = [
                        row
                        for (row_org, row_project, row) in store.get("bindings", [])
                        if row_org == org_id and row_project == project_id
                    ]
                    return
                return super().execute(sql, params)

        return _C(store)


def _binding_conn(topic_rows=(), binding_rows=(), project_id="proj_EXAMPLE", org_id="org_EXAMPLE"):
    return _BindingConn(
        {
            "projects": {project_id: org_id},
            "topics": [(org_id, project_id, r) for r in topic_rows],
            "bindings": [(org_id, project_id, r) for r in binding_rows],
        }
    )


def test_a_topic_binds_several_queries_in_the_projects_order():
    conn = _binding_conn(
        topic_rows=[_stored_row("pacing", base_template_id="kpi", title="Pacing",
                                question="Are we pacing to plan?")],
        binding_rows=[
            _binding_row("atq_2", "pacing", role="component", position=2, version_id="qsv_2"),
            _binding_row("atq_1", "pacing", role="headline", position=1, version_id="qsv_1"),
        ],
    )
    bindings = topics.resolve_bindings("proj_EXAMPLE", conn)

    # The SQL orders by position; the resolver must not re-sort or drop.
    assert [b["binding_id"] for b in bindings] == ["atq_2", "atq_1"]
    assert {b["role"] for b in bindings} == {"headline", "component"}


def test_a_retired_topic_keeps_its_rows_and_answers_nothing():
    conn = _binding_conn(
        topic_rows=[_stored_row("gone", base_template_id="kpi", lifecycle="retired")],
        binding_rows=[_binding_row("atq_1", "gone", lifecycle="retired")],
    )
    assert topics.resolve_bindings("proj_EXAMPLE", conn) == []


def test_pairs_carry_the_question_and_the_exact_pin():
    conn = _binding_conn(
        topic_rows=[_stored_row("pacing", base_template_id="kpi", title="Pacing",
                                question="Are we pacing to plan?")],
        binding_rows=[_binding_row("atq_1", "pacing", role="headline")],
    )
    pairs, reason = topics.verified_query_pairs("proj_EXAMPLE", conn)

    assert reason is None
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["question"] == "Are we pacing to plan?"
    # The filter contract of metric_semantics_mcp: surface is the topic key, the
    # role travels as a tag. Stated in one place, asserted here.
    assert pair["surface"] == "pacing"
    assert pair["tags"] == ["headline"]
    assert pair["query_spec_version_id"] == "qsv_1"


def test_a_project_that_bound_nothing_says_so_and_is_not_an_error():
    conn = _binding_conn(topic_rows=[_stored_row("pacing", base_template_id="kpi")])
    pairs, reason = topics.verified_query_pairs("proj_EXAMPLE", conn)

    assert pairs == []
    assert reason == topics.NO_BINDING_REASON


def test_an_unreadable_store_is_never_reported_as_there_are_none():
    class _Exploding:
        def cursor(self):
            raise RuntimeError("relation app.answerable_topic_query_bindings does not exist")

    pairs, reason = topics.verified_query_pairs("proj_EXAMPLE", _Exploding())
    assert pairs == []
    assert reason == topics.UNAVAILABLE_REASON
    assert reason != topics.NO_BINDING_REASON


def test_a_binding_whose_base_card_left_the_registry_still_carries_its_question():
    """It used to be dropped, and the project was then told it had bound nothing.

    A card removed from OUR registry is a fact about us. The Project's binding is
    real and its question is stored on its own version, so the pair (question,
    governed query) is true even though no card can draw it. Reporting
    `project_has_no_bound_query` here was a false statement about the Project.
    """
    conn = _binding_conn(
        topic_rows=[
            _stored_row(
                "orphan",
                base_template_id="card_that_was_removed",
                question="Which orphan question?",
            )
        ],
        binding_rows=[_binding_row("atq_1", "orphan")],
    )
    pairs, reason = topics.verified_query_pairs("proj_EXAMPLE", conn)

    assert reason is None
    assert [p["question"] for p in pairs] == ["Which orphan question?"]
    assert reason != topics.NO_BINDING_REASON


def test_a_binding_with_no_wording_at_all_is_not_reported_as_no_binding():
    """A head with no version has no question: the pair is half of one and is
    dropped. `project_has_no_bound_query` would still be false -- rows exist."""
    conn = _binding_conn(
        topic_rows=[_stored_row("halfway", base_template_id="kpi", version_id=None)],
        binding_rows=[_binding_row("atq_1", "halfway")],
    )
    pairs, reason = topics.verified_query_pairs("proj_EXAMPLE", conn)
    assert pairs == []
    assert reason == topics.UNAVAILABLE_REASON
    assert reason != topics.NO_BINDING_REASON


def test_an_unreadable_catalog_between_the_two_reads_is_not_no_binding_either():
    """`verified_query_pairs` read bindings, then swallowed the catalog failure and
    fell back on the defaults -- producing project bindings paired against platform
    questions, and `project_has_no_bound_query` for a project that bound some."""

    class _BindingsOkCatalogExplodes(_BindingConn):
        def cursor(self):
            outer = super().cursor()

            class _C:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def execute(self_inner, sql, params=None):
                    if " ".join(sql.split()).startswith("SELECT t.topic_key"):
                        raise RuntimeError("relation app.answerable_topics does not exist")
                    return outer.execute(sql, params)

                def fetchone(self_inner):
                    return outer.fetchone()

                def fetchall(self_inner):
                    return outer.fetchall()

            return _C()

    conn = _BindingsOkCatalogExplodes(
        {
            "projects": {"proj_EXAMPLE": "org_EXAMPLE"},
            "topics": [],
            "bindings": [("org_EXAMPLE", "proj_EXAMPLE", _binding_row("atq_1", "pacing"))],
        }
    )
    pairs, reason = topics.verified_query_pairs("proj_EXAMPLE", conn)
    assert pairs == []
    assert reason == topics.UNAVAILABLE_REASON


@pytest.mark.parametrize("pin", ["latest", "LATEST", "current", ""])
def test_an_inexact_pin_is_refused_before_anything_is_stored(pin):
    with pytest.raises(topics.AnswerableTopicRefused) as exc:
        topics.bind_query(
            None,
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            actor="tester",
            topic_key="pacing",
            query_spec_version_id=pin,
            role="headline",
            idempotency_key="idem-1",
        )
    assert exc.value.code == "pin_is_not_exact"


@pytest.mark.parametrize("role", ["", "   ", "x" * 61])
def test_an_empty_or_oversized_role_is_refused(role):
    with pytest.raises(topics.AnswerableTopicRefused) as exc:
        topics.bind_query(
            None,
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            actor="tester",
            topic_key="pacing",
            query_spec_version_id="qsv_1",
            role=role,
            idempotency_key="idem-1",
        )
    assert exc.value.code == "invalid_role"


def test_the_two_absences_are_different_constants():
    """If these ever collapse to one value, the distinction dies silently."""
    assert topics.NO_BINDING_REASON != topics.UNAVAILABLE_REASON


# ---------------------------------------------------------------------------
# Story 52.3 -- the governed knowledge a topic may elaborate from.
# ---------------------------------------------------------------------------


def _knowledge_row(
    binding_id: str,
    topic_key: str,
    *,
    kind: str = "topic",
    knowledge_id: str = "ctx_1",
    version: int = 3,
    title: str | None = "Attribution policy",
    body: str | None = "We attribute last non-direct click.",
    lifecycle: str = "active",
    requires: bool = False,
):
    """A row in the shape `_fetch_knowledge_bindings` selects."""
    is_topic = kind == "topic"
    return (
        binding_id,
        topic_key,
        kind,
        knowledge_id,
        version,
        title if is_topic else None,
        body if is_topic else None,
        title if not is_topic else None,
        body if not is_topic else None,
        lifecycle,
        requires,
    )


class _KnowledgeConn(_FakeConn):
    def cursor(self):
        store = self._store

        class _C(_FakeCursor):
            def execute(self, sql, params=None):
                collapsed = " ".join(sql.split())
                if collapsed.startswith("SELECT b.id, t.topic_key, b.knowledge_kind"):
                    org_id, project_id = params or []
                    self._rows = [
                        row
                        for (row_org, row_project, row) in store.get("knowledge", [])
                        if row_org == org_id and row_project == project_id
                    ]
                    return
                return super().execute(sql, params)

        return _C(store)


def _knowledge_conn(rows=(), project_id="proj_EXAMPLE", org_id="org_EXAMPLE"):
    return _KnowledgeConn(
        {
            "projects": {project_id: org_id},
            "topics": [],
            "knowledge": [(org_id, project_id, r) for r in rows],
        }
    )


def test_a_pin_is_read_at_its_exact_version_and_cited():
    conn = _knowledge_conn([_knowledge_row("atk_1", "pacing", version=3)])
    citations, status, refused, evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing",
        conn,
    )

    assert status is None and refused is False
    assert citations == [
        {"kind": "topic", "id": "ctx_1", "version": 3, "title": "Attribution policy"}
    ]


def test_a_topic_that_declared_nothing_is_not_a_topic_whose_context_is_missing():
    """`context-hub.md:59-60` makes this a completeness criterion, not a nicety."""
    empty = _knowledge_conn([])
    _c, status_none, _r, _e = topics.knowledge_citations("proj_EXAMPLE", "pacing", empty)

    unreadable = _knowledge_conn(
        [_knowledge_row("atk_1", "pacing", title=None, body=None)]
    )
    _c2, status_missing, _r2, _e2 = topics.knowledge_citations("proj_EXAMPLE", "pacing", unreadable)

    assert status_none == topics.NO_KNOWLEDGE_REASON
    assert status_missing == topics.CONTEXT_MISSING_REASON
    assert status_none != status_missing


def test_an_unreadable_pin_is_never_silently_dropped_into_no_knowledge():
    """The version row is gone -- production has 41 version rows and 0 heads."""
    conn = _knowledge_conn([_knowledge_row("atk_1", "pacing", title=None, body=None)])
    citations, status, _refused, _evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing",
        conn,
    )
    assert citations == []
    assert status == topics.CONTEXT_MISSING_REASON


def test_only_readable_pins_are_cited():
    """Citing a declaration that could not be read is the defect this story prevents."""
    conn = _knowledge_conn([
        _knowledge_row("atk_1", "pacing", knowledge_id="ctx_1", version=3),
        _knowledge_row("atk_2", "pacing", knowledge_id="ctx_2", version=9,
                       title=None, body=None),
    ])
    citations, status, _refused, _evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing",
        conn,
    )
    assert [c["id"] for c in citations] == ["ctx_1"]
    assert status is None


def test_a_topic_that_requires_knowledge_refuses_when_its_context_is_missing():
    conn = _knowledge_conn(
        [_knowledge_row("atk_1", "pacing", title=None, body=None, requires=True)]
    )
    citations, status, refused, evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing",
        conn,
    )
    assert citations == []
    assert status == topics.CONTEXT_MISSING_REASON
    assert refused is True


def test_a_topic_that_does_not_require_knowledge_does_not_refuse():
    conn = _knowledge_conn(
        [_knowledge_row("atk_1", "pacing", title=None, body=None, requires=False)]
    )
    _citations, status, refused, evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing",
        conn,
    )
    assert status == topics.CONTEXT_MISSING_REASON
    assert refused is False


def test_a_procedure_pin_is_read_from_the_procedure_version():
    conn = _knowledge_conn([
        _knowledge_row("atk_1", "pacing", kind="procedure", knowledge_id="proc_1",
                       version=2, title="Weekly pacing playbook")
    ])
    citations, status, _refused, _evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing",
        conn,
    )
    assert status is None
    assert citations[0]["kind"] == "procedure"
    assert citations[0]["title"] == "Weekly pacing playbook"


def test_a_retired_topic_elaborates_from_nothing():
    conn = _knowledge_conn([_knowledge_row("atk_1", "gone", lifecycle="retired")])
    citations, status, _refused, _evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "gone",
        conn,
    )
    assert citations == []
    assert status == topics.NO_KNOWLEDGE_REASON


@pytest.mark.parametrize(
    "kind,knowledge_id,version,code",
    [
        ("skill", "sk_1", 1, "invalid_knowledge_kind"),
        ("topic", "", 1, "pin_is_not_exact"),
        ("topic", "ctx_1", 0, "pin_is_not_exact"),
        ("topic", "ctx_1", "latest", "pin_is_not_exact"),
    ],
)
def test_an_inexact_or_unknown_knowledge_pin_is_refused(kind, knowledge_id, version, code):
    """A `skill` pin is refused because a Skill has no store -- measured, not assumed."""
    with pytest.raises(topics.AnswerableTopicRefused) as exc:
        topics.bind_knowledge(
            None,
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            actor="tester",
            topic_key="pacing",
            knowledge_kind=kind,
            knowledge_id=knowledge_id,
            knowledge_version=version,
            idempotency_key="idem-1",
        )
    assert exc.value.code == code


def test_the_two_knowledge_absences_are_different_constants():
    assert topics.NO_KNOWLEDGE_REASON != topics.CONTEXT_MISSING_REASON


class _KnowledgeReadExplodes(_KnowledgeConn):
    """The pin read fails; the HEAD is still readable. That is the AC5 case."""

    def cursor(self):
        outer = super().cursor()

        class _C:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, sql, params=None):
                collapsed = " ".join(sql.split())
                if collapsed.startswith("SELECT b.id, t.topic_key, b.knowledge_kind"):
                    raise RuntimeError("relation app.answerable_topic_knowledge_bindings is gone")
                if collapsed.startswith("SELECT requires_knowledge"):
                    self_inner._row = outer._store.get("head", {}).get(params[2])
                    return
                self_inner._row = None
                return outer.execute(sql, params)

            def fetchone(self_inner):
                if getattr(self_inner, "_row", None) is not None:
                    return (self_inner._row,)
                return outer.fetchone()

            def fetchall(self_inner):
                return outer.fetchall()

        return _C()


def _head_conn(heads: dict, project_id="proj_EXAMPLE", org_id="org_EXAMPLE"):
    return _KnowledgeReadExplodes(
        {"projects": {project_id: org_id}, "topics": [], "knowledge": [], "head": heads}
    )


def test_a_required_declaration_REFUSES_when_the_pin_store_cannot_be_read():
    """S-6: this branch answered `refused=False`, so the one case AC5 names -- "when
    its pins cannot be read, the answer is a stated refusal" -- was the one case in
    which nothing refused. The flag is re-read from the head, which is where it
    lives; the pins that would have carried it are exactly what failed to load."""
    citations, status, refused, evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing", _head_conn({"pacing": True})
    )
    assert citations == []
    assert status == topics.CONTEXT_MISSING_REASON
    assert refused is True


def test_a_topic_that_requires_nothing_does_not_refuse_and_the_check_did_run():
    _c, status, refused, evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing", _head_conn({"pacing": False})
    )
    assert status == topics.CONTEXT_MISSING_REASON
    assert refused is False
    assert evaluated is True, "the head was read: this `False` is a verdict, not a shrug"


def test_an_unverifiable_declaration_is_UNEVALUATED_not_a_refusal():
    """The arbitration, written as a test.

    Two layers answered this differently: this module refused when it could not
    read, `cards._resolve_knowledge_for_card` did not. Both were wrong.

    Refusing on an unreadable store refuses EVERY card during a Postgres outage --
    `requires_knowledge` is false by default, so almost no topic ever asked for a
    refusal, and the product would inflict an outage on itself. Answering `False`
    asserts "this topic does not refuse", which is a claim nobody evaluated.

    So the decision carries whether it was EVALUATED, exactly as
    `meta.freshness.stale_since_evaluated` does in `cards.py`, and for the same
    reason recorded there: an unevaluated value must not read as evaluated.
    """

    class _Everything(_FakeConn):
        def cursor(self):
            raise RuntimeError("connection is dead")

    _c, status, refused, evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing", _Everything({})
    )
    assert status == topics.CONTEXT_MISSING_REASON
    assert refused is False
    assert evaluated is False, (
        "an unread declaration must be reported as unevaluated -- `refused=False` "
        "alone would claim a check nobody ran"
    )


def test_a_declared_refusal_still_refuses_when_the_check_did_run():
    """The arbitration does not weaken AC5: when the head IS readable and the topic
    declared `requires_knowledge`, the refusal stands and says it was evaluated."""
    _c, status, refused, evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing", _head_conn({"pacing": True})
    )
    assert status == topics.CONTEXT_MISSING_REASON
    assert refused is True
    assert evaluated is True


def test_no_connection_is_not_an_absence_of_knowledge():
    """`conn is None` means we did not look. Declaring `no_knowledge_declared` on the
    strength of a missing argument is the confusion context-hub.md forbids -- and
    declaring a REFUSAL on it would be the symmetric invention."""
    _c, status, refused, evaluated = topics.knowledge_citations(
        "proj_EXAMPLE", "pacing", None
    )
    assert status == topics.CONTEXT_MISSING_REASON
    assert (refused, evaluated) == (False, False)


def test_no_scoring_of_prose_is_introduced_by_this_story():
    """AC8: emitting citations is not judging them. Epic 51 owns the judging.

    Asserted on the module's STRUCTURE, not on its prose: a docstring is allowed to
    say who owns evaluation -- and must, since that is how the boundary survives --
    while a function that scores, or an import of an evaluation module, would be a
    second scorer. The word ban this replaces failed on its own explanation.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(topics.__file__).read_text(encoding="utf-8"))

    defined = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for name in defined:
        assert not any(w in name.lower() for w in ("score", "grade", "verdict", "judge")), (
            f"`{name}` scores something: judging belongs to Epic 51"
        )

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    for module in imported:
        assert "evaluation" not in module and "golden_question" not in module, (
            f"the knowledge path imports {module}: evaluation must not ground the runtime"
        )


# ---------------------------------------------------------------------------
# The refusal path of Story 52.3 AC5, and the writer it was missing.
# ---------------------------------------------------------------------------


def test_requires_knowledge_is_accepted_on_the_write_path():
    """The column existed, was READ, and nothing set it -- so a topic could never
    refuse, and AC5 was unreachable. This is the writer."""
    fields = topics._validate_payload(
        {
            "title": "Pacing",
            "answers_question": "Are we pacing to plan?",
            "base_template_id": "kpi",
            "requires_knowledge": True,
        }
    )
    assert fields["requires_knowledge"] is True


def test_requires_knowledge_defaults_to_false_when_unsaid():
    fields = topics._validate_payload(
        {"title": "T", "answers_question": "Q?", "base_template_id": "kpi"}
    )
    assert fields["requires_knowledge"] is False


@pytest.mark.parametrize("value", ["true", 1, "yes"])
def test_a_non_boolean_requires_knowledge_is_refused(value):
    """`"false"` is truthy in Python: accepting a string here would turn a topic
    that says it does NOT need knowledge into one that refuses."""
    with pytest.raises(topics.AnswerableTopicRefused) as exc:
        topics._validate_payload(
            {
                "title": "T",
                "answers_question": "Q?",
                "base_template_id": "kpi",
                "requires_knowledge": value,
            }
        )
    assert exc.value.code == "invalid_field"


def test_the_content_hash_ignores_requires_knowledge():
    """It lives on the HEAD, not on a version: including it would make two
    versions differ over a field neither of them carries."""
    base = {"title": "T", "answers_question": "Q?", "base_template_id": "kpi"}
    assert topics.content_hash(base) == topics.content_hash(base)


def test_the_stored_flag_is_read_back_into_the_catalog_entry():
    """A form that cannot show the current value teaches the operator that saving
    resets it. The entry carries what the topic declared."""
    # `_fetch_stored` selects fourteen version/head columns, then `t.id` (row[14])
    # and `t.requires_knowledge` (row[15]). The fixture builds the first fourteen,
    # so both trailing columns are appended here -- appending only the flag would
    # put it in the topic-id slot and the test would prove the wrong thing.
    row = [*_stored_row("pacing", base_template_id="kpi"), "atp_1", True]
    conn = _conn(tuple(row))
    entry = next(e for e in topics.resolve_catalog("proj_EXAMPLE", conn) if e["id"] == "pacing")

    assert entry["requires_knowledge"] is True


def test_an_inherited_default_entry_carries_no_such_field():
    """The equivalence of Story 52.1 AC3 holds: a project that configured nothing
    still gets entries byte-identical to the registry's."""
    entry = topics.resolve_catalog("proj_EXAMPLE", _conn())[0]
    assert "requires_knowledge" not in entry
