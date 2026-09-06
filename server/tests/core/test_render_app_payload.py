"""Story 65.1: one closed server-owned payload reaches the shared runtime."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest
from core import render_app_payload
from core.analyze_feedback import mint_delivery_feedback_context


def _render_input() -> dict:
    return render_app_payload.compose_render_input(
        result={
            "result_id": "qr_EXAMPLE",
            "content_hash": "a" * 64,
            "outcome": "success",
            "schema": {"fields": [{"name": "day"}, {"name": "clicks"}]},
            "rows": [{"day": "2026-08-10", "clicks": 7}],
            "manifest": {"grain": "day", "freshness": {"state": "fresh"}},
            "truncated": False,
            "row_count": 1,
        },
        spec={
            "visualization_spec_version_id": "vsv_EXAMPLE",
            "spec_contract_version": "visualization-spec.v1",
            "schema_version": 1,
            "document": {
                "spec_contract_version": "visualization-spec.v1",
                "schema_version": 1,
                "family": "bar",
            },
        },
        pins={
            "theme_version": "viz-theme@1",
            "formatter_version": "viz-formatters@1",
            "renderer_build": "bar/toorow-echarts-bar@1.0.0",
            "runtime_build": "@toorow/card-shell/viz@0.1.0+abcdef0",
        },
        profile="mcp-inline",
        display={},
    )


def test_compose_render_input_has_the_closed_runtime_shape():
    value = _render_input()
    assert set(value) == {"result", "spec", "pins", "profile", "display"}
    assert value["profile"] == "mcp-inline"


def test_v1_payload_is_schema_valid_and_has_no_capability_bag():
    payload = render_app_payload.compose_render_app_payload(render_input=_render_input())
    assert set(payload) == {"schema_version", "kind", "render_input"}
    assert payload["schema_version"] == 1
    assert payload["kind"] == "render"
    render_app_payload.validate_render_app_payload(payload)

    schema_path = (
        Path(render_app_payload.__file__).parent / "schemas/render-app-payload.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_typed_payload_nests_middleware_moves_without_opening_the_contract():
    payload = render_app_payload.compose_render_app_payload(
        render_input=_render_input(), moved={"rows": [{"day": "2026-08-10"}]}
    )
    assert payload["moved"] == {"rows": [{"day": "2026-08-10"}]}
    assert "rows" not in set(payload) - {"render_input", "moved"}


@pytest.mark.parametrize("field", ["sql", "relation", "access_token", "credentials", "option"])
def test_sensitive_or_executable_keys_are_refused_recursively(field):
    value = _render_input()
    value["result"]["manifest"][field] = "must-not-cross"
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(render_input=value)
    assert excinfo.value.code == "unsafe_render_payload"


def test_result_manifest_projection_keeps_evidence_ids_but_drops_physical_fields():
    projected = render_app_payload.project_result_manifest(
        {
            "time": {"start": "2026-08-01", "end": "2026-08-10"},
            "provenance": {
                "mapping_version_id": "map_1",
                "publication_log_id": "pub_1",
                "relation": "warehouse.private.table",
                "values": [
                    {
                        "member_id": "sessions",
                        "pull_id": "pull_1",
                        "source_field": "private_session_count",
                    }
                ],
            },
            "dq_evaluation_ids": ["dq_1"],
        }
    )
    assert projected["time_window"] == {"start": "2026-08-01", "end": "2026-08-10"}
    assert projected["provenance"] == {
        "mapping_version_id": "map_1",
        "publication_log_id": "pub_1",
        "values": [{"member_id": "sessions", "pull_id": "pull_1"}],
    }
    assert projected["dq_evaluation_ids"] == ["dq_1"]


def test_business_row_and_schema_keys_are_not_mistaken_for_control_material():
    value = _render_input()
    value["result"]["schema"] = {
        "fields": [{"name": "series"}, {"name": "option"}, {"name": "table_name"}]
    }
    value["result"]["rows"] = [
        {"series": "organic", "option": "A", "table_name": "customer-facing label"}
    ]
    render_app_payload.compose_render_app_payload(render_input=value)


def test_existing_meta_ai_path_control_material_is_refused_but_result_rows_are_data():
    existing = {
        "toorow.result": {
            "schema": {"fields": [{"name": "token"}]},
            "rows": [{"token": "customer segment label"}],
            "ai_path_walk": {"steps": [{"detail": {"token": "must-not-cross"}}]},
        }
    }
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(
            render_input=_render_input(), existing_meta=existing
        )
    assert excinfo.value.code == "unsafe_render_payload"
    assert "ai_path_walk" in excinfo.value.details["path"]


@pytest.mark.parametrize(
    "existing",
    [
        {"toorow.result": {"schema": {"access_token": "secret"}}},
        {
            "toorow.result": {
                "rows": [{"option": {"series": [1]}, "renderer_options": {"xaxis": {}}}]
            }
        },
    ],
)
def test_existing_meta_refuses_control_material_nested_below_business_boundaries(existing):
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(
            render_input=_render_input(), existing_meta=existing
        )
    assert excinfo.value.code == "unsafe_render_payload"


def test_a_normalized_visualization_spec_crosses_with_every_well_it_declares():
    """AI-337: the grammar's own wells are not control material.

    `visualization_specs.normalize_document` emits ALL of `WELL_ROLES` on every
    document, filled or empty -- and `series` is also the name a raw ECharts
    option would wear, so the scan refused every real Spec and the render tool
    could not draw one. Measured 2026-08-31 on the first captured Result; unseen
    until then because the only document that had ever reached the tool was a
    literal with no `series` key. The wells are read from the grammar, so a well
    added there is covered here without a second edit.
    """
    from core.visualization_families import WELL_ROLES

    value = _render_input()
    value["spec"]["document"]["bindings"] = {well: [] for well in WELL_ROLES}
    value["spec"]["document"]["bindings"]["measure"] = ["sc_EXAMPLE_MEASURE"]
    value["spec"]["document"]["bindings"]["series"] = ["sc_EXAMPLE_SERIES"]
    payload = render_app_payload.compose_render_app_payload(render_input=value)
    assert payload["render_input"]["spec"]["document"]["bindings"]["series"] == [
        "sc_EXAMPLE_SERIES"
    ]


def test_an_option_object_hidden_under_a_well_is_still_refused():
    """The exemption is the well NAME, never its value."""
    value = _render_input()
    value["spec"]["document"]["bindings"] = {"series": [{"renderer_options": {"xaxis": {}}}]}
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(render_input=value)
    assert excinfo.value.code == "unsafe_render_payload"
    assert "renderer_options" in excinfo.value.details["path"]


def test_a_control_key_that_is_not_a_well_is_refused_inside_the_bindings_node():
    value = _render_input()
    value["spec"]["document"]["bindings"] = {"measure": [], "renderer_options": {}}
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(render_input=value)
    assert excinfo.value.code == "unsafe_render_payload"


def test_a_well_name_outside_the_bindings_node_is_still_control_material():
    """The exemption is scoped to one path, not to the word `series` everywhere."""
    value = _render_input()
    value["spec"]["document"]["series"] = {"type": "bar"}
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(render_input=value)
    assert excinfo.value.details["path"] == "$.render_input.spec.document.series"


def test_render_schema_refuses_control_material_but_allows_unsafe_words_as_field_names():
    value = _render_input()
    value["result"]["schema"] = {"sql": "SELECT * FROM private"}
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(render_input=value)
    assert excinfo.value.details["path"] == "$.render_input.result.schema.sql"


def test_payload_has_one_exact_aggregate_byte_ceiling(monkeypatch):
    value = _render_input()
    existing = {"toorow.result": {"rows": [{"label": "x" * 100}]}}
    payload = {
        "schema_version": 1,
        "kind": "render",
        "render_input": value,
    }
    measured = render_app_payload.serialized_bytes(
        {**existing, render_app_payload.APP_PAYLOAD_META_KEY: payload}
    )
    monkeypatch.setattr(render_app_payload, "MAX_RENDER_TOOL_META_BYTES", measured + 1)
    render_app_payload.compose_render_app_payload(render_input=value, existing_meta=existing)
    monkeypatch.setattr(render_app_payload, "MAX_RENDER_TOOL_META_BYTES", measured)
    render_app_payload.compose_render_app_payload(render_input=value, existing_meta=existing)
    monkeypatch.setattr(render_app_payload, "MAX_RENDER_TOOL_META_BYTES", measured - 1)
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(
            render_input=value, existing_meta=existing
        )
    assert excinfo.value.code == "render_payload_over_budget"
    assert excinfo.value.details == {"measured": measured, "budget": measured - 1}


def test_only_a_valid_signed_feedback_sidecar_may_cross_the_final_unsafe_scan(
    monkeypatch,
):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    sidecar = mint_delivery_feedback_context(
        org_id="org_1",
        project_id="proj_1",
        result_id="qr_1",
        result_content_hash="a" * 64,
        surface="mcp_app",
        stored_rows=[],
        delivered_rows=[],
        delivered_fields=[],
    )
    render_app_payload.compose_render_app_payload(
        render_input=_render_input(), existing_meta={"toorow.feedback": sidecar}
    )

    damaged = {**sidecar, "token": sidecar["token"] + "x"}
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(
            render_input=_render_input(), existing_meta={"toorow.feedback": damaged}
        )
    assert excinfo.value.code == "unsafe_render_payload"
    assert excinfo.value.details["path"] == "$.toorow.feedback.token"


def test_future_or_extra_top_level_fields_are_refused():
    payload = render_app_payload.compose_render_app_payload(render_input=_render_input())
    future = deepcopy(payload)
    future["schema_version"] = 2
    with pytest.raises(render_app_payload.RenderAppPayloadRefused):
        render_app_payload.validate_render_app_payload(future)
    extra = deepcopy(payload)
    extra["capabilities"] = {}
    with pytest.raises(render_app_payload.RenderAppPayloadRefused):
        render_app_payload.validate_render_app_payload(extra)


def test_share_adapter_delegates_without_changing_its_five_field_envelope(monkeypatch):
    from core import render_shares

    calls: list[dict] = []
    real = render_app_payload.compose_render_input

    def capture(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(render_app_payload, "compose_render_input", capture)
    render_row = (
        "rnd_EXAMPLE", "qr_EXAMPLE", "a" * 64, True, "vsv_EXAMPLE", "echarts",
        "bar/toorow-echarts-bar@1.0.0", "@toorow/card-shell/viz@0.1.0+abcdef0",
        "viz-theme@1", "viz-formatters@1", "console", {}, {}, {}, "b" * 64, None,
    )
    payload_row = (
        "success", {"fields": [{"name": "clicks"}]},
        {
            "grain": "day",
            "query_sql": "SELECT * FROM private",
            "physical_relation": "warehouse.secret_table",
            "unavailable_reason": "password=sekret; relation=warehouse.private",
            "freshness": {
                "state": "fresh",
                "renderer_options": {"series": []},
                "options": {"series": []},
            },
        },
        [{"clicks": 7}], 1, False, "vsv_EXAMPLE", "visualization-spec.v1", 1,
        {"spec_contract_version": "visualization-spec.v1", "schema_version": 1,
         "family": "bar"},
    )
    composed, missing = render_shares._compose_runtime_input(render_row, payload_row)
    assert missing is None
    assert calls and calls[0]["profile"] == "share"
    assert set(composed) == {"result", "spec", "pins", "profile", "display"}
    assert composed["profile"] == "share"
    assert composed["result"]["manifest"] == {
        "grain": "day",
        "freshness": {"state": "fresh"},
        "unavailable_reason": "The source could not produce this result.",
    }


def test_fastmcp_golden_traverses_the_real_share_composer_without_semantic_drift():
    from core import render_shares

    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "ui/cards/shell/src/viz/__tests__/fixtures/analyzeRenderToolResult.json"
    )
    wire = json.loads(fixture_path.read_text(encoding="utf-8"))
    mcp_input = wire["_meta"]["toorow.app_payload"]["render_input"]
    result = mcp_input["result"]
    spec = mcp_input["spec"]
    pins = mcp_input["pins"]
    render_row = (
        "rnd_FIXTURE", result["result_id"], result["content_hash"], True,
        spec["visualization_spec_version_id"], "echarts", pins["renderer_build"],
        pins["runtime_build"], pins["theme_version"], pins["formatter_version"],
        "console", mcp_input["display"], {}, {}, "render_hash", None,
    )
    payload_row = (
        result["outcome"], result["schema"], result["manifest"], result["rows"],
        result["row_count"], result["truncated"],
        spec["visualization_spec_version_id"], spec["spec_contract_version"],
        spec["schema_version"], spec["document"],
    )

    composed, missing = render_shares._compose_runtime_input(render_row, payload_row)
    expected = deepcopy(mcp_input)
    expected["profile"] = "share"
    assert missing is None
    assert composed == expected


def test_manifest_reason_and_missing_link_are_fail_closed_not_blacklisted():
    projected = render_app_payload.project_result_manifest(
        {
            "unavailable_reason": (
                "404 Not found: Table acme-prod:marketing.customer_events "
                "was not found in location EU"
            ),
            "missing_link": "acme-prod:marketing.customer_events",
        }
    )
    assert projected == {
        "unavailable_reason": "The source could not produce this result.",
        "missing_link": "source_output",
    }
    assert render_app_payload.project_result_manifest(
        {
            "unavailable_reason": "this Datastream has no published output to query yet",
            "missing_link": "datastream_output_versions",
        }
    ) == {
        "unavailable_reason": "this Datastream has no published output to query yet",
        "missing_link": "datastream_output_versions",
    }


def _write_runtime(tmp_path: Path, *, hash_override: str | None = None) -> Path:
    bundle = tmp_path / "mcp-app.html"
    bundle.write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "bundle_sha256": hash_override or digest,
        "runtime_build": "@toorow/card-shell/viz@0.1.0+abcdef0",
        "theme_version": "viz-theme@1",
        "formatter_version": "viz-formatters@1",
        "renderers": {
            "bar": {
                "renderer_build": "bar/toorow-echarts-bar@1.0.0",
                "schema_versions": {"min": 1, "max": 1},
                "profiles": [
                    "console",
                    "mcp-inline",
                    "mcp-fullscreen",
                    "mcp-pip",
                    "share",
                ],
            }
        },
    }
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return bundle


def test_runtime_manifest_pins_the_exact_served_html_and_renderer(tmp_path):
    bundle = _write_runtime(tmp_path)
    manifest = render_app_payload.load_runtime_manifest(bundle)
    pins = render_app_payload.resolve_runtime_pins(
        manifest, family="bar", schema_version=1, profile="mcp-inline"
    )
    assert pins == {
        "runtime_build": "@toorow/card-shell/viz@0.1.0+abcdef0",
        "renderer_build": "bar/toorow-echarts-bar@1.0.0",
        "theme_version": "viz-theme@1",
        "formatter_version": "viz-formatters@1",
    }


def test_runtime_manifest_refuses_a_bundle_sha_mismatch(tmp_path):
    bundle = _write_runtime(tmp_path, hash_override="0" * 64)
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.load_runtime_manifest(bundle)
    assert excinfo.value.code == "runtime_manifest_mismatch"


def test_runtime_manifest_refuses_duplicate_json_keys(tmp_path):
    bundle = _write_runtime(tmp_path)
    manifest_path = tmp_path / "runtime-manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1),
        encoding="utf-8",
    )
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.load_runtime_manifest(bundle)
    assert excinfo.value.code == "runtime_manifest_unavailable"


@pytest.mark.parametrize(
    ("family", "schema_version", "profile"),
    # `mcp-pip` used to be the third case: it was a profile no build drew. It is
    # drawn since 2026-08-24, so the case that proves the refusal is a profile the
    # DEPLOYED renderer does not declare -- which is what this test is about, and
    # stays true whatever the vocabulary grows to.
    [("line", 1, "mcp-inline"), ("bar", 2, "mcp-inline"), ("bar", 1, "mcp-carousel")],
)
def test_runtime_manifest_refuses_an_unsupported_render_request(
    tmp_path, family, schema_version, profile
):
    manifest = render_app_payload.load_runtime_manifest(_write_runtime(tmp_path))
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.resolve_runtime_pins(
            manifest, family=family, schema_version=schema_version, profile=profile
        )
    assert excinfo.value.code == "runtime_build_unavailable"


def test_the_well_exemption_holds_where_the_middleware_rescans_the_payload_as_existing_meta():
    """AI-345: the scan has TWO roots, and the exemption is a path SUFFIX so both admit it.

    `mcp_profiles` revalidates a typed payload with the whole `_meta` as
    `existing_meta`, so the same document is walked again from
    `$.toorow.app_payload.render_input.spec.document.bindings`. Proven rather
    than assumed: a root-anchored rule would refuse every render whose answer the
    middleware had split.
    """
    from core.model_channel import APP_PAYLOAD_META_KEY
    from core.visualization_families import WELL_ROLES

    value = _render_input()
    value["spec"]["document"]["bindings"] = {well: [] for well in WELL_ROLES}
    value["spec"]["document"]["bindings"]["series"] = ["sc_EXAMPLE_SERIES"]
    payload = render_app_payload.compose_render_app_payload(render_input=value)
    payload["moved"] = {"rows": [{"series": "a business column"}]}
    render_app_payload.validate_render_app_payload(
        payload, existing_meta={APP_PAYLOAD_META_KEY: payload, "toorow.result": {"rows": []}}
    )


def test_an_option_object_under_a_well_is_refused_at_the_second_root_too():
    from core.model_channel import APP_PAYLOAD_META_KEY

    value = _render_input()
    value["spec"]["document"]["bindings"] = {"series": [{"renderer_options": {}}]}
    smuggled = {"schema_version": 1, "kind": "render", "render_input": value}
    with pytest.raises(render_app_payload.RenderAppPayloadRefused) as excinfo:
        render_app_payload.compose_render_app_payload(
            render_input=_render_input(), existing_meta={APP_PAYLOAD_META_KEY: smuggled}
        )
    assert excinfo.value.details["path"] == (
        "$.toorow.app_payload.render_input.spec.document.bindings.series[0].renderer_options"
    )


@pytest.mark.parametrize("empty", [[], None])
def test_an_empty_well_crosses_like_a_filled_one(empty):
    """The pre-AI-337 scan refused the KEY, so `series: []` failed exactly like a filled well."""
    from core.visualization_families import WELL_ROLES

    value = _render_input()
    value["spec"]["document"]["bindings"] = {well: empty for well in WELL_ROLES}
    payload = render_app_payload.compose_render_app_payload(render_input=value)
    assert payload["render_input"]["spec"]["document"]["bindings"]["series"] == empty
