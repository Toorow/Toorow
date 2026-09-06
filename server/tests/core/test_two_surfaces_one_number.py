"""Story 53.9 — two surfaces, one number. The promise, held on BOTH of them.

`caveats-register.md:74` (CAV-17) names two analytical paths that answer the same
business question: `core.query_execution` reads the Datastream's published output
relation and pins its Semantic View and Query Spec versions on the Result;
`cards.py` reads an aggregate mart. The story chose DISCLOSURE over convergence —
"Soit une source unique, soit une divulgation explicite de l'écart"
(`planning-artifacts/epic-53-caveats-non-enonces.md:225`).

What shipped on 2026-07-31 disclosed on one surface. `meta.analytical_path` was
stamped inside `build_canonical_envelope`, whose single production caller is
`get_daily_report` in `core/main.py` — the only call in the repository
(`grep -rn "build_canonical_envelope" server/core`). The module CAV-17 actually
names, `cards.py`, assembled its four envelopes as dict literals and reached
`core.envelope` never, so `get_card` shipped a `fact_daily_kpi` figure that named
no engine. Removing the field from those four envelopes cost zero tests, because
the meta-key contract lived in
`test_branding.py::test_envelope_omits_branding_key_entirely_when_none`, pinned
on the constructor nobody on that surface called.

These tests hold three things that each redden on their own:
  1. no envelope on the figure-bearing path is built by hand (structural);
  2. both surfaces declare an analytical path, and agree on the mart's identity;
  3. every card declares the relation IT read, not the one its neighbour read.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core import cards as cards_module  # noqa: E402
from core.envelope import (  # noqa: E402
    ANALYTICAL_PATH_KEYS,
    ANALYTICAL_PATH_MART,
    build_canonical_envelope,
    build_envelope,
    declare_analytical_path,
    derive_meta_from_rows,
)

_CORE = Path(__file__).resolve().parents[2] / "core"

#: The ONE module allowed to write the envelope literal: the constructor itself.
_ENVELOPE_CONSTRUCTOR = "envelope.py"

#: The figure-bearing path of CAV-17: the two surfaces that answer the same
#: business question with a number, plus the constructor they share.
#:
#: Scoped, and the scope is STATED rather than assumed. `_envelope_literals`
#: applied to all of `server/core` finds 19; the other 14 belong to MCP tools
#: that return state rather than a business figure (diagnosis, recovery, mapping
#: proposals, governance...), and to `reports.render_report` and the two
#: `report_ref == "adhoc"` notebook branches, which ARE figure-bearing and are
#: named in the CAV-17 row as the same class left untreated (AI-274). Widening
#: this tuple is the repair; loosening it is not.
#:
#: WIDENED 2026-08-16 (AI-275). The three the CAV-17 row named as left untreated
#: -- `reports.render_report` and the two `report_ref == "adhoc"` branches of
#: `notebook_mcp` -- now build through the constructor and declare the relation
#: they actually read. The ad-hoc branches declare `relation="none"`, which is a
#: fact about the path rather than an absence of one: a notebook with no report
#: definition reads nothing, and saying so is what lets a reader compare two
#: declarations instead of one declaration and one silence.
_FIGURE_BEARING_MODULES = ("cards.py", "envelope.py", "reports.py", "notebook_mcp.py")


def _envelope_literals(module: Path) -> list[int]:
    """Line numbers of dict literals shaped like an AD-1 envelope."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if {"schema_version", "meta", "data"} <= keys:
            found.append(node.lineno)
    return found


# ---------------------------------------------------------------------------
# 1. The guard: on this path, an envelope is CONSTRUCTED, never assembled.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _FIGURE_BEARING_MODULES)
def test_the_figure_bearing_path_builds_no_envelope_by_hand(module_name):
    """A hand-built envelope inherits nothing the AD-1 contract gains later.

    This is the guard the story shipped without.
    `test_branding.py::test_envelope_omits_branding_key_entirely_when_none` pins
    the meta keys on `build_canonical_envelope`; a module that assembles the dict
    itself walks past that assertion, which is exactly how four envelopes spent
    story 53.9 with no `analytical_path` and no failing line.
    """
    module = _CORE / module_name
    literals = _envelope_literals(module)

    if module_name == _ENVELOPE_CONSTRUCTOR:
        # The constructor writes the literal exactly once -- that IS its job.
        assert len(literals) == 1, (
            f"core/{module_name} writes {len(literals)} envelope literals; the "
            "constructor is one function, not a family"
        )
        return

    assert literals == [], (
        f"core/{module_name} assembles an AD-1 envelope by hand at line(s) "
        f"{literals}. Call core.envelope.build_envelope(meta=..., data=..., "
        "analytical_path=...) instead: a literal cannot inherit a contract."
    )


def test_the_constructor_refuses_an_envelope_that_names_no_relation():
    """`analytical_path` is required, so a fifth surface cannot arrive silent."""
    with pytest.raises(TypeError):
        build_envelope(meta={}, data={})  # type: ignore[call-arg]

    for bad in (
        None,
        {},
        {"path": "mart"},
        {**ANALYTICAL_PATH_MART, "relation": ""},
        {**ANALYTICAL_PATH_MART, "extra": 1},
    ):
        with pytest.raises(ValueError):
            build_envelope(meta={}, data={}, analytical_path=bad)


