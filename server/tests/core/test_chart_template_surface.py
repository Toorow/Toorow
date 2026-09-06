"""Story 72.5 -- what the surface serves, proved offline.

WHAT IS PROVED HERE, and none of it needs a database:

  * AC15 -- a connector DECLARES Chart Templates as data, never as code: the
    declaration is read from JSON, every document meets the one validator, a
    refused one is NAMED rather than repaired, and no module is imported;
  * the seed identity is DERIVED, which is what makes the gesture idempotent;
  * a list row states the question, the family, the provenance and what it asks
    for, IN THE SERVER'S WORDS -- never a stored token;
  * a row whose document does not validate under the shipped contract is
    `readable: false` with its reason, and is NOT dropped and NOT shown as
    incompatible;
  * the requirement sentence reuses the verdict's own vocabulary, so a template
    describes itself in the words it is later refused in;
  * the route family declares literal segments before identifiers, and every
    write is a `member` role while every read is `viewer`.

WHAT IS PROVED ELSEWHERE. That the reads and writes hit the real schema, and
that a connector seed becomes project-owned at its first edit, is the pg-gated
suite of the same story.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core import chart_template_store as store
from core import chart_templates_api as api
from core import connector_chart_template_seeds as seeds
from core.template_compatibility import requirements_sentence
from core.visualization_templates import CHART_TEMPLATE_CONTRACT_VERSION


def _document(**overrides) -> dict:
    document = {
        "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
        "schema_version": 1,
        "family": "bar",
        "answers_question": "How does one measure compare across a few categories?",
        "requires": {
            "measure": {"min": 1, "max": 1},
            "dimension": {"min": 1, "max": 1, "max_cardinality": 12},
        },
    }
    document.update(overrides)
    return document


def _head(**overrides) -> dict:
    head = {
        "id": "vtpl_EXAMPLE",
        "label": "Spend by channel",
        "description": None,
        "seed_origin": "platform_seed",
        "seed_module_name": None,
        "seed_template_id": None,
        "current_version_id": "vtv_EXAMPLE",
        "archived_at": None,
        "created_by": "owner@example.com",
        "created_at": "2026-09-01T09:00:00+00:00",
        "updated_at": "2026-09-01T09:00:00+00:00",
    }
    head.update(overrides)
    return head


# ---------------------------------------------------------------------------
# The row a person reads.
# ---------------------------------------------------------------------------


def test_a_row_states_the_question_the_family_and_what_it_asks_for():
    row = store.template_row_payload(
        head=_head(), document=_document(), current_version_id="vtv_EXAMPLE", version_count=1
    )
    assert row["answers_question"] == "How does one measure compare across a few categories?"
    assert row["family_label"] == "Bar"
    assert row["readable"] is True
    assert "measure" in row["requires_sentence"]
    assert "dimension" in row["requires_sentence"]
    #  The bound the template declares travels into the sentence: a person must
    #  be able to see WHY a Result with 400 channels will not satisfy it.
    assert "12" in row["requires_sentence"]


def test_the_provenance_is_a_label_and_never_the_stored_token():
    platform = store.template_row_payload(
        head=_head(), document=_document(), current_version_id="vtv_EXAMPLE", version_count=1
    )
    assert platform["origin_label"] == "Shipped with toorow"
    assert "platform_seed" not in platform["origin_label"]

    connector = store.template_row_payload(
        head=_head(
            seed_origin="connector_seed",
            seed_module_name="google-ads",
            seed_template_id="spend_by_campaign",
        ),
        document=_document(),
        current_version_id="vtv_EXAMPLE",
        version_count=1,
    )
    #  "Seeded by" with nothing after it is not a provenance. The module name
    #  completes it, and it is the name of the connector rather than its row.
    assert connector["origin_label"] == "Seeded by google-ads"
    assert connector["seed"] == {"module_name": "google-ads", "template_id": "spend_by_campaign"}


def test_a_document_the_contract_cannot_read_is_named_rather_than_dropped():
    #  A Visualization Spec document is a legal object and an illegal template:
    #  it binds members. It must not vanish from the list, and it must not be
    #  reported as "does not fit a Result" -- nobody has judged it against one.
    row = store.template_row_payload(
        head=_head(),
        document={"spec_contract_version": "visualization-spec.v1", "bindings": {"measure": ["m"]}},
        current_version_id="vtv_EXAMPLE",
        version_count=1,
    )
    assert row["readable"] is False
    assert row["unreadable_reason"]["code"] == "template_unreadable"
    assert row["unreadable_reason"]["remedy"]
    assert row["requires_sentence"] is None
    #  It is still a row: the head, the provenance and the version count are all
    #  facts that do not depend on the document being readable.
    assert row["id"] == "vtpl_EXAMPLE"
    assert row["version_count"] == 1


def test_a_head_with_no_version_is_unreadable_as_a_presentation():
    row = store.template_row_payload(
        head=_head(current_version_id=None),
        document=None,
        current_version_id=None,
        version_count=0,
    )
    assert row["readable"] is False
    assert row["family"] is None


def test_the_requirement_sentence_is_the_verdict_s_own_vocabulary():
    #  The template says "one measure in Measure"; the verdict, when it refuses,
    #  says "this template needs one measure in Measure". Two paraphrases of one
    #  predicate is how a person stops recognising the thing they chose.
    sentence = requirements_sentence({"measure": {"min": 1, "accepts": ["measure"]}})
    assert sentence == "one measure in Measure"
    assert requirements_sentence({}) .startswith("nothing in particular")


# ---------------------------------------------------------------------------
# AI-347 -- what the Presentation tab EDITS against.
# ---------------------------------------------------------------------------


def test_the_vocabulary_carries_each_family_with_its_own_wells():
    from core.visualization_families import get_family  # noqa: PLC0415
    from core.visualization_templates import template_vocabulary  # noqa: PLC0415

    vocabulary = template_vocabulary()
    catalogue = {entry["id"]: entry for entry in vocabulary["family_catalogue"]}

    #  The same families, and no other: a screen must not offer a family whose
    #  wells no Result can fill.
    assert sorted(catalogue) == sorted(vocabulary["families"])

    bar = catalogue["bar"]
    #  A NAME, never the stored id. An editing control labelled `bar` would be
    #  the product identifier the ratified criterion refuses.
    assert bar["label"] == "Bar"
    assert bar["description"]

    #  Every bound the validator will judge against travels with the well, so the
    #  screen can offer only what `_check_requires_against_family` accepts.
    wells = {well["name"]: well for well in bar["wells"]}
    declared = get_family("bar")
    assert set(wells) == {well.name for well in declared.wells}
    for well in declared.wells:
        served = wells[well.name]
        assert served["label"] and served["label"] != well.name
        assert served["required"] is well.required
        assert served["max_members"] == well.max_members
        assert served["max_cardinality"] == well.max_cardinality
        assert sorted(served["accepts"]) == sorted(well.accepts)
        #  A well that cannot be required says so AND says who owns making it
        #  available -- never a story number, never a deployment state.
        assert served["available"] is well.available
        if not well.available:
            assert served["unavailable_reason"]
            assert served["unavailable_owner"]


def test_the_family_catalogue_is_read_off_the_registry_and_not_written_here():
    import inspect  # noqa: PLC0415

    from core import visualization_templates as module  # noqa: PLC0415

    source = inspect.getsource(module.template_vocabulary)
    #  `get_family(...)` and `well.as_dict()` -- the registry's own answer. A
    #  literal label or a literal bound in this function would be the second
    #  authority the ratified criterion refuses.
    assert "get_family(" in source
    assert "as_dict()" in source
    assert "Bar" not in source


def test_a_document_the_editing_screen_can_compose_is_accepted():
    from core.visualization_templates import validate_template_document  # noqa: PLC0415

    #  The screen offers `min` and `max` within `max_members`, and a cardinality
    #  bound only where the well carries one. That document validates.
    validated = validate_template_document(
        _document(
            requires={
                "measure": {"min": 1, "max": 3},
                "dimension": {"min": 1, "max": 1, "max_cardinality": 50},
            }
        )
    )
    assert validated.family == "bar"
    assert validated.content_hash


# ---------------------------------------------------------------------------
# AC15 -- the connector seed gesture.
# ---------------------------------------------------------------------------


def test_a_connector_declares_templates_as_data_and_a_refused_one_is_named(tmp_path: Path):
    module = tmp_path / "example-connector" / seeds.SEED_DIRNAME
    module.mkdir(parents=True)
    (module / "spend_by_channel.json").write_text(
        json.dumps({"label": "Spend by channel", **_document()}), encoding="utf-8"
    )
    #  A document that binds a member is a Visualization, not a starting point.
    (module / "already_bound.json").write_text(
        json.dumps({**_document(), "bindings": {"measure": ["mdm_EXAMPLE"]}}), encoding="utf-8"
    )
    (module / "not_even_json.json").write_text("{", encoding="utf-8")

    declared, unusable = seeds.declared_chart_templates("example-connector", root=tmp_path)

    assert [entry.seed_template_id for entry in declared] == ["spend_by_channel"]
    assert declared[0].label == "Spend by channel"
    assert declared[0].family == "bar"
    #  Named, both of them, with a reason a person can act on. A silently
    #  dropped declaration is a connector shipping something nobody can see.
    assert {entry.seed_template_id for entry in unusable} == {"already_bound", "not_even_json"}
    assert all(entry.reason for entry in unusable)


def test_a_module_that_declares_nothing_is_not_an_error(tmp_path: Path):
    (tmp_path / "quiet-connector").mkdir()
    assert seeds.declared_chart_templates("quiet-connector", root=tmp_path) == ([], [])


def test_no_connector_ships_a_chart_template_today_which_is_why_the_empty_state_is_honest():
    #  The console says "No connector in this Project brings a Chart Template".
    #  That sentence is a MEASUREMENT, and this is the measurement.
    shipped = sorted(
        path.parent.name
        for path in seeds.MODULES_ROOT.glob(f"*/{seeds.SEED_DIRNAME}")
        if path.is_dir()
    )
    assert shipped == []


def test_a_name_that_is_not_a_module_name_reads_no_file(tmp_path: Path):
    declared, unusable = seeds.declared_chart_templates("../../etc", root=tmp_path)
    assert declared == []
    assert unusable[0].reason == "this is not a connector module name"


def test_the_seed_identity_is_derived_so_the_gesture_is_idempotent():
    first = seeds.seed_head_id("google-ads", "spend", "proj_EXAMPLE")
    assert first == seeds.seed_head_id("google-ads", "spend", "proj_EXAMPLE")
    #  Two Projects never share a head, and two connectors never share one either.
    assert first != seeds.seed_head_id("google-ads", "spend", "proj_OTHER")
    assert first != seeds.seed_head_id("meta-ads", "spend", "proj_EXAMPLE")
    assert seeds.seed_version_id("google-ads", "spend", "proj_EXAMPLE").endswith("__v1")


def test_the_seed_module_imports_no_connector_module():
    #  "No executable byte comes from the manifest" is a property of the SOURCE,
    #  not of a run: an import added later would execute connector code on a
    #  read. Asserted structurally, like the console's own drawing guard.
    source = Path(seeds.__file__).read_text(encoding="utf-8")
    assert "import_module" not in source
    assert "importlib" not in source
    assert "exec(" not in source
    assert "eval(" not in source


# ---------------------------------------------------------------------------
# The route family.
# ---------------------------------------------------------------------------


def _paths() -> list[str]:
    return [route.path for route in api.ROUTES]


def test_a_literal_segment_is_declared_before_the_identifier_that_could_swallow_it():
    paths = _paths()
    seeds_index = paths.index("/api/projects/{project_id}/analyze/chart-templates/seeds")
    generic_index = paths.index("/api/projects/{project_id}/analyze/chart-templates/{template_id}")
    #  Starlette matches in declaration order: `seeds` read as a template id
    #  would answer 404 for a gesture the product offers.
    assert seeds_index < generic_index


@pytest.mark.parametrize(
    "handler,role",
    [
        (api._vocabulary, "viewer"),
        (api._list_chart_templates, "viewer"),
        (api._get_chart_template, "viewer"),
        (api._compatibility, "viewer"),
        (api._create_chart_template, "member"),
        (api._add_chart_template_version, "member"),
        (api._seed_chart_templates, "member"),
        (api._apply, "member"),
        #  Both archive gestures share ONE guarded helper, so the guard is asserted
        #  where it lives. `_archive_chart_template` and `_restore_chart_template`
        #  are proven to reach it by `test_both_gestures_reach_the_one_guarded_helper`.
        (api._set_archived, "member"),
    ],
)
def test_every_read_is_a_viewer_and_every_write_is_a_member(handler, role):
    import inspect  # noqa: PLC0415

    source = inspect.getsource(handler)
    assert f'_authorize(request, "{role}")' in source


def test_the_verdict_route_writes_nothing():
    import inspect  # noqa: PLC0415

    source = inspect.getsource(api._compatibility)
    for verb in ("INSERT", "UPDATE", "DELETE", "create_", "append_"):
        assert verb not in source


def test_applying_goes_through_the_one_materialisation_function_and_no_other_writer():
    import inspect  # noqa: PLC0415

    #  The BODY, without the docstring: the docstring names the writer on purpose
    #  ("through `create_visualization_spec_version` and nothing else"), and a
    #  guard that read it would forbid saying what the code does.
    source = inspect.getsource(api._apply)
    body = source.split('"""', 2)[-1]
    assert "materialize_template(" in body
    #  Not `create_visualization_spec_version`, not an INSERT: one writer, so a
    #  Spec born from a template cannot skip the validator a hand-built one meets.
    assert "create_visualization_spec_version" not in body
    assert "INSERT" not in body


