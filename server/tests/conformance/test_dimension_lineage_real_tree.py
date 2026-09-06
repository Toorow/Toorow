"""Stories 27.8 / 27.9 -- the lineage, proven against the REPOSITORY, not against fixtures.

WHY THIS FILE EXISTS. `tests/core/test_dimension_lineage.py` has 35 proofs and every one
of them asserts on manifests the test itself writes into `tmp_path`. They prove the
FUNCTIONS behave; they cannot prove the repository does. A control pass on 2026-07-31
measured the gap that hid behind them: 38 shipped manifests declared 135 canonical
targets and NOT ONE was a language, so `get_fed_by` returned an empty `fed_by` for the
whole family -- including for the very example that justifies Story 27.9. A synthetic
fixture cannot fail that way, which is exactly why it must not be the only witness.

So everything below reads `server/modules` with NO argument, the way production does.

THE INVARIANT THAT MATTERS MOST is not "language is bound" -- it is that a manifest may
only bind what the evidence SETTLES. A manifest entry is a static, repository-level
claim: it is applied to every project, and nobody confirms it. `core.language_dimensions`
classifies each catalog field from the provider's own words and returns `pending` when
those words do not say whether the field is an observation or a targeting setting.
Binding a `pending` field here would be precisely the auto-binding-on-name-similarity
that the story forbids -- committed once, invisible forever. The test therefore walks the
WHOLE family across the WHOLE tree rather than checking the connectors we happened to
touch: the rule is about the class, not about five files.
"""

from __future__ import annotations

import json
from pathlib import Path

from core import dimension_lineage as dl
from core import language_dimensions as ld

_MODULES_DIR = Path(__file__).resolve().parents[2] / "modules"

_FAMILY = (ld.AUDIENCE_LANGUAGE, ld.CONTENT_LANGUAGE, ld.TARGETING_LANGUAGE)


def _real_index() -> dict[str, dict[str, str]]:
    """The index production builds: no argument, no fixture, the shipped tree."""
    return dl.read_manifest_dimension_mappings()


def _catalog_fields(connector: str) -> dict[str, tuple[dict, ...]]:
    """{source_field -> EVERY declaration of it} in one connector's shipped catalog.

    THIS USED TO KEEP THE FIRST OCCURRENCE ONLY (`fields.setdefault`). Measured on
    2026-08-22: 11 shipped catalogs declare 50 field ids more than once -- stripe
    declares `created` seven times, square declares `status` three times. A field
    declared twice can carry two different descriptions, and the provider's words are
    the whole evidence these tests judge on: keeping the first meant the verdict
    depended on the order of a JSON file, and one declaration that settles a language
    while another leaves it open would pass on ordering alone.
    """
    path = _MODULES_DIR / connector / "api_catalog.json"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        catalog = json.load(handle)
    fields: dict[str, list[dict]] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            name = node.get("source_field") or node.get("field_id")
            if isinstance(name, str) and name:
                fields.setdefault(name, []).append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(catalog)
    return {name: tuple(nodes) for name, nodes in fields.items()}


# ---------------------------------------------------------------------------
# A. The reader works on the repository, not only on tmp_path.
# ---------------------------------------------------------------------------


def test_the_default_modules_dir_is_the_shipped_tree_and_it_is_readable():
    index = _real_index()
    # Not an arbitrary threshold: it must simply be the real tree rather than an empty
    # dict silently returned by the OSError branch, which is how this reader fails.
    assert len(index) > 20, "read_manifest_dimension_mappings() did not reach server/modules"
    assert set(index) <= {d.name for d in _MODULES_DIR.iterdir() if d.is_dir()}


def test_every_shipped_canonical_target_is_a_usable_identifier():
    # Class-wide shape check: one malformed target anywhere silently drops a field out
    # of the lineage, and a per-connector test would never see it.
    for connector, fields in _real_index().items():
        for source_field, target in fields.items():
            where = f"{connector}.{source_field}"
            assert isinstance(target, str) and target.strip(), where
            assert target == target.strip().lower(), f"{where}: targets are stable ids"


# ---------------------------------------------------------------------------
# B. The language family is REACHABLE -- the measurement of 2026-07-31, inverted.
# ---------------------------------------------------------------------------


def test_each_member_of_the_language_family_is_fed_by_at_least_one_real_manifest():
    index = _real_index()
    bound: dict[str, list[str]] = {member: [] for member in _FAMILY}
    for connector, fields in index.items():
        for source_field, target in fields.items():
            if target in bound:
                bound[target].append(f"{connector}.{source_field}")
    for member, sources in bound.items():
        assert sources, (
            f"no shipped manifest maps any field to '{member}' -- the family is "
            "unreachable by the inverse lineage, which is the 2026-07-31 defect"
        )


