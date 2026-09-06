"""The Builder printed `sc_01KZ…` where a person expects "Views".

This was not a missing label: `_visualization_options` wrote LITERALLY
`"label": str(e.get("id"))`. The member rail therefore rendered the concept
identifier as the name, on every measure and every dimension -- revision 7 of the
2026-08-12 journey report.

The label is read off the PINNED concept version, never off the concept: that is
the version the query selected, and it is the same word `query-facets` serves to
the screen next door. The two surfaces therefore cannot name one thing two ways.

THE IDENTIFIER REMAINS THE FALLBACK, and a test pins it: a concept with no
recorded label shows its identifier. That is ugly and true -- the alternative
would be to invent a name.

2026-08-21 -- THE SAME DEFECT CAME BACK THROUGH THE SECOND CONTRACT, and this
file is why it could. The 2026-08-12 rule was written as a prefix deny-list
(`sc_`, `scv_`) on the `query-spec.v1` path only, and the cross-source fixture
below named its field `"revenue"` -- a readable word no deny-list could ever
catch. So when `85c8005a` taught the resolver `multi-source-plan.v1`, its labels
went back to being canonical ids (`mdm_<ULID>`, `canonical_field_registry.py:379`)
and every test here stayed green.

Two changes, and the second is the real one:
  * the cross-source fixtures mint their ids the way the registry mints them --
    `f"mdm_{ULID()}"`, the exact expression of `canonical_field_registry.py:379`
    -- so a fixture can never again be readable where production is opaque;
  * the rule is `label != id`, asserted BOTH WAYS and on BOTH contracts. A prefix
    list only ever catches the prefixes somebody already knew about.

2026-08-21, LATER THE SAME DAY -- THE SECOND CHANGE ABOVE LOST A TOOTH. Replacing
the prefix assertion with `label != id` was written as a strengthening, but the
prefix assertion was DELETED rather than kept, and `label != id` only ever sees
ONE identifier: the one the member carries. Measured on this file as it stood,
all three of these PASSED:

    {'id': 'sc_01EXAMPLE00000000000001', 'label': 'scv_01EXAMPLE00000000000001'}
    {'id': 'm_mdm_01AAAA',               'label': 'svv_01KZXYZ'}
    {'id': 'm_mdm_01BBBB',               'label': 'proj_EXAMPLE'}

Every identifier that is not this member's own walked through -- which is the
mode by which the defect came back in the first place. Both halves are held now:
`label != id` for the envelope, and the SHAPE of a product identifier for
everything else. The shape is DERIVED from the expressions that mint the ids
(`minted_identifier_prefixes`), so it gains the next family the day it is minted;
retyping a list here is what failed on 2026-08-12.
"""

from __future__ import annotations

import json

import pytest
from core import visualization_specs, visualization_specs_api
from starlette.requests import Request
from ulid import ULID

from tests.support.minted_identifiers import (
    identifier_shape,
    minted_identifier_prefixes,
    unaccounted_ulid_uses,
    unresolved_mint_sites,
)

PROJECT = "proj_EXAMPLE"
QUERY_SPEC = "qs_01EXAMPLE0000000000000000"
QUERY_SPEC_VERSION = "qsv_01EXAMPLE000000000000000"
VIEW = "sv_01EXAMPLE0000000000000000"
VIEW_VERSION = "svv_01EXAMPLE000000000000000"

VIEWS = "sc_01EXAMPLE0000000000000001"
VIEWS_V = "scv_01EXAMPLE00000000000001"
DATE = "sc_01EXAMPLE0000000000000002"
DATE_V = "scv_01EXAMPLE00000000000002"

#: MINTED THE WAY PRODUCTION MINTS THEM -- `canonical_field_registry.py:379` is
#: literally `field_id = f"mdm_{ULID()}"`. Spelling a readable word here instead
#: (`"revenue"`, as this file did until 2026-08-21) makes every label assertion
#: below pass on a value the product never produces.
SPEND = f"mdm_{ULID()}"
REVENUE = f"mdm_{ULID()}"
ROAS = f"mdm_{ULID()}"
DAY = f"mdm_{ULID()}"