# ---------------------------------------------------------------------------
# AI-351 -- the archive and restore routes.
# ---------------------------------------------------------------------------


def test_archive_and_restore_are_declared_after_the_literal_seeds_segment():
    paths = _paths()
    seeds_index = paths.index("/api/projects/{project_id}/analyze/chart-templates/seeds")
    for tail in ("archive", "restore"):
        path = "/api/projects/{project_id}/analyze/chart-templates/{template_id}/" + tail
        assert path in paths, f"{tail} has no route"
        #  Same rule as `/versions`: the literal one-segment route is declared
        #  first, so `seeds` is never read as a template id on the way past.
        assert seeds_index < paths.index(path)


def test_neither_gesture_is_a_read_and_both_go_through_the_store():
    import inspect  # noqa: PLC0415

    body = inspect.getsource(api._set_archived).split('"""', 2)[-1]
    #  The state is written by `chart_template_store` and by nothing else. An
    #  UPDATE composed here would be a second writer of `archived_at`, and the
    #  next rule about archiving would land on only one of them.
    assert "archive_chart_template" in body
    assert "restore_chart_template" in body
    for verb in ("INSERT", "UPDATE", "DELETE", "SELECT"):
        assert verb not in body


def test_both_gestures_reach_the_one_guarded_helper():
    import inspect  # noqa: PLC0415

    for handler in (api._archive_chart_template, api._restore_chart_template):
        assert "_set_archived(request" in inspect.getsource(handler)