def test_a_manifest_never_binds_a_field_the_evidence_leaves_undecided():
    """THE invariant: static binding is allowed only where the provider's words settle it.

    Nothing here lists connectors or fields. The expectation is derived, per field, from
    the classifier reading that connector's own catalog -- so a manifest that binds a new
    language field tomorrow is judged by the same rule, without editing this test.
    """
    for connector, fields in _real_index().items():
        catalog = None
        for source_field, target in fields.items():
            if target not in _FAMILY:
                continue
            if catalog is None:
                catalog = _catalog_fields(connector)
            declarations = catalog.get(source_field)
            assert declarations, (
                f"{connector}.{source_field} -> {target}: the manifest binds a field that "
                "is not in the shipped catalog, so the claim cannot be checked by anyone"
            )
            # EVERY declaration, not the first one: a field declared twice must be
            # settled twice, or the catalog contradicts itself and nothing may bind it.
            for index, field in enumerate(declarations):
                where = f"{connector}.{source_field}[{index}]"
                proposal = ld.propose_binding(field, connector=connector)
                assert proposal.status == ld.STATUS_PROPOSED, (
                    f"{where} is '{proposal.status}'"
                    f"{f' ({proposal.pending_reason})' if proposal.pending_reason else ''} -- "
                    "a manifest may not settle statically what the evidence leaves open; "
                    "that decision belongs to the client, in app.dimension_field_bindings"
                )
                assert proposal.canonical_dimension == target, (
                    f"{where}: manifest says '{target}', the provider's own "
                    f"description says '{proposal.canonical_dimension}'"
                )


def test_a_field_the_classifier_leaves_pending_is_absent_from_every_manifest():
    """The mirror of the rule above, walked from the CATALOGS rather than the manifests.

    Coming from the other side matters: the test above can only see fields somebody chose
    to bind. This one starts from every language-ish field the repository ships and checks
    that the undecided ones were left alone.
    """
    index = _real_index()
    for catalog_path in sorted(_MODULES_DIR.glob("*/api_catalog.json")):
        connector = catalog_path.parent.name
        for source_field, declarations in _catalog_fields(connector).items():
            for field in declarations:
                if not ld.is_language_candidate(field):
                    continue
                proposal = ld.propose_binding(field, connector=connector)
                if proposal.status == ld.STATUS_PROPOSED:
                    continue
                assert index.get(connector, {}).get(source_field) not in _FAMILY, (
                    f"{connector}.{source_field} is '{proposal.status}' yet a manifest binds "
                    "it to the family -- silence is the honest answer, not a guess"
                )


# ---------------------------------------------------------------------------
# C. The end of the chain: a real manifest actually produces fed-by rows,
#    at REPORT grain -- the claim Story 27.9 is named after.
#
# REWRITTEN 2026-08-22. What stood here fabricated its evidence twice over:
# `_one_real_binding` took the first manifest entry for a family member WITHOUT
# asking the catalog whether that field can be selected at all, and `_plan_row`
# invented report ids ("report_a", "report_b") that no shipped profile carries. The
# only `content_language` / `targeting_language` fields it reached are
# `exposure: excluded` in `amazon-ads/api_catalog.json` and
# `amazon-dsp/api_catalog.json` -- "DSP seat required" -- so the proof that those two
# members are fed rested on plans no project can ever submit. A fixture that cannot
# fail the way the repository fails is not a witness.
#
# Everything below derives from what the tree DECLARES SELECTABLE: a field whose
# every catalog declaration says `exposure: exposed`, and a report id taken from that
# connector's own `report_profiles`. Where nothing is selectable, the test says so --
# unreachable is a fact to state, not a gap to paper over.
# ---------------------------------------------------------------------------


def _selectable_bindings(member: str) -> list[tuple[str, str]]:
    """[(connector, source_field)] feeding *member* that a project can actually pull.

    EVERY declaration of the field must say `exposed`. One `excluded` declaration is
    the connector saying this field is out of reach, and an "exposed somewhere else in
    the file" is precisely the ordering-dependent verdict section A stopped accepting.
    """
    found: list[tuple[str, str]] = []
    for connector, fields in sorted(_real_index().items()):
        catalog = None
        for source_field, target in sorted(fields.items()):
            if target != member:
                continue
            if catalog is None:
                catalog = _catalog_fields(connector)
            declarations = catalog.get(source_field)
            if not declarations:
                continue
            if all(d.get("exposure") == "exposed" for d in declarations):
                found.append((connector, source_field))
    return found


def _real_report_ids(connector: str) -> list[str]:
    """The report ids this connector SHIPS, in manifest order."""
    path = _MODULES_DIR / connector / "manifest.json"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    return [
        str(profile["id"])
        for profile in manifest.get("report_profiles") or ()
        if isinstance(profile, dict) and profile.get("id")
    ]


def _plan_row(connector: str, source_field: str, report_id: str, suffix: str) -> dict:
    return {
        "datastream_id": f"ds_EXAMPLE_{suffix}",
        "datastream_name": f"example {suffix}",
        "connector": connector,
        "plan_version_id": f"dpv_EXAMPLE_{suffix}",
        "version_number": 1,
        "enabled": True,
        "archived": False,
        "payload": {
            "source": {
                "report_id": report_id,
                "selection": {"dimensions": [source_field]},
            }
        },
    }