SPEC = {
    "measures": [{"id": VIEWS, "version_id": VIEWS_V}],
    "dimensions": [{"id": DATE, "version_id": DATE_V}],
}


class ScriptedCursor:
    """A cursor that returns, in order, what the handler is about to ask for."""

    def __init__(self, one=None, many=()):
        self._one = one
        self._many = list(many)

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, _sql, _params=None) -> None:
        return None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class ScriptedConnection:
    """Each `cursor()` serves the next cursor of the scenario, in order."""

    def __init__(self, cursors: list[ScriptedCursor]):
        self._cursors = list(cursors)
        self.opened = 0

    def __enter__(self) -> ScriptedConnection:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def cursor(self) -> ScriptedCursor:
        self.opened += 1
        return self._cursors.pop(0) if self._cursors else ScriptedCursor()


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/projects/{PROJECT}/analyze/visualization-options",
            "headers": [],
            "query_string": f"query_spec_version_id={QUERY_SPEC_VERSION}".encode(),
            "path_params": {"project_id": PROJECT},
        }
    )


def _install(monkeypatch, labels: list[tuple[str, str]]) -> None:
    """The pinned version, its facets, its monitors, then its labels."""
    cursors = [
        ScriptedCursor(one=(QUERY_SPEC, VIEW, VIEW_VERSION, SPEC)),
        ScriptedCursor(  # semantic_concept_versions, for the five facets
            many=[
                (VIEWS_V, "Views as the source reports them.", [], "additive", None),
                (DATE_V, "The reporting day.", ["day"], None, None),
            ]
        ),
        ScriptedCursor(many=[]),  # dq_monitors
        ScriptedCursor(many=labels),  # semantic_concept_versions, for the labels
    ]

    async def _authorized(_request, _role="viewer"):
        return ("person_01EXAMPLE00000000000000", "org_01EXAMPLE0000000000000")

    monkeypatch.setattr(visualization_specs_api, "_authorize", _authorized)
    monkeypatch.setattr(
        visualization_specs_api,
        "analyze_connection",
        lambda _identity: ScriptedConnection(cursors),
    )


@pytest.mark.anyio
async def test_members_carry_the_readable_name(monkeypatch):
    _install(monkeypatch, [(VIEWS_V, "Views"), (DATE_V, "Date")])

    payload = json.loads((await visualization_specs_api._visualization_options(_request())).body)

    assert [m["label"] for m in payload["measures"]] == ["Views"]
    assert [m["label"] for m in payload["dimensions"]] == ["Date"]


#: THE DERIVATION MOVED OUT OF THIS FILE, and gained the shape it was missing.
#: It used to be one regular expression here, reading `f"<prefix>_{ULID()}"` and
#: nothing else. The product mints through a helper just as often --
#: `_mint("mdnode")` at `master_data.py:701`, whose prefix is a literal at the
#: CALL SITE -- and every family minted that way was absent from a set whose
#: docstring said it was derived from the mints. `tests/support/minted_identifiers`
#: walks the AST instead, follows the prefix through the helper to its literal,
#: and returns as UNRESOLVED anything it cannot follow. See the exhaustiveness
#: test below: a derivation that cannot prove its own coverage is a hand-typed
#: list wearing a regular expression.

IDENTIFIER_PREFIXES = minted_identifier_prefixes()

#: THE SHAPE OF A PRODUCT IDENTIFIER -- defined ONCE, in `minted_identifiers`,
#: because the refusal suites of `multi_source_plan` assert the same rule on the
#: same families. Each of them used to write `assert "mdm_" not in message`, and
#: a hand-typed prefix names one family out of the two hundred and twenty-eight
#: this product mints: `ds_01KZ...` and `qsv_01KZ...` walked past all ten of
#: them.
IDENTIFIER_SHAPE = identifier_shape()


