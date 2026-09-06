"""A Business Domain reference is checked BEFORE it is frozen into a version.

`governance.md:72` makes a Semantic View "linkable to Business Domains", and
`_apply_view` / `_apply_concept` write `business_domain_refs` from the change-set
intent straight into `app.semantic_view_versions` and
`app.semantic_concept_versions`. Measured on 2026-08-03, nothing between the two
checked anything: an id that resolves to no domain, or to a domain in another
organization, was written verbatim.

Why that is worse than an ordinary unchecked field — and it is the reason these
tests exist at all: a published version is IMMUTABLE. A dangling reference can
never be corrected in place, only superseded, and every Result already pinned to
the bad version keeps pointing at it. There is exactly one moment where the
refusal costs nothing, and it is before the row exists.

The scope test is the one that would rot quietly if it were missing: Business
Domains are ORGANIZATION objects (`README.md:97`), so the check joins through
`app.projects` rather than trusting an org supplied by the caller. A test that
only proved "unknown id refused" would pass just as happily against a query that
forgot the join, and cross-organization leakage is not a defect anyone notices
by clicking.
"""

from __future__ import annotations

import pytest
from core.semantic_model import _validate_business_domain_refs


class _Cursor:
    """The two attributes `_fetch` reads: `description` and `fetchall`."""

    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.description = [("id",), ("status",), ("archived_at",)]
        self.executed: list[tuple] = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Conn:
    """A connection that answers the domain lookup with exactly these rows.

    Deliberately dumb: it does NOT filter by the ids it is given. So a query
    that forgot its `WHERE` would still be caught by the assertions below, which
    check the refusal produced from the rows the database actually returned.
    """

    def __init__(self, rows: list[tuple]):
        self.rows = rows
        self.cursor_obj = _Cursor(rows)

    def cursor(self):
        return self.cursor_obj


def _codes(refusals) -> list[str]:
    return sorted(refusal.code for refusal in refusals)


def test_empty_is_silence_not_a_refusal():
    # "No domain linked" is the honest default of every object that has not been
    # classified yet. Refusing it would make the field mandatory, which no
    # ratified page asks for.
    for empty in (None, [], ()):
        assert _validate_business_domain_refs(_Conn([]), "proj_EXAMPLE", empty, path="$") == []


def test_a_resolving_domain_passes():
    conn = _Conn([("bd_1", "active", None)])
    assert _validate_business_domain_refs(conn, "proj_EXAMPLE", ["bd_1"], path="$") == []


def test_an_unknown_id_is_refused_by_name():
    conn = _Conn([])
    refusals = _validate_business_domain_refs(conn, "proj_EXAMPLE", ["bd_ghost"], path="$.view")
    assert _codes(refusals) == ["unknown_business_domain"]
    # The id is named, and the reason says why it cannot be fixed later.
    assert "bd_ghost" in refusals[0].message
    assert "immutable" in refusals[0].message
    assert refusals[0].path == "$.view"


def test_a_domain_of_another_organization_is_refused():
    # The query joins `app.projects` -> `app.mdm_business_domains` on `org_id`,
    # so a domain outside this project's organization is simply absent from the
    # result and lands in the same named refusal. This test pins the BEHAVIOUR;
    # `test_scope_is_enforced_by_the_query` below pins the mechanism.
    conn = _Conn([])
    refusals = _validate_business_domain_refs(conn, "proj_EXAMPLE", ["bd_other_org"], path="$")
    assert _codes(refusals) == ["unknown_business_domain"]


def test_scope_is_enforced_by_the_query_not_by_a_caller_supplied_org():
    conn = _Conn([("bd_1", "active", None)])
    _validate_business_domain_refs(conn, "proj_EXAMPLE", ["bd_1"], path="$")
    query, params = conn.cursor_obj.executed[0]
    assert "JOIN app.projects" in query
    assert "p.org_id = d.org_id" in query
    # The project is the ONLY scope input. An `org_id` argument would be a
    # client-trusted value, which is what the API layer already refuses to pass.
    assert params == {"project_id": "proj_EXAMPLE", "ids": ["bd_1"]}