def test_the_archive_routes_carry_no_idempotency_key():
    import inspect  # noqa: PLC0415

    #  Not an omission, and the module says why: this is a state a caller ASKS
    #  FOR, so asking twice is answered with the state rather than an error. Only
    #  a route that EXECUTES a question needs a key.
    source = inspect.getsource(api._set_archived)
    assert "idempotency" not in source.lower().replace("no `idempotency-key`", "")


def test_a_route_exists_for_every_gesture_the_service_names_as_a_remedy():
    """The measured defect of AI-347, stated as a test rather than as a comment.

    The service refuses a version on an archived head with the remedy "List
    archived templates and restore this one first" -- and until AI-351 neither
    gesture had a route. A remedy naming something the API cannot do is a sentence
    that sends a person nowhere.

    THE REMEDY IS READ WHERE IT LIVES, WHICH IS ONE PLACE (AI-353):
    `refuse_version_on_archived_head`, called by `append_chart_template_version`
    and by the projection of the shipped catalogue. Both halves are asserted, so
    moving the sentence out of the refusal or the refusal out of the append path
    is still a failure.
    """
    import inspect  # noqa: PLC0415

    from core import chart_template_store as store_module  # noqa: PLC0415

    remedy = inspect.getsource(store_module.refuse_version_on_archived_head)
    assert "restore this one first" in remedy.lower()
    assert "refuse_version_on_archived_head" in inspect.getsource(
        store_module.append_chart_template_version
    )
    paths = " ".join(_paths())
    assert "/restore" in paths
    #  "List archived templates" is the same route with a flag, and it has been
    #  served since 72.5 -- asserted here so the pair of gestures the sentence
    #  names is proven together.
    assert "include_archived" in inspect.getsource(api._list_chart_templates)