def assert_labels_are_names_not_identifiers(members: list[dict]) -> None:
    """THE RULE: a label is NO identifier -- not its own, and not any other.

    Two halves, and both are needed.

    `label != id`, asserted BOTH WAYS, is the half `f3ca3d50` added. It catches
    the envelope: a Result column WRAPS the id it names (`m_<field>`,
    `r_<field>`, `k_<field>`), so an unresolved `mdm_01KZ...` label is not equal
    to the `m_mdm_01KZ...` member id, it is contained in it.

    THE SHAPE TEST IS THE OTHER HALF, and `f3ca3d50` deleted it when it removed
    the `sc_`/`scv_` prefix assertion instead of keeping it. `label != id` sees
    one identifier -- this member's. Every other identifier of the product is
    different from it and therefore passes: a concept named by its own VERSION id
    (`sc_...` labelled `scv_...`), a label from a foreign family (`svv_...` on an
    `m_mdm_...` column), a project id. That is exactly the mode by which the
    defect came back the first time, so the shape is asserted too -- derived from
    the mints (`minted_identifier_prefixes`), never typed out.
    """
    assert members, "a rule asserted over an empty list proves nothing"
    for member in members:
        label, member_id = member["label"], member["id"]
        assert label, f"{member_id} carries no label at all"
        assert label != member_id, f"{member_id} is named by its own identifier"
        assert label not in member_id, f"{member_id} is named by part of its identifier"
        assert member_id not in label, f"{member_id} leaks its identifier into its label"
        leaked = IDENTIFIER_SHAPE.search(label)
        assert not leaked, (
            f"{member_id} is named by an identifier: "
            f"{label!r} carries {leaked.group().strip()!r}"
        )


#: THE THREE ATTACKS THE `label != id` RULE ALONE LETS THROUGH.
#:
#: Every one of them is a product identifier rendered where a person expects a
#: word, and every one of them differs from the id of the member carrying it --
#: which is the whole of what `label != id` can see. The first is a member named
#: by the VERSION of its own concept, the second a label from a different family
#: entirely, the third a project id. They are written here as data so the rule
#: below is asserted against them and not merely described.
FOREIGN_IDENTIFIER_ATTACKS = [
    {"id": "sc_01EXAMPLE00000000000001", "label": "scv_01EXAMPLE00000000000001"},
    {"id": "m_mdm_01AAAA", "label": "svv_01KZXYZ"},
    {"id": "m_mdm_01BBBB", "label": "proj_EXAMPLE"},
]


def test_the_identifier_shape_is_derived_from_the_mints_and_is_not_empty():
    """A derivation that quietly yields nothing disarms the rule without failing.

    The families named here are the ones the two contracts actually put on the
    rail; they are asserted PRESENT, not asserted to be the whole set -- the whole
    set is whatever the mints say today.
    """
    assert {"sc", "scv", "sv", "svv", "mdm", "proj", "org"} <= IDENTIFIER_PREFIXES
    assert len(IDENTIFIER_PREFIXES) > 100, "the scan found almost nothing: the mints moved"