def test_an_archived_domain_cannot_be_linked_by_a_new_version():
    conn = _Conn([("bd_old", "archived", "2026-01-01T00:00:00Z")])
    refusals = _validate_business_domain_refs(conn, "proj_EXAMPLE", ["bd_old"], path="$")
    assert _codes(refusals) == ["archived_business_domain"]
    assert "bd_old" in refusals[0].message


def test_a_duplicate_link_is_named_rather_than_silently_deduplicated():
    # Deduplicating in silence would store something the author did not write,
    # in a row nobody can edit afterwards.
    conn = _Conn([("bd_1", "active", None)])
    refusals = _validate_business_domain_refs(conn, "proj_EXAMPLE", ["bd_1", "bd_1"], path="$")
    assert _codes(refusals) == ["duplicate_business_domain_ref"]


def test_a_non_list_is_refused_before_any_query_runs():
    conn = _Conn([])
    refusals = _validate_business_domain_refs(conn, "proj_EXAMPLE", "bd_1", path="$")
    assert _codes(refusals) == ["malformed_business_domain_refs"]
    assert conn.cursor_obj.executed == []


def test_unknown_and_archived_are_reported_together():
    # One pass, every reason. A validator that stopped at the first refusal would
    # make the author fix one id, resubmit, and discover the next.
    conn = _Conn([("bd_old", "archived", "2026-01-01T00:00:00Z")])
    refusals = _validate_business_domain_refs(
        conn, "proj_EXAMPLE", ["bd_old", "bd_ghost"], path="$"
    )
    assert _codes(refusals) == ["archived_business_domain", "unknown_business_domain"]


# ---------------------------------------------------------------------------
# The intent shape, refused at creation rather than discovered at apply time
# ---------------------------------------------------------------------------


def test_a_flat_intent_is_refused_at_creation_not_applied_as_empty():
    """An intent whose fields sit at the top level is refused by name.

    Every reader downstream does `intent.get("view") or {}` (`semantic_model.py`
    :959, :1582 and five more), so a flat intent resolves to an EMPTY payload and
    `_apply_view` writes `str(payload.get("name"))` -- the string "None" -- into
    an immutable published version.

    Measured on 2026-08-03, this was live: both console creation dialogs sent the
    flat shape, and the screen could not show it. `prepare` is called with
    `.catch(() => null)`, so its refusal is swallowed; `confirm` is then skipped
    for want of a token; and the dialog announces that the object was created. A
    change set exists and nothing is published -- a success message over an
    unfinished write, which is the one failure this repository keeps finding.
    """
    from core.semantic_model import SemanticRefused, create_change_set

    with pytest.raises(SemanticRefused) as excinfo:
        create_change_set(
            object(),
            "proj_EXAMPLE",
            actor="owner@example.com",
            object_type="semantic-view",
            object_id=None,
            base_version_id=None,
            # The shape the dialogs send: `name` beside `action`, no `view`.
            intent={"action": "create_view", "name": "revenue", "label": "Revenue"},
            idempotency_key="key-1",
        )
    assert excinfo.value.code == "missing_intent_payload"
    assert "intent.view" in str(excinfo.value)


def test_the_correct_nested_intent_is_not_refused_for_its_shape():
    # The guard must not become a second reason to refuse a well-formed command:
    # a nested payload passes this check and goes on to the real validation.
    from core.semantic_model import SemanticRefused, create_change_set

    with pytest.raises(SemanticRefused) as excinfo:
        create_change_set(
            object(),
            "proj_EXAMPLE",
            actor="owner@example.com",
            object_type="semantic-view",
            object_id=None,
            base_version_id=None,
            intent={"action": "create_view", "view": {"name": "revenue"}},
            idempotency_key="",  # refused for the KEY, proving the shape passed
        )
    assert excinfo.value.code == "missing_idempotency_key"


