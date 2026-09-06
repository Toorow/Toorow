"""Pure compiler tests for Story 47.2."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from core.datastream_preconfiguration import (
    _COMPILER_INJECTED,
    PreconfigurationValidationError,
    _draft_payload,
    _first_incomplete,
    _validate_inputs,
    compile_preconfiguration,
    read_proposal,
)


def _inputs() -> dict:
    return {
        "operator_input": {
            "mode": "connector_pull",
            "source": {"connector_ref": "generic", "source_account_ref": "sacct_safe"},
            "name": "Daily acquisition",
            # Asked at `Source` since 57.10, and half of what the fee/tax agreement
            # table needs. The other half is `source_category` below.
            "data_role": "Spend",
        },
        "connector_contract": {
            "object_id": "ccv_contract",
            "version_id": "ccv_v7",
            "fingerprint": "a" * 64,
            "observed_at": "2026-07-29T08:00:00Z",
            "source_category": "paid_media",
            "source_category_origin": "connector_manifest",
            "contract": {
                "contract_version": "7",
                "reports": [
                    {
                        "id": "daily",
                        "display_name": "Daily performance",
                        "availability": {"status": "selectable"},
                        "metrics": ["spend"],
                        "dimensions": ["date", "country"],
                        "supported_grains": [["date", "country"]],
                        "cadence": {
                            "minimum_interval_minutes": 1440,
                            "supported_modes": ["nightly"],
                        },
                        "quota_cost": {"read_points": 1, "unit": "request"},
                    }
                ],
                "fields": [
                    {
                        "field_id": "date",
                        "kind": "date",
                        "physical_type": "date",
                        "semantic_hints": ["primary_date"],
                        "canonical_target": "date",
                        "aggregation": "none",
                        "non_additive": False,
                    },
                    {
                        "field_id": "country",
                        "kind": "dimension",
                        "physical_type": "string",
                        "semantic_hints": ["country"],
                        "canonical_target": "country",
                        "aggregation": "none",
                        "non_additive": False,
                    },
                    {
                        "field_id": "spend",
                        "kind": "metric",
                        "physical_type": "number",
                        "semantic_hints": ["spend"],
                        "canonical_target": "spend",
                        "aggregation": "sum",
                        "non_additive": False,
                    },
                ],
            },
        },
        "observed_metadata": {
            "object_id": "obs_schema",
            "version_id": "obs_4",
            "fingerprint": "b" * 64,
            "observed_at": "2026-07-29T08:30:00Z",
            "safe_metadata": {
                "field_ids": ["date", "country", "spend"],
                "row_count_bucket": "100-999",
            },
        },
        "project_configuration": {
            "object_id": "project_configuration",
            "version_id": "pcv_3",
            "fingerprint": "c" * 64,
            "observed_at": "2026-07-29T08:45:00Z",
            "settings": {"reporting_currency": "EUR", "reporting_timezone": "Europe/Paris"},
        },
        # THE FIVE ROWS `seed_project_capabilities` writes for every Project
        # (migration 131), not the one this fixture used to carry. A section that
        # renders one row cannot prove that five sentences differ.
        "capabilities": [
            {
                "key": key,
                "state": state,
                "active_version_id": None,
                "pending_version_id": f"pcs_{key}_2",
                "fingerprint": "d" * 64,
            }
            for key, state in (
                ("country", "draft"),
                ("currency_fx", "draft"),
                ("reporting_timezone", "draft"),
                ("tax_fees", "disabled"),
                ("competitors", "disabled"),
            )
        ],
        "governance_presets": [],
    }


def _capability_items(result: dict) -> dict[str, dict]:
    section = next(item for item in result["sections"] if item["key"] == "capabilities")
    return {item["key"]: item for item in section["items"]}


def test_the_compiler_accepts_the_input_its_own_loader_builds() -> None:
    """The loader injects two keys into operator_input; the validator refused them.

    `_load_compiler_inputs` adds `project_id` and `revision_id` because
    `compile_preconfiguration` reads them back -- project_id to scope the
    proposal, revision_id as the version of the operator_input evidence ref.
    `_validate_operator_union` then rejected every key outside `_OPERATOR_COMMON`,
    so ANY draft that declared its mode died at compile with
    "Mode-irrelevant operator fields are not accepted: project_id, revision_id".

    It survived because the union check returns early when `mode` is absent, and
    because every compiler test above hands `compile_preconfiguration` a
    dictionary built by hand -- none of them goes through the loader, so the seam
    between the two was the one thing never exercised. Measured live on
    2026-08-07: 422 on the three modes, not just managed_feed.
    """
    inputs = _inputs()
    inputs["operator_input"] = {
        **inputs["operator_input"],
        **{key: f"{key}_value" for key in _COMPILER_INJECTED},
    }
    compile_preconfiguration(inputs)


def test_an_operator_may_not_send_the_keys_the_compiler_injects() -> None:
    """The tolerance is the compiler's alone: project_id chooses evidence scope.

    A client that could PATCH `project_id` into its draft would choose the scope
    its own proposal is compiled against, so the strict check stays the default
    and `update_draft` keeps refusing.
    """
    for key in _COMPILER_INJECTED:
        with pytest.raises(PreconfigurationValidationError) as excinfo:
            _validate_inputs(
                {"operator_input": {**_inputs()["operator_input"], key: "injected_by_client"}}
            )
        assert key in str(excinfo.value)


def test_compiler_is_deterministic_complete_and_explainable() -> None:
    first = compile_preconfiguration(_inputs())
    assert first == compile_preconfiguration(deepcopy(_inputs()))
    sections = {section["key"]: section for section in first["sections"]}
    assert set(sections) == {
        "source",
        "mode",
        "fields",
        "grain",
        "physical_mapping",
        "classification",
        "schedule",
        "history",
        "cost_quota",
        "capabilities",
        "processing",
        "outputs",
        "downstream_candidates",
    }
    history = sections["history"]["items"][0]["proposed_value"]
    assert history["start"] == "2026-06-29T00:00:00Z"
    assert history["end_exclusive"] == "2026-07-29T00:00:00Z"

    required = {
        "key",
        "section",
        "requirement",
        "status",
        "proposed_value",
        "evidence_refs",
        "confidence",
        "coverage",
        "exceptions",
        "blockers",
        "warnings",
        "owner_links",
        "downstream_impact",
        "dependency_fingerprint",
    }
    for section in first["sections"]:
        for item in section["items"]:
            assert required.issubset(item)
    evidence = [
        ref
        for section in first["sections"]
        for item in section["items"]
        for ref in item["evidence_refs"]
    ]
    assert {ref["kind"] for ref in evidence} >= {
        "connector_contract",
        "observed_metadata",
        "project_setting",
        "operator_input",
    }
    assert all(ref["version_id"] and len(ref["fingerprint"]) == 64 for ref in evidence)


def test_pending_capability_is_only_a_project_settings_proposal() -> None:
    result = compile_preconfiguration(_inputs())
    item = _capability_items(result)["capability.country"]
    assert item["status"] == "warning"
    assert item["proposed_value"]["label"] == "Proposed in Project Settings"
    assert item["proposed_value"]["name"] == "Country"
    assert result["configuration_summary"]["will_remain_a_proposal"]


def test_capability_owner_link_is_reachable() -> None:
    """Story 57.4 — the WHOLE address, never its last two segments.

    What stood here asserted `route.endswith("/settings/capabilities")`. The route was
    `/projects/{project_id}/settings/capabilities`, which `parsePath` refuses outright
    (`if (parts[0] !== "org") return unknown(...)`): the only gesture this screen
    offers opened nothing, and a suffix assertion could not see it. The compiler no
    longer composes an address at all -- it names the owning surface and the router
    builds `/org/{orgId}/project/{projectId}/settings/capabilities` from it.
    """
    result = compile_preconfiguration(_inputs())
    for key, item in _capability_items(result).items():
        link = item["owner_links"][0]
        assert "route" not in link, f"{key} still composes a server-side address"
        assert link["owner_reference"] == {
            "surface": "global",
            "workspace": None,
            "section": None,
            "global_surface": "project-settings",
            "global_section": "capabilities",
            "object_type": None,
            "object_id": None,
            "tab": None,
            "action": None,
            "version_id": None,
            "evidence_id": None,
        }
        assert link["label"] == "Propose"
    assert "/projects/" not in str(_capability_items(result))


def test_no_destination_in_this_payload_is_a_composed_address() -> None:
    """THE CLASS, not the one link 57.4 was written for.

    `grep -n '"/projects/{' server/core/datastream_preconfiguration.py` returned TEN
    composed paths, and `parsePath` refuses all ten on their first segment. Four were
    `repair_route` inside blockers, printed as text by `summarizeObject`; two were
    `owner_links` drawn as `<a href>`; three were `owner_route` nothing ever read; one
    was `resume_href`, the way back to a draft. This walks the whole payload rather
    than naming the places, so an eleventh cannot be added quietly.
    """
    result = compile_preconfiguration(_inputs())

    def strings(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield str(key)
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
        elif isinstance(value, str):
            yield value

    emitted = list(strings(result))
    assert not [text for text in emitted if text.startswith("/projects/")]
    # And the key names that carried them are gone with them.
    assert "repair_route" not in emitted
    assert "owner_route" not in emitted
    assert "route" not in emitted


def test_every_owner_reference_names_a_destination_the_shell_declares() -> None:
    """A reference is only better than a path if the shell can resolve it.

    The eleven keys are `ContentRouter.openOwner`'s own contract; the workspace and
    section pairs are checked against the ones `navigation.ts` declares. The front test
    (`DatastreamSetupCapabilities.test.tsx`) closes the loop by building the address
    with the real `buildPath` and re-parsing it.
    """
    inputs = _inputs()
    inputs["operator_input"]["domain_ids"] = ["bdm_growth"]
    result = compile_preconfiguration(inputs)
    keys = {
        "surface", "workspace", "section", "global_surface", "global_section",
        "object_type", "object_id", "tab", "action", "version_id", "evidence_id",
    }
    declared = {
        ("data", "sources"), ("data", "connectors"), ("data", "datastreams"),
        ("governance", "semantic-model"), ("analyze", "reports"),
    }

    def references(value):
        if isinstance(value, dict):
            if set(value) == keys:
                yield value
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)

    found = list(references(result))
    assert found, "the payload names no destination at all"
    for reference in found:
        if reference["surface"] == "global":
            assert (reference["global_surface"], reference["global_section"]) == (
                "project-settings",
                "capabilities",
            )
            assert reference["workspace"] is None and reference["section"] is None
        else:
            assert (reference["workspace"], reference["section"]) in declared
            assert reference["global_surface"] is None


def test_the_way_back_to_a_draft_states_no_address_it_cannot_honour() -> None:
    """`resume_href` was the gravest of the ten and appeared on no list.

    `/projects/{id}/data/datastreams/add?draft=…&section=…` is wrong three times over:
    the root is refused, `add` is not the declared action (`create` is), and `data ›
    datastreams` declares no query contract, so the draft id is dropped even from a
    well-formed address. The key stays for the story 47.2 wire contract; its value is
    the stated absence, and `resume_reference` names the screen.
    """
    payload = _draft_payload(
        ("dsd_1", "proj_1", "draft", "dsdr_1", 1, None, "source", [], {}),
    )
    assert payload["resume_href"] == ""
    assert payload["resume_reference"]["workspace"] == "data"
    assert payload["resume_reference"]["section"] == "datastreams"
    assert payload["resume_reference"]["action"] == "create"
    # The two facts that actually resume a draft are still on the payload.
    assert payload["draft_ref"] == "dsd_1"
    assert payload["first_incomplete_section"] == "source"


def test_each_capability_states_its_own_effect_on_this_datastream() -> None:
    """The defect this story exists to close: ONE constant for five capabilities.

    `impact=["May change compatible Datastream fields, grain, processing and Outputs."]`
    was emitted on every row. Five identical sentences are not an effect; they are a
    placeholder shaped like an answer.
    """
    items = _capability_items(compile_preconfiguration(_inputs()))
    assert set(items) == {
        "capability.country",
        "capability.currency_fx",
        "capability.reporting_timezone",
        "capability.tax_fees",
        "capability.competitors",
    }
    effects = [item["proposed_value"]["effect"] for item in items.values()]
    assert all(effect for effect in effects)
    assert len(set(effects)) == len(effects)
    assert not any("May change compatible Datastream fields" in effect for effect in effects)
    # Every row carries the evidence that supports its sentence.
    assert all(item["evidence_refs"] for item in items.values())


def test_country_effect_names_the_grain_it_would_add() -> None:
    inputs = _inputs()
    inputs["operator_input"]["source"]["report_ref"] = "daily"
    inputs["operator_input"]["configure"] = {"grain": ["date"]}
    effect = _capability_items(compile_preconfiguration(inputs))["capability.country"][
        "proposed_value"
    ]["effect"]
    assert effect.startswith("Adds country to the grain: date -> ")
    assert "country" in effect.split("->")[1]


def test_country_effect_says_nothing_is_added_when_the_report_has_no_country() -> None:
    inputs = _inputs()
    report = inputs["connector_contract"]["contract"]["reports"][0]
    report["dimensions"] = ["date"]
    report["supported_grains"] = [["date"]]
    inputs["connector_contract"]["contract"]["fields"] = [
        field
        for field in inputs["connector_contract"]["contract"]["fields"]
        if field["field_id"] != "country"
    ]
    inputs["observed_metadata"]["safe_metadata"]["field_ids"] = ["date", "spend"]
    item = _capability_items(compile_preconfiguration(inputs))["capability.country"]
    effect = item["proposed_value"]["effect"]
    assert effect == (
        "This source declares no country dimension, so Country would add nothing to "
        "this Datastream."
    )
    assert item["proposed_value"]["effect_coverage"] == "not_applicable"


def test_tax_fees_effect_uses_the_agreement_table_and_never_lists_phases() -> None:
    item = _capability_items(compile_preconfiguration(_inputs()))["capability.tax_fees"]
    effect = item["proposed_value"]["effect"]
    assert "PAID_MEDIA" in effect
    assert "cascade_phase" not in str(item).lower()
    # A pair the table does not enumerate is the TYPED unknown, with its own reason.
    unknown = _inputs()
    unknown["operator_input"]["data_role"] = "Context"
    other = _capability_items(compile_preconfiguration(unknown))["capability.tax_fees"]
    assert "UNKNOWN" in other["proposed_value"]["effect"]
    assert other["proposed_value"]["effect"] != effect


def test_reporting_timezone_is_unknown_when_the_connector_declares_no_time_context() -> None:
    """36 connectors of 39 land here, and the third over-promise on this module dies.

    It SIGNALS a difference; it never re-aligns one, and it can state no number
    before a publication has been observed.
    """
    item = _capability_items(compile_preconfiguration(_inputs()))["capability.reporting_timezone"]
    effect = item["proposed_value"]["effect"]
    assert effect.startswith("Effect unknown before the first run")
    assert "signalled, never corrected" in effect
    assert not any(character.isdigit() for character in effect)
    assert item["proposed_value"]["effect_coverage"] == "unavailable"

    declared = _inputs()
    declared["connector_contract"]["contract"]["time_context"] = {
        "locus": "property",
        "fallback": "gap",
    }
    spoken = _capability_items(compile_preconfiguration(declared))["capability.reporting_timezone"]
    assert "day boundary at the property level" in spoken["proposed_value"]["effect"]
    assert "signalled and never corrected" in spoken["proposed_value"]["effect"]


def test_competitors_effect_reads_the_selected_report_declaration() -> None:
    inputs = _inputs()
    absent = _capability_items(compile_preconfiguration(inputs))["capability.competitors"]
    assert absent["proposed_value"]["effect"].startswith("Not applicable:")

    inputs["operator_input"]["source"]["report_ref"] = "daily"
    inputs["connector_contract"]["contract"]["tracked_entity"] = {
        "support": "declared",
        "reports": [
            {"report_id": "daily", "direction": "observe", "entity_kinds": ["advertiser"]}
        ],
    }
    declared = _capability_items(compile_preconfiguration(inputs))["capability.competitors"]
    assert "advertiser" in declared["proposed_value"]["effect"]
    assert declared["proposed_value"]["effect_coverage"] == "covered"


def test_wizard_never_emits_an_activation_intent() -> None:
    """`Propose` is a link. Nothing in this proposal names an activation route."""
    result = compile_preconfiguration(_inputs())
    rendered = str(result)
    assert "change-sets" not in rendered
    for item in _capability_items(result).values():
        assert "enable" not in str(item).lower()
        assert "activate" not in str(item).lower()


def test_an_empty_capability_list_is_not_a_failure() -> None:
    inputs = _inputs()
    inputs["capabilities"] = []
    sections = compile_preconfiguration(inputs)["sections"]
    section = next(item for item in sections if item["key"] == "capabilities")
    assert section["status"] == "not_applicable"
    item = section["items"][0]
    assert item["key"] == "capabilities.none"
    assert item["proposed_value"]["label"] == "This Project has no capability row yet"
    assert "seed_project_capabilities" in item["proposed_value"]["effect"]


def test_missing_evidence_is_explicit_and_never_guessed() -> None:
    inputs = _inputs()
    inputs["connector_contract"] = None
    inputs["observed_metadata"] = None
    result = compile_preconfiguration(inputs)
    assert next(s for s in result["sections"] if s["key"] == "fields")["status"] == "missing"
    assert (
        next(s for s in result["sections"] if s["key"] == "physical_mapping")["status"] == "blocked"
    )
    assert "google_ads" not in str(result).lower() and "meta_ads" not in str(result).lower()


def test_dependency_change_selectively_invalidates_downstream_sections() -> None:
    first = compile_preconfiguration(_inputs())
    changed = _inputs()
    changed["project_configuration"]["version_id"] = "pcv_4"
    changed["project_configuration"]["fingerprint"] = "e" * 64
    second = compile_preconfiguration(changed, previous_proposal=first)
    statuses = {section["key"]: section["status"] for section in second["sections"]}
    assert statuses["source"] == "complete" and statuses["mode"] == "complete"
    for key in (
        "physical_mapping",
        "classification",
        "capabilities",
        "processing",
        "outputs",
        "downstream_candidates",
    ):
        assert statuses[key] == "needs_review"
    assert any(
        cause["dependency"] == "project_configuration" for cause in second["invalidation_causes"]
    )


def test_client_authored_observed_metadata_is_refused_by_name() -> None:
    """Story 47.3: the compiler resolves an exact observation_ref server-side.

    Split out of the sensitive-input test, where it was proven only by
    accident: that case passed a client-authored `observed_metadata` that was
    ALSO oversized, so a single `raises` could not say which guard fired, and
    the size guard was in fact never reached.
    """
    with pytest.raises(PreconfigurationValidationError, match="Client-authored observed_metadata"):
        compile_preconfiguration({"operator_input": {"observed_metadata": {"safe_metadata": {}}}})


def test_oversized_observed_metadata_is_rejected() -> None:
    inputs = _inputs()
    inputs["observed_metadata"]["safe_metadata"]["blob"] = "x" * 8192
    with pytest.raises(PreconfigurationValidationError):
        compile_preconfiguration(inputs)


def test_sensitive_or_authorizing_input_is_rejected() -> None:
    inputs = _inputs()
    inputs["observed_metadata"]["safe_metadata"]["access_token"] = "do-not-store"
    with pytest.raises(PreconfigurationValidationError):
        compile_preconfiguration(inputs)
    result = compile_preconfiguration(_inputs())
    forbidden = {
        "datastream_id",
        "plan_version_id",
        "mapping_version_id",
        "publication_id",
        "current_plan_version_id",
        "enabled",
        "credential",
        "provider_account_id",
    }

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert forbidden.isdisjoint(set(keys(result)))


def test_governance_preset_is_versioned_evidence() -> None:
    inputs = _inputs()
    inputs["governance_presets"] = [
        {
            "key": "regulated-data",
            "version_id": "gpv_2",
            "fingerprint": "f" * 64,
            "observed_at": "2026-07-29T08:40:00Z",
        }
    ]
    result = compile_preconfiguration(inputs)
    classification = next(
        section for section in result["sections"] if section["key"] == "classification"
    )
    refs = classification["items"][0]["evidence_refs"]
    assert any(ref["kind"] == "governance_preset" and ref["version_id"] == "gpv_2" for ref in refs)


class _StaleCursor:
    def __init__(self, proposal_row):
        self.proposal_row = proposal_row
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params):
        self.query = query

    def fetchone(self):
        if "FROM app.datastream_preconfiguration_proposals p" in self.query:
            return self.proposal_row
        if "FROM app.projects p" in self.query:
            return (
                "pcv_2",
                {"reporting_timezone": "UTC"},
                "f" * 64,
                datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc),
            )
        return None

    def fetchall(self):
        return []


class _StaleConnection:
    def __init__(self, proposal_row):
        self.proposal_row = proposal_row

    def cursor(self):
        return _StaleCursor(self.proposal_row)


def test_proposal_read_discloses_external_dependency_drift() -> None:
    initial = {
        "operator_input": {"project_id": "proj_1", "revision_id": "dsdr_1"},
        "connector_contract": None,
        "observed_metadata": None,
        "project_configuration": {
            "object_id": "project_configuration",
            "version_id": "pcv_1",
            "fingerprint": "e" * 64,
            "observed_at": "2026-07-29T09:00:00Z",
            "settings": {"reporting_timezone": "Europe/Paris"},
        },
        "capabilities": [],
        "governance_presets": [],
    }
    payload = compile_preconfiguration(initial)
    row = (
        "dsdr_1",
        payload["dependency_fingerprint"],
        payload,
        "dsdr_1",
        initial["operator_input"],
    )
    result = read_proposal(
        _StaleConnection(row),
        project_id="proj_1",
        draft_id="dsd_1",
        proposal_id="dspp_1",
    )
    assert result["is_stale"] is True
    assert any(
        cause["dependency"] == "project_configuration" for cause in result["invalidation_causes"]
    )


def test_external_observation_fields_form_the_same_mapping_universe_as_connector_fields() -> None:
    inputs = _inputs()
    inputs["operator_input"] = {
        "mode": "external_bq",
        "source": {
            "access_ref": "xaccess_1",
            "object_ref": "acme.analytics.daily",
            "declared_writer": "external",
            "readonly_acknowledged": True,
        },
        "configure": {
            "watermark_semantics": "event_date",
            "logical_dataset_name": "Daily acquisition",
        },
        "name": "Daily acquisition",
    }
    inputs["connector_contract"] = None
    inputs["observed_metadata"]["safe_metadata"] = {
        "fields": [
            {"field_id": "event_date", "type": "DATE", "nullable": False},
            {"field_id": "country", "type": "STRING", "nullable": False},
            {"field_id": "spend", "type": "NUMERIC", "nullable": True},
        ],
        "schema_hash": "f" * 64,
    }

    result = compile_preconfiguration(inputs)
    sections = {section["key"]: section for section in result["sections"]}
    fields = sections["fields"]["items"][0]["proposed_value"]
    mapping = sections["physical_mapping"]["items"][0]["proposed_value"]

    assert sections["fields"]["status"] == "complete"
    assert [field["field_id"] for field in fields] == ["country", "event_date", "spend"]
    assert mapping["joint_grain"] == ["country", "event_date"]
    assert {field["source_identity"] for field in mapping["fields"]} == {
        "country",
        "event_date",
        "spend",
    }
    assert all(
        {"role", "semantic_type", "aggregation", "sensitivity", "included"}
        <= set(field)
        for field in mapping["fields"]
    )


def test_compiler_emits_complete_review_intent_bundle_without_owner_side_effects() -> None:
    inputs = _inputs()
    inputs["operator_input"]["data_role"] = "performance"
    inputs["operator_input"]["domain_ids"] = ["bdm_growth", "bdm_acquisition"]
    inputs["operator_input"]["configure"] = {"grain": ["date", "country"]}

    bundle = compile_preconfiguration(inputs)["confirmed_intent_bundle"]

    assert bundle["datastream_name"] == "Daily acquisition"
    assert bundle["data_role"] == "performance"
    assert bundle["joint_grain"] == ["country", "date"]
    assert bundle["business_domain_ids"] == ["bdm_acquisition", "bdm_growth"]
    assert bundle["field_mappings"]
    assert bundle["dq_gates"]
    assert bundle["processing"]
    assert bundle["outputs"]
    assert bundle["exceptions"] == []
    assert all(
        item["state"] in {"existing", "will_be_created", "proposal", "blocked"}
        for item in bundle["owner_proposals"]
    )


def _complete_operator_input(wizard_state: dict) -> dict:
    """An input whose source and configure sections are both settled."""
    return {
        "mode": "connector_pull",
        "name": "Daily acquisition",
        "data_role": "Spend",
        "source": {"observation_ref": "dso_1"},
        "configure": {"date_field": "date", "metrics": "spend", "dimensions": "date"},
        "wizard_state": wizard_state,
    }


def test_resume_section_never_names_a_step_the_wizard_no_longer_has() -> None:
    """Story 57.9 — the server repeats the client's section name, so the name the
    client stops writing is a name the server stops producing.

    `_first_incomplete` computes `source` and `configure` itself and echoes
    `wizard_state.first_incomplete` for everything after them. A legacy row can
    therefore still carry the removed step's name: the function must hand it back
    without raising, because `_resume_href` builds a query parameter from it and
    nothing in the front resolves that parameter.
    """
    for wizard_state in (
        {},
        {"first_incomplete": "classify_and_map"},
        {"first_incomplete": "preview_validate"},
    ):
        assert _first_incomplete(_complete_operator_input(wizard_state)) != "destination"

    # A row written before 57.9. It is repeated, not invented, and nothing falls.
    legacy = _first_incomplete(_complete_operator_input({"first_incomplete": "destination"}))
    assert legacy == "destination"

    # And the two branches the server owns are unchanged: a missing data role is
    # a `source` answer (57.10), never a later section.
    incomplete_role = _complete_operator_input({"first_incomplete": "schedule_activate"})
    del incomplete_role["data_role"]
    assert _first_incomplete(incomplete_role) == "source"


def _managed_feed_inputs(*, channel: str = "file_upload", profile=None) -> dict:
    """A managed feed carries NO connector contract -- that is what it means."""
    inputs = _inputs()
    inputs["connector_contract"] = None
    # No schema observed: an inbound channel has no file yet, and a staged upload
    # has not been staged in this fixture either.
    inputs["observed_metadata"] = None
    inputs["operator_input"] = {
        "mode": "managed_feed",
        "name": "Deliveries",
        "data_role": "Spend",
        "source": {"channel": channel},
        "configure": {"write_mode": "replace"},
    }
    if profile is not None:
        inputs["observed_metadata"] = profile
    return inputs


def _items(result: dict) -> dict[str, dict]:
    return {
        item["key"]: item for section in result["sections"] for item in section.get("items", [])
    }


def test_a_managed_feed_does_not_block_on_a_connector_contract_it_can_never_have() -> None:
    """Three sections read a connector report. A managed feed has no report, ever.

    `schedule.policy` and `cost_quota.estimate` come from `report.cadence` and
    `report.quota_cost`; `history.window` reads `configure.history_intent` /
    `date_window`, two keys `_OPERATOR_CONFIGURE_KEYS['managed_feed']` does not
    even accept. So for a managed feed all three are permanently `missing` -- and
    `freeze_final_review` counts `missing` as blocking.

    The consequence, measured live 2026-08-07: NO managed-feed Datastream could
    be materialised by the assistant. Not the inbound ones -- ANY of them, a
    plain file upload included, on any project.

    `not_applicable` already exists in `ItemStatus` and already means this. An
    absence that can never be filled is not a work item; presenting it as one
    sends the operator looking for a field that does not exist.
    """
    items = _items(compile_preconfiguration(_managed_feed_inputs()))

    for key in ("schedule.policy", "cost_quota.estimate", "history.window"):
        assert items[key]["status"] == "not_applicable", key
        assert not items[key]["blockers"], key


def test_an_inbound_channel_defers_what_it_cannot_observe_until_a_file_arrives() -> None:
    """An inbound channel has no file until a delivery arrives -- by construction.

    An address can only be issued against a MATERIALIZED Datastream
    (`datastream-workbench-and-wizard.md`, Incomplete if), and
    `observe_first_delivery` needs a Datastream that has already received. So the
    schema-dependent sections cannot be satisfied before materialisation, and
    blocking on them makes the channel unreachable -- the same list already names
    that defect: "an inbound channel is offered whose next step can observe
    nothing".

    They become WARNINGS, not silent skips: `freeze_final_review` requires every
    warning to be acknowledged explicitly, so creating a Datastream whose first
    delivery will be RETAINED for review rather than imported stays a conscious
    act. `InboundContractReviewRequired` is what the arrival then raises.
    """
    items = _items(compile_preconfiguration(_managed_feed_inputs(channel="inbound_email")))

    for key in (
        "fields.selection",
        "mapping.proposal",
        "processing.proposal",
        "outputs.full_grain",
        "grain.joint",
        "classification.fields",
    ):
        assert items[key]["status"] == "warning", key
        assert not items[key]["blockers"], key
        if key in ("fields.selection", "mapping.proposal"):
            assert [w["cause"] for w in items[key]["warnings"]] == [
                "no_delivery_received_yet"
            ], key


def test_an_uploaded_feed_still_demands_the_schema_it_can_actually_carry() -> None:
    """The deferral belongs to the CHANNEL, not to the mode.

    A `file_upload` feed stages its file during setup, so its schema is available
    before materialisation and an unmapped one is still a blocker. Deferring it
    too would let any managed feed through unmapped.
    """
    items = _items(compile_preconfiguration(_managed_feed_inputs(channel="file_upload")))

    assert items["mapping.proposal"]["status"] == "blocked"
    assert [b["cause"] for b in items["mapping.proposal"]["blockers"]] == [
        "mapping_evidence_unavailable"
    ]


def test_no_write_demotes_a_materialized_draft() -> None:
    """Completer un flux entrant repasse par TOUTES les portes du brouillon.

    L'invariant `state='materialized' <=> materialized_datastream_id IS NOT NULL`
    vit dans Postgres ; toute instruction qui force `state='draft'` sans condition
    le viole des que le brouillon a materialise quelque chose. C'est arrive deux
    fois -- a l'observation, puis a la revision -- et chaque fois le symptome
    etait un 503 qui ne nommait rien.

    La garde lit les INSTRUCTIONS, parce que la contrainte qui les punit n'est pas
    dans cette suite : une doublure qui n'applique aucune contrainte accepte les
    deux versions sans broncher.
    """
    from pathlib import Path

    core = Path(__file__).resolve().parents[2] / "core"
    for name in ("datastream_preconfiguration.py", "datastream_setup_observations.py"):
        text = (core / name).read_text(encoding="utf-8")
        for index, line in enumerate(text.splitlines()):
            if "UPDATE app.datastream_setup_drafts" not in line:
                continue
            statement = "\n".join(text.splitlines()[index : index + 6])
            if "state=" not in statement and "state '" not in statement:
                continue
            assert "state='draft'," not in statement, (
                f"{name}:{index + 1} remet le brouillon en 'draft' sans condition"
            )


def test_no_revision_write_can_revive_a_discarded_draft() -> None:
    """AI-336, 2026-08-31 : `archived` est TERMINAL, et le rester est structurel.

    Les deux ecrivains de la revision -- l'autosave et la decouverte -- portaient
    `state=CASE WHEN materialized_datastream_id IS NULL THEN 'draft' ELSE state
    END`. Cette clause avait ete ecrite pour proteger un brouillon MATERIALISE,
    et elle le faisait ; mais un brouillon ne quitte 'draft' que de deux facons,
    et la seconde -- l'abandon -- n'existait pas encore quand elle a ete ecrite.
    Le jour ou elle a existe, la meme clause est devenue la resurrection : la
    premiere frappe apres un abandon aurait ramene le brouillon a la vie.

    Deux gardes, parce qu'une seule ne tient pas : le refus explicite peut etre
    contourne par un futur appelant, et l'instruction muette sur `state` peut
    voir la clause revenir. La suite lit les DEUX, sur les DEUX fichiers -- la
    contrainte qui les punirait n'est pas dans cette suite, et une doublure sans
    contrainte accepte n'importe laquelle des deux versions.
    """
    from pathlib import Path

    core = Path(__file__).resolve().parents[2] / "core"
    for name in ("datastream_preconfiguration.py", "datastream_setup_observations.py"):
        text = (core / name).read_text(encoding="utf-8")
        lines = text.splitlines()
        pointer_writes = 0
        for index, line in enumerate(lines):
            if "UPDATE app.datastream_setup_drafts" not in line:
                continue
            statement = "\n".join(lines[index : index + 6])
            if "current_revision_id=" not in statement:
                continue
            pointer_writes += 1
            assert "state=" not in statement, (
                f"{name}:{index + 1} touche `state` en deplacant le pointeur de "
                f"revision : c'est par la qu'un brouillon abandonne revient"
            )
        assert pointer_writes >= 1, (
            f"{name} n'ecrit plus le pointeur de revision -- la garde ci-dessus "
            f"ne mesure plus rien"
        )
        assert "DRAFT_STATE_DISCARDED" in text, (
            f"{name} n'interroge plus l'etat terminal avant d'ecrire : terminal "
            f"doit valoir sur TOUTES les ecritures, pas seulement sur la reprise"
        )


def test_the_discarded_refusal_names_a_gesture_and_never_a_state_word() -> None:
    """Un refus qui dit << archived >> laisse la personne sans pas suivant.

    C'est la regle d'ecran du depot -- un message nomme le geste qui repare, pas
    la cause technique -- et elle vaut pour la phrase que TROIS portes rendent
    (l'autosave, la decouverte, la publication), parce qu'elles rendent la meme.
    """
    from core.datastream_preconfiguration import DISCARDED_DRAFT_REFUSAL

    assert "archived" not in DISCARDED_DRAFT_REFUSAL.lower()
    assert "state" not in DISCARDED_DRAFT_REFUSAL.lower()
    assert "Start a new Datastream setup" in DISCARDED_DRAFT_REFUSAL
