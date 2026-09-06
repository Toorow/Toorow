"""Story 71.4 -- the row-decision pair on the GENERIC capability surface.

Two properties, and both are about where a verb lives rather than about what it
computes:

  * the pair is declared with the same effects and the same confirmation mode as
    the capability-change pair beside it, so ONE ceremony covers both -- a
    ``prepare`` that cannot authorize and a ``confirmed_write`` that is invisible
    and uncallable without proven interactive presence;
  * neither tool is capability-specific. They take a ``capability_key``, and a key
    with no row-level act is refused BY NAME rather than answered with an empty
    payload.

The write itself is proved against a real database in
``test_analytics_alignment_read_pg.py``: what a stub could tell this file about a
first-decision-stands unique index is nothing.
"""

from __future__ import annotations

import json

import pytest
from core import project_capabilities_mcp as surface
from core.mcp_profiles import registered_declarations, reset_registry_for_tests


class _Recorder:
    """Minimal FastMCP stand-in: records what the module registers."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def tool(self, handler, *, name=None, tags=None, meta=None):
        self.tools[name or handler.__name__] = {"handler": handler, "tags": tags, "meta": meta}
        return handler


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def _declarations() -> dict[str, object]:
    recorder = _Recorder()
    surface.register(recorder)
    return {decl.name: decl for decl in registered_declarations()}


def _error_payload(exc: Exception) -> dict:
    return json.loads(str(exc))


# ---------------------------------------------------------------------------
# One ceremony, declared twice.
# ---------------------------------------------------------------------------


def test_the_row_decision_pair_is_registered_on_the_generic_surface() -> None:
    declared = _declarations()
    assert "prepare_project_capability_row_decision" in declared
    assert "confirm_project_capability_row_decision" in declared


def test_the_row_pair_carries_the_SAME_effects_as_the_change_pair() -> None:
    """The ceremony is shared even though the store is not -- that is the whole claim."""
    declared = _declarations()
    for prepare in (
        "prepare_project_capability_change",
        "prepare_project_capability_row_decision",
    ):
        assert declared[prepare].effect == "prepare"
        assert declared[prepare].confirmation_mode == "none"
    for confirm in (
        "confirm_project_capability_change",
        "confirm_project_capability_row_decision",
    ):
        assert declared[confirm].effect == "confirmed_write"
        assert declared[confirm].confirmation_mode == "human"
        assert declared[confirm].data_class == "sensitive"


def test_every_tool_here_stays_under_the_governance_profile() -> None:
    declared = _declarations()
    assert {decl.profile for decl in declared.values()} == {"governance"}


def test_the_read_takes_the_options_bag_and_nothing_alignment_shaped_on_its_signature() -> None:
    """A pair and a breakdown belong to ONE capability, so they ride in `options`.

    Four top-level parameters would put an alignment word on the signature of a
    read of `tax_fees`, and the next capability would put a fifth there.
    """
    import inspect  # noqa: PLC0415

    recorder = _Recorder()
    surface.register(recorder)
    signature = inspect.signature(recorder.tools["read_project_capability"]["handler"])
    assert list(signature.parameters) == ["project_id", "capability_key", "options"]
    assert signature.parameters["options"].default is None


# ---------------------------------------------------------------------------
# Generic in shape: a key with no row-level act is refused BY NAME.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability_key", ["tax_fees", "currency_fx", "country", "nonsense"])
def test_a_capability_with_no_row_decision_is_refused_by_name(capability_key) -> None:
    with pytest.raises(Exception) as prepared:
        surface._prepare_project_capability_row_decision(
            "proj_EXAMPLE", capability_key, {"left_row_key": "L1"}
        )
    payload = _error_payload(prepared.value)
    assert payload["code"] == "capability_has_no_row_decision"
    # The refusal names the capabilities that DO have one, so it is an answer.
    assert "analytics_alignment" in payload["message"]


def test_an_empty_selector_is_refused_before_anything_is_read() -> None:
    with pytest.raises(Exception) as refused:
        surface._prepare_project_capability_row_decision(
            "proj_EXAMPLE", "analytics_alignment", {}
        )
    assert _error_payload(refused.value)["code"] == "invalid_selector"


# ---------------------------------------------------------------------------
# The confirmed write is uncallable without presence, and it checks the review.
# ---------------------------------------------------------------------------


def test_the_confirm_is_denied_outright_without_proven_presence(monkeypatch) -> None:
    monkeypatch.setattr(surface, "_presence_available", lambda: False)
    with pytest.raises(Exception) as refused:
        surface._confirm_project_capability_row_decision(
            "proj_EXAMPLE", "analytics_alignment", "a" * 64, {"left_row_key": "L1"}
        )
    # Indistinguishable from absence: existence is sensitive here.
    assert _error_payload(refused.value)["code"] == "not_found"


def test_a_review_reference_of_the_wrong_shape_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(surface, "_presence_available", lambda: True)
    with pytest.raises(Exception) as refused:
        surface._confirm_project_capability_row_decision(
            "proj_EXAMPLE", "analytics_alignment", "too-short", {"left_row_key": "L1"}
        )
    assert _error_payload(refused.value)["code"] == "invalid_review_reference"


def test_an_empty_decision_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(surface, "_presence_available", lambda: True)
    with pytest.raises(Exception) as refused:
        surface._confirm_project_capability_row_decision(
            "proj_EXAMPLE", "analytics_alignment", "a" * 64, {}
        )
    assert _error_payload(refused.value)["code"] == "invalid_decision"


def test_the_confirm_takes_no_idempotency_key_and_that_is_deliberate() -> None:
    """The store's unique index is the idempotency, and it is stronger than a key.

    Two callers with two different keys still leave ONE decision on one row. A
    caller-chosen key here would be a second, weaker idempotency nobody could
    reconcile with the first, so the parameter is absent rather than ignored.
    """
    import inspect  # noqa: PLC0415

    signature = inspect.signature(surface._confirm_project_capability_row_decision)
    assert "idempotency_key" not in signature.parameters
    assert "idempotency_key" not in inspect.signature(
        surface._prepare_project_capability_row_decision
    ).parameters


# ---------------------------------------------------------------------------
# The alignment block is a BLOCK on the generic read, not a tool.
# ---------------------------------------------------------------------------


def test_no_alignment_specific_read_tool_is_registered() -> None:
    declared = _declarations()
    assert not [name for name in declared if "alignment" in name]


def test_an_unreadable_alignment_owner_returns_an_empty_block_not_a_crash(
    monkeypatch,
) -> None:
    """A read failure is an absence the caller can see, never a 500 on the whole read."""

    class _Boom:
        def cursor(self):
            raise RuntimeError("the owner is unreadable")

    assert surface._foundation_summary(_Boom(), "proj_EXAMPLE", "analytics_alignment") == {}