def test_archiving_needs_no_payload():
    # `archive_object` names the object through `object_id`; demanding a body
    # would refuse the one action that legitimately has none.
    from core.semantic_model import SemanticRefused, create_change_set

    with pytest.raises(SemanticRefused) as excinfo:
        create_change_set(
            object(),
            "proj_EXAMPLE",
            actor="owner@example.com",
            object_type="semantic-view",
            object_id=None,
            base_version_id=None,
            intent={"action": "archive_object"},
            idempotency_key="key-1",
        )
    assert excinfo.value.code == "missing_exact_base"


# ---------------------------------------------------------------------------
# `used_by`: unavailable and zero are two different answers
# ---------------------------------------------------------------------------


class _RaisingCursor(_Cursor):
    def execute(self, query, params=None):
        raise RuntimeError("relation does not exist")


class _RaisingConn(_Conn):
    def cursor(self):
        return _RaisingCursor([])


def test_used_by_reports_zero_as_a_fact_when_the_owner_answered():
    """Zero consumers is an ANSWER; `unavailable` is the absence of one.

    `governance_read_model.py` hardcoded `used_by=_facet("unavailable")` for the
    Semantic View while eight other governed objects in the same file computed
    it. The workbench therefore said "No owner answers Used by — this is not a
    count of zero" about a question that has an owner and an answer.

    Declaring something unknowable when it is merely unasked is the same class of
    error `README.md` invariant 8 forbids in the other direction: unknown must
    never read as healthy, and knowable must never read as unknown.
    """
    from core.governance_read_model import _semantic_view

    envelope = _semantic_view(
        {"id": "sv_1", "name": "revenue"}, project_id="proj_EXAMPLE", used_by_count=0
    )
    assert envelope["used_by"]["state"] == "empty"
    assert envelope["used_by"]["count"] == 0


def test_used_by_stays_unavailable_when_the_question_could_not_be_asked():
    from core.governance_read_model import _semantic_view

    envelope = _semantic_view(
        {"id": "sv_1", "name": "revenue"}, project_id="proj_EXAMPLE", used_by_count=None
    )
    assert envelope["used_by"]["state"] == "unavailable"


def test_used_by_counts_consumers_when_they_exist():
    from core.governance_read_model import _semantic_view

    envelope = _semantic_view(
        {"id": "sv_1", "name": "revenue"}, project_id="proj_EXAMPLE", used_by_count=3
    )
    # `_semantic_view` counts its consumers and composes no list, so the facet is
    # `unavailable` WITH its count (49-2, 2026-09-01): three is known, and which
    # three is not. "available with an empty list" was the state the workbench
    # rendered as "nothing depends on this object".
    assert envelope["used_by"]["state"] == "unavailable"
    assert envelope["used_by"]["count"] == 3
    assert envelope["used_by"]["reason"]["code"] == "facet_refs_not_composed"


def test_a_failing_consumer_read_returns_none_rather_than_a_false_zero():
    # A read that blew up must NOT become "nothing depends on this view".
    from core.governance_read_model import _semantic_view_used_by

    assert _semantic_view_used_by(_RaisingConn([]), "proj_EXAMPLE") is None


def test_consumers_are_counted_per_consumer_not_per_version():
    # A Golden Question with nine versions pinned to one view is ONE consumer.
    # Counting rows would report a dependency load that does not exist.
    from core.governance_read_model import _SEMANTIC_VIEW_CONSUMERS

    assert "COUNT(DISTINCT consumer_id)" in _SEMANTIC_VIEW_CONSUMERS
    # And the three owners README:116 names are the three that are read.
    for table in ("app.query_specs", "app.golden_question_versions", "app.observed_cohorts"):
        assert table in _SEMANTIC_VIEW_CONSUMERS
