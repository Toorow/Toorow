"""AI-295 -- the reference layer serves ONE reconciliation model, the governed one.

WHAT THIS HOLDS, and why a unit test can hold it. Story 49.4 moved the runtime
onto the governed Rule Sets and left the two READ surfaces on the retired
`app.overlap_groups` cascade. Nothing was red: both models answered, they just
answered differently, and no test compared them. So the probe here is not "does
the reference return a rule" -- it is "does it ask the same resolver the engine
asks", which is the only formulation a second store cannot satisfy by accident.

The measured defect, on a live database before the cut:

    _load_reconciliation_rows(project_id="proj_EXAMPLE")   -> KEEP_SEPARATE rule
    governed_runtime_rule("proj_EXAMPLE", "cost")          -> None

`proj_EXAMPLE` exists nowhere. The cascade answered for nobody because a PLATFORM
row has no project to disagree with -- the same defect 49.4 named for the runtime
(`resolve_route("no_such_project", ...)` returning ROUTED_TO_MART), still served
by the REST reference and the MCP tool eleven days later.

No database: the governed resolver is injected. That is deliberate -- a test that
needs Postgres to prove which module is called would not run on the machine where
the mistake gets made.
"""

from __future__ import annotations

from unittest.mock import patch

from core.metric_semantics import reference_reconciliation

_GOVERNED = "core.controls_quality.governed_runtime_rule"

_PUBLISHED_RULE = {
    "method": "PRIORITY",
    "priority_order": ["google-ads", "meta-ads"],
    "join_key": None,
    "truth_connector": "doubleverify",
    "scope_level": "PROJECT",
    "rule_set_version_id": "grsv_EXAMPLE",
    "overlap_group_id": "grsv_EXAMPLE",
    "governed_sources": [],
}


def test_a_project_read_asks_the_governed_resolver():
    """The rule served is the published version, named by its id."""
    with patch(_GOVERNED, return_value=_PUBLISHED_RULE) as governed:
        entry = reference_reconciliation(project_id="proj_EXAMPLE", metric="cost")

    governed.assert_called_once_with("proj_EXAMPLE", "cost")
    assert entry == {
        "method": "PRIORITY",
        "priority_order": ["google-ads", "meta-ads"],
        "join_key": None,
        "truth_connector": "doubleverify",
        "resolved_scope": "PROJECT",
        "rule_set_version_id": "grsv_EXAMPLE",
    }


def test_no_published_rule_is_none_not_a_borrowed_one():
    """No rule in this Project stays "no rule". It does not become a PLATFORM one."""
    with patch(_GOVERNED, return_value=None):
        assert reference_reconciliation(project_id="proj_EXAMPLE", metric="cost") is None


def test_an_org_scoped_read_carries_no_reconciliation():
    """A governed rule belongs to a Project, so an ORG read has none to serve.

    And it must not reach for one: the resolver is never called, because calling
    it with no project is how the cascade used to be re-entered.
    """
    with patch(_GOVERNED) as governed:
        assert reference_reconciliation(project_id=None, metric="cost") is None
        assert reference_reconciliation(project_id="", metric="cost") is None
    governed.assert_not_called()


def test_the_retired_cascade_is_not_reachable_from_the_reference():
    """The retired loader is GONE, not merely unused.

    THIS TEST CHANGED SHAPE WITH ITS SUBJECT, 2026-08-17. It used to patch
    `core.metric_semantics._load_reconciliation_rows` and assert the reference
    never called it -- the right assertion while the function still existed,
    because both models returned a plausible dict and only "which one was asked"
    separated them.

    The retirement then went one step further and DELETED the function, so the
    patch raised `AttributeError` and this test went red for the best possible
    reason: what it guarded no longer exists. Restoring the patch would have
    meant restoring the function.

    So the assertion becomes the stronger one. An absent loader cannot be called
    by anything -- not by the reference, not by a surface written next month --
    and that is a claim about the whole module rather than about one caller.
    """
    import core.metric_semantics as metric_semantics  # noqa: PLC0415

    assert not hasattr(metric_semantics, "_load_reconciliation_rows"), (
        "the retired overlap-group cascade is back in core.metric_semantics. "
        "There is ONE reconciliation model -- the governed Rule Sets -- and a "
        "second loader beside it is how the two came to answer differently for "
        "eleven days without a single red test."
    )

    # And the reference still answers from the governed resolver: the deletion
    # removed the wrong model, not the right one.
    with patch(_GOVERNED, return_value=_PUBLISHED_RULE) as governed:
        entry = reference_reconciliation(project_id="proj_EXAMPLE", metric="cost")
    governed.assert_called_once()
    assert entry is not None


def test_an_unreadable_policy_reads_as_no_rule_never_as_a_sum():
    """Fail-soft matches the resolver it replaces: a read failure is "keep separate"."""
    with patch(_GOVERNED, side_effect=RuntimeError("policy store down")):
        try:
            entry = reference_reconciliation(project_id="proj_EXAMPLE", metric="cost")
        except RuntimeError:  # pragma: no cover -- the contract is that it does not raise
            raise AssertionError(
                "an unreadable policy must degrade to 'no rule', not reach the caller"
            ) from None
    assert entry is None