def test_every_mint_in_the_product_was_followed_to_a_literal_prefix():
    """THE EXHAUSTIVENESS PROOF, and the reason this file no longer holds a regex.

    A derivation that reads one SHAPE of mint is a hand-typed list one level
    down: it covers the families somebody wrote in the shape somebody expected,
    and says nothing about the rest. The first derivation read
    `f"<prefix>_{ULID()}"` and missed every family minted through a helper --
    `_mint("mdnode")`, `_mint("grs")`, `_mint("aip")`, `_mint("scs")`,
    `_mint("ctco")` -- while its docstring claimed it was read off the mints.

    So the walk reports what it could NOT follow, and this is the assertion that
    reads it. Two residues, and both must be empty:

      * a mint expression whose prefix the walk could not reach a literal for;
      * a `ULID` glued to something by an operator or a method call -- a mint in
        a shape the walk does not read at all.

    Either one comes back with its file and its line, so the day somebody mints
    a new family in a new way, THIS test names it. Nobody has to remember to
    add it to a list, which is the only property that distinguishes an
    instrument from an enumeration.
    """
    unresolved = unresolved_mint_sites()
    assert not unresolved, (
        "the prefix of these mints was never followed to a literal: "
        + ", ".join(f"{site.where} -- {site.source}" for site in unresolved)
    )

    unaccounted = unaccounted_ulid_uses()
    assert not unaccounted, (
        "these mint an identifier in a shape the derivation does not read, so their "
        "family is missing from IDENTIFIER_PREFIXES: "
        + ", ".join(f"{site.where} -- {site.source}" for site in unaccounted)
    )


def test_the_rule_refuses_a_label_from_EVERY_family_the_product_mints():
    """The attack list, swept instead of typed.

    `FOREIGN_IDENTIFIER_ATTACKS` holds three members somebody sat down and
    wrote. That is the right way to pin the three modes, and the wrong way to
    cover a product that mints hundreds of families: the family added tomorrow
    is in nobody's list. So the attack is BUILT from the walk -- one label per
    minted family -- and the rule must refuse every one of them. A family that
    slips out of `IDENTIFIER_PREFIXES` stops being refused here on the same run.
    """
    escaped = [
        prefix
        for prefix in sorted(IDENTIFIER_PREFIXES)
        if not _refuses({"id": "m_mdm_01AAAA", "label": f"{prefix}_01KZEXAMPLE"})
    ]
    assert not escaped, f"a label carrying these families is not refused: {escaped}"


def _refuses(member: dict) -> bool:
    try:
        assert_labels_are_names_not_identifiers([member])
    except AssertionError:
        return True
    return False


def test_the_helper_form_of_a_mint_reaches_the_derived_set():
    """The five families the regex could not see, asserted by name.

    They are named here because they are the MEASURED gap of the 2026-08-21
    review, not because the set is typed out: each is minted by
    `_mint("<prefix>")` in a different module, and each was absent from
    `IDENTIFIER_PREFIXES` while the derivation was a regular expression over the
    f-string. If the walk ever loses the helper hop again, these five go first.
    """
    assert {"mdnode", "grs", "aip", "scs", "ctco"} <= IDENTIFIER_PREFIXES


@pytest.mark.parametrize("member", FOREIGN_IDENTIFIER_ATTACKS)
def test_the_rule_refuses_a_label_that_is_any_identifier(member):
    """A label is no identifier -- not only not its own.

    `f3ca3d50` replaced the `sc_`/`scv_` prefix assertion with `label != id` in
    both directions and DELETED the prefix one. `label != id` catches the
    envelope (`m_<field>` wrapping the id it names) and nothing else: any
    identifier that is not this member's own walks straight past it, which is
    exactly the mode by which the defect came back the first time.
    """
    with pytest.raises(AssertionError):
        assert_labels_are_names_not_identifiers([member])


@pytest.mark.anyio
async def test_no_member_label_is_ever_its_own_identifier(monkeypatch):
    """The assertion that would have failed before the fix, written as a rule."""
    _install(monkeypatch, [(VIEWS_V, "Views"), (DATE_V, "Date")])

    payload = json.loads((await visualization_specs_api._visualization_options(_request())).body)

    assert_labels_are_names_not_identifiers(payload["measures"] + payload["dimensions"])


@pytest.mark.anyio
async def test_a_member_without_a_recorded_label_falls_back_to_its_identifier(monkeypatch):
    """The fallback is ugly and true: it does not manufacture a name."""
    _install(monkeypatch, [(DATE_V, "Date")])

    payload = json.loads((await visualization_specs_api._visualization_options(_request())).body)

    assert payload["measures"][0]["label"] == VIEWS
    assert payload["dimensions"][0]["label"] == "Date"