def test_a_manifest_never_binds_a_field_the_connector_cannot_explain():
    """A binding to a field nobody can select is a lineage row nobody can ever produce.

    This does NOT refuse the binding: a manifest may legitimately describe a field that
    becomes reachable the day an account exists. It refuses the SILENCE -- every such
    field carries the connector's own reason for being out of reach, because a reason
    is what a person acts on.
    """
    for connector, fields in sorted(_real_index().items()):
        catalog = None
        for source_field, target in sorted(fields.items()):
            if target not in _FAMILY:
                continue
            if catalog is None:
                catalog = _catalog_fields(connector)
            for index, declaration in enumerate(catalog.get(source_field) or ()):
                if declaration.get("exposure") == "exposed":
                    continue
                assert declaration.get("exclusion_reason"), (
                    f"{connector}.{source_field}[{index}] is "
                    f"{declaration.get('exposure')!r} and says why nowhere -- a field out "
                    "of reach without a reason cannot be brought into reach"
                )


def test_the_family_members_no_project_can_pull_are_named_as_such():
    """Which members are reachable TODAY, stated rather than assumed.

    `content_language` and `targeting_language` are fed only by Amazon fields gated
    behind a DSP seat. The honest report is that they are unreachable; the day a module
    exposes a real one this test changes on its own, because it lists nothing.
    """
    reachable = {member: _selectable_bindings(member) for member in _FAMILY}
    assert reachable[ld.AUDIENCE_LANGUAGE], (
        "no shipped connector exposes a selectable field feeding audience_language -- "
        "the whole family is then unreachable and section C proves nothing"
    )
    for member, bindings in reachable.items():
        if bindings:
            continue
        # Unreachable, and it must be BECAUSE of a declared exclusion -- never because
        # the manifest simply bound nothing.
        bound = [
            (connector, field)
            for connector, fields in _real_index().items()
            for field, target in fields.items()
            if target == member
        ]
        assert bound, f"{member!r} is bound by no manifest at all, not merely out of reach"


def test_the_same_field_pulled_from_two_reports_yields_two_rows_not_one():
    """The report grain is the point of 27.9, proven on a SELECTABLE field and REAL reports.

    The story's own example (a field offered by six report types) stays unreachable:
    that connector's field is `pending` by design, and binding it is the fault 27.8
    forbids. The grain claim does not depend on that connector.
    """
    candidates = [
        (connector, source_field)
        for connector, source_field in _selectable_bindings(ld.AUDIENCE_LANGUAGE)
        if len(_real_report_ids(connector)) >= 2
    ]
    assert candidates, (
        "no connector both exposes a selectable audience_language field and ships two "
        "report profiles -- the report grain cannot be proven on the real tree"
    )
    connector, source_field = candidates[0]
    first, second = _real_report_ids(connector)[:2]
    uses, gaps = dl.extract_plan_field_uses(
        [
            _plan_row(connector, source_field, first, "a"),
            _plan_row(connector, source_field, second, "b"),
        ],
        manifest_index=_real_index(),
        canonical_dimension=ld.AUDIENCE_LANGUAGE,
    )
    assert not gaps
    fed_by = dl.build_fed_by(
        canonical_dimension=ld.AUDIENCE_LANGUAGE,
        label={},
        plan_uses=uses,
        gaps=gaps,
        mapping_rows=[],
        project_id="proj_EXAMPLE",
    )
    rows = fed_by["fed_by"]
    assert len(rows) == 2, "the report grain was collapsed back to the connector"
    assert {row["report_id"] for row in rows} == {first, second}
    assert {row["source_field"] for row in rows} == {source_field}
    assert {row["connector"] for row in rows} == {connector}


def test_a_reachable_member_is_fed_by_something_and_an_unreachable_one_is_not_faked():
    """Before 2026-08-01 this looped three times and asserted nothing but zeroes; until
    2026-08-22 it asserted three greens, two of them on plans no project can submit."""
    index = _real_index()
    proven: list[str] = []
    for member in _FAMILY:
        bindings = _selectable_bindings(member)
        if not bindings:
            continue
        connector, source_field = bindings[0]
        reports = _real_report_ids(connector) or ["unknown"]
        uses, _ = dl.extract_plan_field_uses(
            [_plan_row(connector, source_field, reports[0], "a")],
            manifest_index=index,
            canonical_dimension=member,
        )
        fed_by = dl.build_fed_by(
            canonical_dimension=member,
            label={},
            plan_uses=uses,
            gaps=[],
            mapping_rows=[],
            project_id="proj_EXAMPLE",
        )
        assert fed_by["fed_by"], f"{member!r} is selectable yet fed by nothing"
        proven.append(member)
    assert proven, "not one member of the family is provable on a plan a project can run"
