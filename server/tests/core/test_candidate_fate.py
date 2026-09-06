"""The reason vocabulary is enumerated, declared ONCE, and imported (Story 54.2, AC2).

Two things are proven here, and the second is the one that lasts.

1. The wire values. `below_cutoff` and its four siblings are what a surface reads
   and what an evaluation asserts on, so they are pinned by value. Renaming one
   breaks this test, which is the point: it is a contract, not an identifier.

2. The structural ban. `core.candidate_fate` is the only place in `server/` where
   those literals may be typed (this file excepted -- it must name them to pin
   them). Any other site imports the constant. This mirrors
   `test_narrative.test_both_injection_sites_share_one_frame`, added in Story
   53.5 for the same failure mode: two sites that re-type a wording drift, and
   nobody notices until they read both.

The ban is deliberately repo-wide rather than "the emitters we know about". The
emitters of Story 54.2 are not all written yet (T3/T4 carry the crossing, Half B
waits on Story 53.8); a rule that only covers today's callers would let the next
one fork the vocabulary silently.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from core import candidate_fate

SERVER_ROOT = Path(__file__).resolve().parents[2]
VOCABULARY_MODULE = SERVER_ROOT / "core" / "candidate_fate.py"
THIS_FILE = Path(__file__).resolve()

_SKIP_PARTS = {"__pycache__", ".venv", "site-packages", "node_modules", ".mypy_cache"}


def _python_sources() -> list[Path]:
    return [
        path
        for path in SERVER_ROOT.rglob("*.py")
        if not _SKIP_PARTS.intersection(path.parts)
    ]


# ---------------------------------------------------------------------------
# 1. The wire values
# ---------------------------------------------------------------------------


def test_the_five_reasons_are_pinned_by_value():
    """AC2 names these five. They are the wire contract, so they are asserted."""
    assert candidate_fate.REASON_BELOW_CUTOFF == "below_cutoff"
    assert candidate_fate.REASON_OUT_OF_SCOPE == "out_of_scope"
    assert candidate_fate.REASON_DATE_MISMATCH == "date_mismatch"
    assert candidate_fate.REASON_METRIC_MISMATCH == "metric_mismatch"
    assert candidate_fate.REASON_CONNECTOR_MISMATCH == "connector_mismatch"
    # Story 45.5 : le declare rejoint le mesure, et ne le remplace pas.
    assert candidate_fate.REASON_ANTI_TRIGGER == "anti_trigger"
    assert set(candidate_fate.REJECTION_REASONS) == {
        "below_cutoff",
        "out_of_scope",
        "date_mismatch",
        "metric_mismatch",
        "connector_mismatch",
        "anti_trigger",
    }


def test_the_three_fates_are_three_and_named():
    """A never-reached node is a THIRD state, not the absence of the other two."""
    assert candidate_fate.FATE_SELECTED == "selected"
    assert candidate_fate.FATE_REJECTED == "rejected"
    assert candidate_fate.FATE_NOT_REACHED == "not_reached"
    assert candidate_fate.FATES == ("selected", "rejected", "not_reached")


def test_the_walk_declares_which_reasons_it_can_actually_produce():
    """The tree walk drops candidates for ONE cause: the cap. It says so in code.

    A vocabulary that lets every emitter claim every reason is a vocabulary that
    explains nothing. `TREE_WALK_REASONS` is the honest subset, and the three
    pairing reasons are held back for Half B (blocked by Story 53.8).
    """
    assert candidate_fate.TREE_WALK_REASONS == (
        candidate_fate.REASON_BELOW_CUTOFF,
        # Story 45.5 : la seconde cause qu'une marche produit vraiment -- un
        # retrait que le candidat a demande lui-meme.
        candidate_fate.REASON_ANTI_TRIGGER,
    )
    # Et il ne remonte JAMAIS dans l'agregat des rejets recurrents.
    assert candidate_fate.UNRECORDED_REASONS == (candidate_fate.REASON_ANTI_TRIGGER,)
    assert set(candidate_fate.UNRECORDED_REASONS) <= set(candidate_fate.REJECTION_REASONS)
    assert candidate_fate.CONTEXT_EVENT_REASONS == (
        candidate_fate.REASON_DATE_MISMATCH,
        candidate_fate.REASON_METRIC_MISMATCH,
        candidate_fate.REASON_CONNECTOR_MISMATCH,
    )
    assert candidate_fate.BRIEFING_CONTEXT_EVENT_REASONS == (
        candidate_fate.REASON_DATE_MISMATCH,
        # Migration 322 (2026-08-30): events carry an optional metric and the
        # walk disqualifies one naming another -- the reason became producible
        # the same day (briefing.context_event_walk), so it joined the subset.
        candidate_fate.REASON_METRIC_MISMATCH,
        candidate_fate.REASON_CONNECTOR_MISMATCH,
        candidate_fate.REASON_BELOW_CUTOFF,
    )
    # Every declared subset stays inside the closed set.
    for subset in (
        candidate_fate.TREE_WALK_REASONS,
        candidate_fate.CONTEXT_EVENT_REASONS,
        candidate_fate.BRIEFING_CONTEXT_EVENT_REASONS,
    ):
        assert set(subset) <= set(candidate_fate.REJECTION_REASONS)


# ---------------------------------------------------------------------------
# 2. The structural ban -- one module owns the literals
# ---------------------------------------------------------------------------


def test_no_second_site_retypes_a_reason_literal():
    """AC2: the set is declared in ONE module and imported by every emitter."""
    literals = [
        f'"{reason}"' for reason in candidate_fate.REJECTION_REASONS
    ] + [f"'{reason}'" for reason in candidate_fate.REJECTION_REASONS]

    offenders: list[str] = []
    for path in _python_sources():
        if path == VOCABULARY_MODULE or path == THIS_FILE:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover -- unreadable file
            continue
        for literal in literals:
            if literal in source:
                offenders.append(f"{path.relative_to(SERVER_ROOT)} re-types {literal}")

    assert not offenders, (
        "these sites re-type a reason literal instead of importing it from "
        "core.candidate_fate:\n  " + "\n  ".join(offenders)
    )


#: Modules that emit a candidate's fate. The ban on fate literals is scoped to
#: THEM rather than to the whole tree, and the reason is measured, not stylistic:
#: `selected` and `rejected` are ordinary English words already carrying
#: unrelated meanings across `server/` -- a mapping status
#: (`dimension_conformance.STATUS_REJECTED`), an FX rate-set state, an inbound
#: attachment outcome, a capability-compiler flag. A repo-wide ban would fail on
#: code that has nothing to do with this vocabulary, and a test that cries wolf
#: gets weakened by the next session. The distinctive REASON literals are banned
#: everywhere (above); the generic FATE literals are banned where they would
#: actually fork the vocabulary.
#:
#: Story 54.2 T3/T4 and Half B add their emitters to this tuple.
_FATE_EMITTER_MODULES = (
    "core.context_search",
    # T3/T4: the module that turns judged candidates into a crossing and a line.
    "core.candidate_emission",
    # T5 (Half B): the business-event pairing, once Story 53.8 repaired it.
    "core.briefing",
)


def test_fate_emitters_do_not_retype_a_fate_literal():
    """Same rule for the fates, scoped to the modules that emit one."""
    import importlib

    literals = [f'"{fate}"' for fate in candidate_fate.FATES] + [
        f"'{fate}'" for fate in candidate_fate.FATES
    ]

    offenders: list[str] = []
    for module_name in _FATE_EMITTER_MODULES:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        for literal in literals:
            if literal in source:
                offenders.append(f"{module_name} re-types {literal}")

    assert not offenders, (
        "these emitters re-type a fate literal instead of importing it from "
        "core.candidate_fate:\n  " + "\n  ".join(offenders)
    )


def test_the_walk_imports_the_vocabulary_rather_than_naming_reasons_itself():
    """The retriever is the first emitter; it must consume the shared module."""
    from core import context_search

    source = inspect.getsource(context_search)
    assert "candidate_fate" in source, "context_search does not import the vocabulary"


# ---------------------------------------------------------------------------
# 3. The guard that stops prose from becoming a reason
# ---------------------------------------------------------------------------


def test_validate_fate_accepts_the_legal_pairs():
    assert candidate_fate.validate_fate(candidate_fate.FATE_SELECTED, None) == (
        candidate_fate.FATE_SELECTED,
        None,
    )
    assert candidate_fate.validate_fate(candidate_fate.FATE_NOT_REACHED, None) == (
        candidate_fate.FATE_NOT_REACHED,
        None,
    )
    assert candidate_fate.validate_fate(
        candidate_fate.FATE_REJECTED, candidate_fate.REASON_BELOW_CUTOFF
    ) == (candidate_fate.FATE_REJECTED, candidate_fate.REASON_BELOW_CUTOFF)


def test_selected_means_retained_by_the_retriever_not_model_use() -> None:
    assert candidate_fate.SELECTED_MEANING == "retained_by_retriever"


def test_a_model_written_reason_is_refused():
    """The whole point of AC2: free text cannot travel as a reason."""
    with pytest.raises(ValueError, match="enumerated reason"):
        candidate_fate.validate_fate(
            candidate_fate.FATE_REJECTED,
            "this fragment felt less relevant to the question",
        )
    with pytest.raises(ValueError, match="enumerated reason"):
        candidate_fate.validate_fate(candidate_fate.FATE_REJECTED, None)


def test_a_selected_or_unreached_candidate_carries_no_reason():
    """A node that was not judged cannot be given a reason for losing."""
    with pytest.raises(ValueError, match="must carry no reason"):
        candidate_fate.validate_fate(
            candidate_fate.FATE_NOT_REACHED, candidate_fate.REASON_BELOW_CUTOFF
        )
    with pytest.raises(ValueError, match="must carry no reason"):
        candidate_fate.validate_fate(
            candidate_fate.FATE_SELECTED, candidate_fate.REASON_BELOW_CUTOFF
        )


def test_an_unknown_fate_is_refused():
    with pytest.raises(ValueError, match="unknown fate"):
        candidate_fate.validate_fate("maybe", None)


def test_is_rejection_reason_is_exact():
    assert candidate_fate.is_rejection_reason(candidate_fate.REASON_BELOW_CUTOFF)
    assert not candidate_fate.is_rejection_reason("BELOW_CUTOFF")
    assert not candidate_fate.is_rejection_reason(None)
    assert not candidate_fate.is_rejection_reason(0)