#: A cross-source plan as `multi_source_plan.compile_plan` writes it: canonical
#: ids everywhere, a `canonical_name` frozen on the conformed key only. Nothing
#: in `members[].measures[]` nor in `derived_measures[]` carries a word, which is
#: why the label has to be resolved server-side against the vocabulary.
MULTI_SOURCE_SPEC = {
    "contract_version": "multi-source-plan.v1",
    "members": [{"measures": [{"canonical_field_id": SPEND, "result_field": f"m_{SPEND}"}]}],
    "derived_measures": [{"canonical_field_id": ROAS, "result_field": f"r_{ROAS}"}],
    "edges": [{"components": [{"canonical_field_id": DAY, "canonical_name": "Reporting day"}]}],
    "bounds": {"row_limit": 750},
}

#: What `app.mdm_canonical_fields` answers for those ids: `(id, canonical_name,
#: status)`. The third column arrived with `load_canonical_field_identities` and
#: is UNFILTERED on purpose -- an immutable plan pinned these fields, and
#: archiving one must not put its ULID back on the rail
#: (`visualization-and-rendering.md`, *A member's label is a name*).
VOCABULARY = [
    (SPEND, "Ad spend", "active"),
    (ROAS, "Return on ad spend", "active"),
    (DAY, "Reporting day", "active"),
]


def _install_multi_source(monkeypatch, spec: dict, vocabulary: list[tuple[str, str]]):
    """The pinned cross-source version, then the canonical-name read behind it."""
    connection = ScriptedConnection(
        [
            ScriptedCursor(one=(QUERY_SPEC, VIEW, VIEW_VERSION, spec)),
            ScriptedCursor(many=vocabulary),  # app.mdm_canonical_fields
        ]
    )

    async def _authorized(_request, _role="viewer"):
        return ("person_01EXAMPLE00000000000000", "org_01EXAMPLE0000000000000")

    monkeypatch.setattr(visualization_specs_api, "_authorize", _authorized)
    monkeypatch.setattr(
        visualization_specs_api, "analyze_connection", lambda _identity: connection
    )
    monkeypatch.setattr(
        visualization_specs_api, "load_member_presentation_metadata", lambda *_a, **_kw: {}
    )
    monkeypatch.setattr(visualization_specs_api, "load_member_labels", lambda *_a, **_kw: {})
    return connection


@pytest.mark.anyio
async def test_multi_source_options_expose_only_executed_fields_and_inherited_context(monkeypatch):
    context = {
        "contract_version": "analysis-context.v1",
        "semantic_view_version_id": VIEW_VERSION,
        "business_domain": {"id": "bd_paid", "version_number": 2, "name": "Paid media"},
        "golden_question": {"id": "gq_roas", "version_number": 3, "title": "Is ROAS healthy?"},
        "requested_skills": [],
    }
    spec = {**MULTI_SOURCE_SPEC, "analysis_context": context}
    _install_multi_source(monkeypatch, spec, VOCABULARY)

    payload = json.loads((await visualization_specs_api._visualization_options(_request())).body)

    assert payload["measures"] == [
        {
            "id": f"m_{SPEND}",
            "version_id": "",
            "label": "Ad spend",
            "role": "measure",
            "metadata": {},
        },
        {
            "id": f"r_{ROAS}",
            "version_id": "",
            "label": "Return on ad spend",
            "role": "measure",
            "metadata": {},
        },
    ]
    assert payload["dimensions"] == [
        {
            "id": f"k_{DAY}",
            "version_id": "",
            "label": "Reporting day",
            "role": "dimension",
            "metadata": {},
        }
    ]
    assert payload["row_limit"] == 750
    assert payload["analysis_context"] == context


