"""What a common key accepts, and the eight things it refuses by name.

Story 66.1. Proven without a database: component validation and hashing are pure,
and the refusals are the value. The store, the immutability trigger and the
derived Mapping Coverage are proven on real Postgres in
`server/tests/integration/test_mdm_common_keys_pg.py`.

Why the object exists at all, measured 2026-08-13: `grep -rn "common_key"` over
`server/`, `infra/nango/migrations/` and `ui/admin/src/` returned two hits, both a
local variable in `ai_paths.py:673`. Two Datastreams could bind the same canonical
field and no object anywhere said they therefore shared one business identity.
"""

from __future__ import annotations

import pytest
from core.mdm_common_keys import (
    KEYABLE_VALUE_TYPES,
    MAX_COMPONENTS,
    CommonKeyRefused,
    Component,
    clean_name,
    components_hash,
    resolve_components,
)

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# EVERY STATEMENT THE EXERCISED PATHS ISSUE, NAMED ONCE. `resolve_components` is
# the only entry point here that touches a connection -- `clean_name` and
# `components_hash` are pure -- and it reads exactly twice. Neither fragment can
# be widened without swallowing the other read, which is the whole point: the
# two shapes are different, and a fake that answered one to both would prove
# nothing about the physical-type comparison.
_STATEMENTS = StatementInventory(
    "test_mdm_common_keys._FakeCursor",
    # core/mdm_common_keys.py:598 -- `mapping_coverage`, the published mappings
    # whose `physical_type` the component refusal compares.
    mapping_coverage="from app.datastreams d",
    # core/canonical_field_registry.py:244 -- `list_visible_canonical_fields`,
    # the one visibility predicate a binding is validated against.
    canonical_fields="from app.mdm_canonical_fields",
)


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317. `_FakeCursor` used to answer with a ternary that had no refusal:
    anything that was not the Datastream read got the canonical field rows. A
    read added to `resolve_components` tomorrow -- the org of the project, the
    key rows themselves -- would have been handed twelve-column registry tuples
    and unpacked in silence, and this file would have stayed green while
    asserting about a branch the product never took.
    """
    assert (
        _STATEMENTS.find("SELECT id, project_id FROM app.mdm_canonical_fields WHERE x")
        == "canonical_fields"
    )

    with pytest.raises(UnknownStatement) as raised:
        _STATEMENTS.match("SELECT org_id FROM app.projects WHERE id = %s")
    # The statement that moved, and a neighbour to compare it against.
    assert "app.projects" in str(raised.value)
    assert "canonical_fields" in str(raised.value)


class _FakeConn:
    """Only what `list_visible_canonical_fields` needs, and nothing else.

    The registry read is one function and it is the seam: this fake returns the
    rows it would have returned, so the refusals below are proven against the
    exact shape production hands the resolver.
    """

    def __init__(self, rows, datastreams=()):
        self._rows = rows
        self._datastreams = list(datastreams)

    def cursor(self):
        return _FakeCursor(self._rows, self._datastreams)


class _FakeCursor:
    """The two reads of `resolve_components`, and a REFUSAL for everything else.

    AI-317. It used to answer with a ternary -- the Datastream rows when the
    statement named `app.datastreams`, the canonical field rows otherwise -- so
    every statement it had never been taught got the registry shape and no
    warning. `_STATEMENTS.match` raises instead, naming the query.
    """

    def __init__(self, rows, datastreams=()):
        self._rows = rows
        # `resolve_components` now reads the published mappings too, to compare
        # the physical types implementing each component. Two reads, two shapes:
        # a fake that answered one shape to both would prove nothing.
        self._datastreams = list(datastreams)
        self._result: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, _params=None):
        statement = _STATEMENTS.match(sql)
        # Derived, never spelled out: a hand-written column tuple is a second
        # copy of the product's projection, and it is the copy that rots first.
        self.description = describe(sql)
        match statement:
            case "mapping_coverage":
                self._result = self._datastreams
            case "canonical_fields":
                self._result = self._rows

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


def _field(field_id, name, kind="dimension", value_type="string"):
    # The column order of `canonical_field_registry._READ_COLUMNS`.
    return (
        field_id, "proj_EXAMPLE", name, kind, value_type, None, False, None, None,
        None, None, "active",
    )


def _conn(*fields, datastreams=()):
    return _FakeConn(list(fields), datastreams)


def _datastream(name, *bindings):
    """One published Datastream and the columns its mapping binds.

    `bindings` are `(mdm_target, physical_type)` pairs, resolved.
    """
    return (
        f"ds_{name}",
        name,
        f"dsmv_{name}",
        {
            "fields": [
                {
                    "field_id": f"{name}_{index}",
                    "physical_type": physical_type,
                    "binding": {"mdm_target": target, "status": "resolved"},
                }
                for index, (target, physical_type) in enumerate(bindings)
            ]
        },
    )


DAY = _field("mdm_00000000000000000000000001", "day", value_type="date")
CAMPAIGN = _field("mdm_00000000000000000000000002", "campaign_id")
SPEND = _field("mdm_00000000000000000000000003", "spend", kind="metric", value_type="money")
CTR = _field("mdm_00000000000000000000000004", "ctr", value_type="ratio")
SEEN_AT = _field("mdm_00000000000000000000000005", "seen_at", value_type="timestamp")


# ---------------------------------------------------------------------------
# What it accepts
# ---------------------------------------------------------------------------


def test_one_component_is_a_legal_key():
    """`Day` alone is the common case, not a degenerate one."""
    components = resolve_components(
        _conn(DAY), project_id="proj_EXAMPLE", canonical_field_ids=[DAY[0]]
    )
    assert [c.canonical_name for c in components] == ["day"]
    assert components[0].ordinal == 0


def test_the_order_is_part_of_the_identity():
    """[Day, Campaign] and [Campaign, Day] are two keys, and the hash says so.

    Without this, a relationship pinning one version would be indistinguishable
    from a relationship pinning the other, and the physical resolution of a
    composite key depends on the order of its components.
    """
    forward = resolve_components(
        _conn(DAY, CAMPAIGN), project_id="proj_EXAMPLE", canonical_field_ids=[DAY[0], CAMPAIGN[0]]
    )
    backward = resolve_components(
        _conn(DAY, CAMPAIGN), project_id="proj_EXAMPLE", canonical_field_ids=[CAMPAIGN[0], DAY[0]]
    )
    assert components_hash(forward) != components_hash(backward)


def test_the_hash_ignores_the_names_so_a_rename_does_not_break_a_pin():
    """Renaming a canonical field does not change which identity a key expresses."""
    a = [Component(0, "mdm_A", "day", "date")]
    b = [Component(0, "mdm_A", "reporting_day", "date")]
    assert components_hash(a) == components_hash(b)


def test_a_hash_is_stable_across_calls():
    components = [Component(0, "mdm_A", "day", "date"), Component(1, "mdm_B", "campaign", "string")]
    assert components_hash(components) == components_hash(list(components))


# ---------------------------------------------------------------------------
# The eight refusals, each naming what it rejected
# ---------------------------------------------------------------------------


def test_zero_components_is_refused():
    with pytest.raises(CommonKeyRefused) as excinfo:
        resolve_components(_conn(DAY), project_id="proj_EXAMPLE", canonical_field_ids=[])
    assert excinfo.value.code == "components_required"


def test_more_than_the_bound_is_refused():
    ids = [f"mdm_{i:026d}" for i in range(MAX_COMPONENTS + 1)]
    with pytest.raises(CommonKeyRefused) as excinfo:
        resolve_components(_conn(DAY), project_id="proj_EXAMPLE", canonical_field_ids=ids)
    assert excinfo.value.code == "too_many_components"


def test_a_duplicated_component_is_refused_before_the_registry_is_read():
    with pytest.raises(CommonKeyRefused) as excinfo:
        resolve_components(
            _conn(DAY), project_id="proj_EXAMPLE", canonical_field_ids=[DAY[0], DAY[0]]
        )
    assert excinfo.value.code == "component_duplicated"


def test_an_unknown_or_foreign_component_is_one_answer():
    """Unknown, archived and foreign are indistinguishable on purpose.

    `list_visible_canonical_fields` returns only ACTIVE rows of the project and
    the platform scope, so all three arrive here as absence -- and a caller of
    another project must not learn that an id exists somewhere else.
    """
    with pytest.raises(CommonKeyRefused) as excinfo:
        resolve_components(
            _conn(DAY), project_id="proj_EXAMPLE", canonical_field_ids=["mdm_ELSEWHERE"]
        )
    assert excinfo.value.code == "component_not_found"
    assert "mdm_ELSEWHERE" in excinfo.value.message


def test_a_measure_cannot_be_a_key_component():
    with pytest.raises(CommonKeyRefused) as excinfo:
        resolve_components(
            _conn(DAY, SPEND), project_id="proj_EXAMPLE", canonical_field_ids=[SPEND[0]]
        )
    assert excinfo.value.code == "component_is_metric"
    assert "spend" in excinfo.value.message


@pytest.mark.parametrize("field", [CTR, SEEN_AT])
def test_a_non_keyable_value_type_is_refused_and_names_the_type(field):
    """A float never equals a float across two producers; an instant has no declared grain."""
    with pytest.raises(CommonKeyRefused) as excinfo:
        resolve_components(
            _conn(DAY, field), project_id="proj_EXAMPLE", canonical_field_ids=[field[0]]
        )
    assert excinfo.value.code.startswith("component_not_keyable:")
    assert excinfo.value.code.endswith(field[4])


def test_the_keyable_types_are_exactly_the_four_comparable_ones():
    assert KEYABLE_VALUE_TYPES == frozenset({"string", "integer", "date", "boolean"})


def test_a_missing_name_is_refused_with_a_sentence():
    with pytest.raises(CommonKeyRefused) as excinfo:
        clean_name("   ")
    assert excinfo.value.code == "name_required"


def test_a_name_longer_than_the_bound_is_refused():
    with pytest.raises(CommonKeyRefused) as excinfo:
        clean_name("x" * 121)
    assert excinfo.value.code == "name_too_long"


def test_every_refusal_carries_a_code_and_a_human_sentence():
    """A screen renders `message`; a caller branches on `code`. Both are required."""
    with pytest.raises(CommonKeyRefused) as excinfo:
        resolve_components(_conn(DAY), project_id="proj_EXAMPLE", canonical_field_ids=[])
    refusal = excinfo.value
    assert refusal.code and refusal.message
    assert refusal.message[0].isupper() and refusal.message.endswith(".")


# ---------------------------------------------------------------------------
# The physical types implementing a component -- `governance.md:1554`
# ---------------------------------------------------------------------------


def test_two_sources_typing_the_same_component_differently_are_refused():
    """A key over a column that is a string here and an integer there is a lie.

    `governance.md:1554` refuses the component BY NAME, and the sentence names
    both Datastreams: a refusal saying only "types disagree" leaves the person
    with two mappings and no way to know which one is wrong.
    """
    conn = _conn(
        DAY,
        CAMPAIGN,
        datastreams=(
            _datastream("google_ads", (CAMPAIGN[0], "string")),
            _datastream("meta_ads", (CAMPAIGN[0], "bigint")),
        ),
    )
    with pytest.raises(CommonKeyRefused) as excinfo:
        resolve_components(
            conn, project_id="proj_EXAMPLE", canonical_field_ids=[CAMPAIGN[0]]
        )
    assert excinfo.value.code == "component_physical_types_disagree"
    assert "google_ads" in excinfo.value.message
    assert "meta_ads" in excinfo.value.message


def test_two_spellings_of_one_type_agree():
    """`VARCHAR` and `text` are one warehouse's word against another's.

    Comparing the raw strings would refuse this, and that refusal would teach
    nobody anything.
    """
    conn = _conn(
        CAMPAIGN,
        datastreams=(
            _datastream("google_ads", (CAMPAIGN[0], "VARCHAR")),
            _datastream("meta_ads", (CAMPAIGN[0], "text")),
        ),
    )
    components = resolve_components(
        conn, project_id="proj_EXAMPLE", canonical_field_ids=[CAMPAIGN[0]]
    )
    assert [c.canonical_name for c in components] == ["campaign_id"]


def test_a_type_the_classifier_does_not_recognize_is_not_a_disagreement():
    """`unknown` is an ambiguity the mapping already raises, not evidence.

    Refusing here would answer "these two disagree" to a question nobody asked.
    """
    conn = _conn(
        CAMPAIGN,
        datastreams=(
            _datastream("google_ads", (CAMPAIGN[0], "string")),
            _datastream("weird_source", (CAMPAIGN[0], "st_geography")),
        ),
    )
    assert resolve_components(
        conn, project_id="proj_EXAMPLE", canonical_field_ids=[CAMPAIGN[0]]
    )


def test_a_suggested_binding_does_not_implement_the_component():
    """Only `confirmed`/`resolved` bind. A suggestion is not a declaration."""
    google = _datastream("google_ads", (CAMPAIGN[0], "string"))
    meta = _datastream("meta_ads", (CAMPAIGN[0], "bigint"))
    meta[3]["fields"][0]["binding"]["status"] = "suggested"
    conn = _conn(CAMPAIGN, datastreams=(google, meta))
    assert resolve_components(
        conn, project_id="proj_EXAMPLE", canonical_field_ids=[CAMPAIGN[0]]
    )


def test_a_read_that_failed_does_not_refuse_a_declaration():
    """"I could not look" is not "they disagree", and declaring is not executing.

    The relationship that pins this version reads the mappings again; blocking a
    declaration on a transient read would refuse a fact nobody measured.
    """

    class _Unreadable(_FakeConn):
        def cursor(self):
            cursor = _FakeCursor(self._rows, self._datastreams)
            execute = cursor.execute

            def _execute(sql, params=None):
                if "app.datastreams" in sql:
                    raise RuntimeError("connection reset")
                execute(sql, params)

            cursor.execute = _execute
            return cursor

    assert resolve_components(
        _Unreadable([CAMPAIGN]), project_id="proj_EXAMPLE",
        canonical_field_ids=[CAMPAIGN[0]],
    )