def test_a_path_can_never_declare_itself_governed_from_this_builder():
    """The mart side declares `False` by construction, not by remembering to."""
    path = declare_analytical_path(path="mart", relation="some_relation")
    assert path["governed_result"] is False
    assert set(path) == set(ANALYTICAL_PATH_KEYS)

    with pytest.raises(ValueError):
        declare_analytical_path(path="mart", relation="   ")

    # And the constructor refuses a declaration whose flag was retyped as a string.
    with pytest.raises(ValueError):
        build_envelope(
            meta={}, data={}, analytical_path={**ANALYTICAL_PATH_MART, "governed_result": "false"}
        )


# ---------------------------------------------------------------------------
# 2. The two surfaces, compared.
# ---------------------------------------------------------------------------


def _report_surface_envelope() -> dict:
    """Surface A: `get_daily_report`, exactly as `core.main` builds it."""
    rows = [
        {
            "date": "2026-07-05",
            "connector": "seeded-connector",
            "metric": "sessions",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        }
    ]
    freshness, provenance = derive_meta_from_rows(rows, ["seeded-connector"])
    return build_canonical_envelope(
        rows=rows,
        meta_freshness=freshness,
        meta_provenance=provenance,
        date_range={"start": "2026-07-05", "end": "2026-07-05"},
        connectors=["seeded-connector"],
    )


def _card_surface_envelope() -> dict:
    """Surface B: `get_card`, the generic path that reads the same mart."""
    rows = [
        {
            "date": "2026-07-05",
            "connector": "seeded-connector",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-06",
            "connector": "seeded-connector",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 120.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-06T00:00:00",
        },
    ]
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _summary, envelope, _uri = cards_module.get_card(
            [], "default", template="kpi", metrics=["sessions"]
        )
    return envelope


def test_both_surfaces_declare_which_engine_produced_the_number():
    """The title of the story, asserted where it was never asserted.

    Reddens the moment either surface stops declaring -- which, before this test,
    cost nothing on the card side and nothing on the report side either, since
    only one of the two was ever read.
    """
    report = _report_surface_envelope()
    card = _card_surface_envelope()

    for surface, envelope in (("get_daily_report", report), ("get_card", card)):
        assert "analytical_path" in envelope["meta"], (
            f"{surface} emits a business figure without saying which engine "
            "produced it"
        )
        assert set(envelope["meta"]["analytical_path"]) == set(ANALYTICAL_PATH_KEYS)


def test_the_two_surfaces_agree_on_the_identity_of_the_mart_they_both_read():
    """Both read `fact_daily_kpi`; they must say so in the same words.

    Two surfaces disclosing two different names for one relation is the defect
    the disclosure exists to prevent, arriving through the disclosure itself.
    """
    report = _report_surface_envelope()["meta"]["analytical_path"]
    card = _card_surface_envelope()["meta"]["analytical_path"]

    assert report == card == dict(ANALYTICAL_PATH_MART)
    assert report["governed_result"] is False


def test_neither_surface_retypes_the_relation_in_its_provenance():
    """One authority on the mart's name, on both surfaces.

    `derive_meta_from_rows` and `cards.get_card` each wrote `fact_daily_kpi` into
    `provenance.source_field` by hand; a rename would have moved the declaration
    and left both provenances describing a relation that no longer exists.
    """
    report = _report_surface_envelope()
    card = _card_surface_envelope()

    assert report["meta"]["provenance"][0]["source_field"] == ANALYTICAL_PATH_MART["relation"]
    assert card["meta"]["provenance"]["source_field"] == ANALYTICAL_PATH_MART["relation"]


# ---------------------------------------------------------------------------
# 3. Each card declares the relation IT read.
# ---------------------------------------------------------------------------


def test_every_card_path_declares_a_distinct_relation_it_actually_reads():
    """Three of the four cards do NOT read `fact_daily_kpi`.

    Stamping the mart on all four would have been a fabricated provenance wearing
    the disclosure's clothes: the connectors card reads the app control plane, the
    dedup card reads `marts.dedup_estimate`, the pacing card reads the two pacing
    marts. Each declaration must match the relation that card's own provenance
    names, and the four must not collapse into one.
    """
    declared = {
        "connectors": cards_module.ANALYTICAL_PATH_CONNECTION_HEALTH,
        "dedup": cards_module.ANALYTICAL_PATH_DEDUP_ESTIMATE,
        "mediaplan_pacing": cards_module.ANALYTICAL_PATH_PLAN_PACING,
        "kpi": ANALYTICAL_PATH_MART,
    }
    relations = [p["relation"] for p in declared.values()]
    assert len(set(relations)) == len(relations), (
        f"two card paths claim the same relation: {relations}"
    )
    for name, path in declared.items():
        assert set(path) == set(ANALYTICAL_PATH_KEYS), name
        assert path["governed_result"] is False, name
        assert "disagree" in path["note"], (
            f"the {name} declaration does not say the two paths can disagree"
        )
    # The one card CAV-17 is written about is the one that reads the mart.
    assert declared["kpi"]["relation"] == "fact_daily_kpi"
    assert declared["connectors"]["path"] == "app"


def test_the_connectors_card_carries_the_declaration_end_to_end():
    """The context cards are envelopes too, and they were the silent ones.

    No DB is patched: this card is designed to answer with an empty envelope when
    the inventory is unreadable, and the disclosure must survive that branch —
    a degraded card is exactly when a reader most needs to know what it read.
    """
    _summary, envelope, _uri = cards_module.get_card([], "default", template="connectors")
    path = envelope["meta"]["analytical_path"]
    assert path == dict(cards_module.ANALYTICAL_PATH_CONNECTION_HEALTH)
    assert envelope["meta"]["provenance"]["source_field"] == path["relation"]