@pytest.mark.anyio
async def test_no_cross_source_member_label_is_ever_its_own_identifier(monkeypatch):
    """THE GUARD THE 2026-08-12 ONE COULD NOT BE: the second contract, real ids.

    `85c8005a` reintroduced `labels[result_field] = field_id` on this branch and
    the suite stayed green, because the fixture spelled its field `"revenue"`.
    Minted ids and the `label != id` rule together make that impossible: with the
    resolution removed, every measure here is labelled `mdm_<ULID>` and this test
    is the one that says so.
    """
    _install_multi_source(monkeypatch, MULTI_SOURCE_SPEC, VOCABULARY)

    payload = json.loads((await visualization_specs_api._visualization_options(_request())).body)

    assert_labels_are_names_not_identifiers(payload["measures"] + payload["dimensions"])


@pytest.mark.anyio
async def test_a_cross_source_member_without_a_canonical_name_falls_back_to_its_identifier(
    monkeypatch,
):
    """The fallback is ugly and true, exactly as it is on `query-spec.v1`.

    A vocabulary row that no longer exists is an absence, and an absence is shown
    -- never replaced by a manufactured word.
    """
    _install_multi_source(monkeypatch, MULTI_SOURCE_SPEC, [(DAY, "Reporting day", "active")])

    payload = json.loads((await visualization_specs_api._visualization_options(_request())).body)

    assert [m["label"] for m in payload["measures"]] == [SPEND, ROAS]
    assert [d["label"] for d in payload["dimensions"]] == ["Reporting day"]


def test_multi_source_binding_check_names_members_the_same_way_the_rail_does():
    """ONE PRODUCER, asserted: the rail and the binding check cannot drift.

    `load_pinned_query_spec_version` is what refuses `unknown_member`; it and
    `_visualization_options` now read the same resolver, so a member offered
    under a name is bound under that same name.
    """
    connection = ScriptedConnection(
        [
            ScriptedCursor(one=(QUERY_SPEC, VIEW, VIEW_VERSION, MULTI_SOURCE_SPEC)),
            ScriptedCursor(many=VOCABULARY),
        ]
    )

    pinned = visualization_specs.load_pinned_query_spec_version(
        connection, project_id=PROJECT, query_spec_version_id=QUERY_SPEC_VERSION
    )

    assert pinned.labels == {
        f"m_{SPEND}": "Ad spend",
        f"r_{ROAS}": "Return on ad spend",
        f"k_{DAY}": "Reporting day",
    }
    for member_id, label in pinned.labels.items():
        assert label != member_id and label not in member_id


def test_multi_source_visualization_validation_uses_source_qualified_result_fields():
    spec = {
        "contract_version": "multi-source-plan.v1",
        "members": [
            {"measures": [{"canonical_field_id": REVENUE, "result_field": "m_ds_paid_revenue"}]},
            {"measures": [{"canonical_field_id": REVENUE, "result_field": "m_ds_shop_revenue"}]},
        ],
        "edges": [],
        "bounds": {"row_limit": 1000},
    }
    connection = ScriptedConnection(
        [
            ScriptedCursor(one=(QUERY_SPEC, VIEW, VIEW_VERSION, spec)),
            ScriptedCursor(many=[(REVENUE, "Revenue", "active")]),
        ]
    )

    pinned = visualization_specs.load_pinned_query_spec_version(
        connection,
        project_id=PROJECT,
        query_spec_version_id=QUERY_SPEC_VERSION,
    )

    assert pinned.roles == {
        "m_ds_paid_revenue": "measure",
        "m_ds_shop_revenue": "measure",
    }
    # ONE CANONICAL FIELD, TWO COLUMNS, ONE NAME. The columns stay distinct -- the
    # Result must keep which source produced which number -- and both read
    # "Revenue", which is what the person asked two sources for.
    assert pinned.labels == {
        "m_ds_paid_revenue": "Revenue",
        "m_ds_shop_revenue": "Revenue",
    }
