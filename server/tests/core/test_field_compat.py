"""Unit tests for the declarative field-compatibility engine (Story 26.1, B).

All fixtures use neutral vocabulary (alpha, beta, share_a, ...). No provider
names or real field names appear in this test file (AD-2).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from core.field_compat import (
    FIELD_COMPATIBILITY_KEY,
    CompatViolation,
    FieldCompatibilityError,
    enforce_selection,
    load_module_rules,
    load_rules,
    validate_rules,
    validate_selection,
)
from core.pull_errors import InvalidRequestError

# ---------------------------------------------------------------------------
# Fixture rules: one rule of each of the 5 kinds (neutral vocabulary)
# ---------------------------------------------------------------------------

RULES = {
    "schema_version": "1",
    "_note": "test fixture",
    "rules": [
        {
            "id": "share_vs_attrs",
            "kind": "mutually_exclusive",
            "sets": [["share_a", "share_b"], ["attr_x", "attr_y"]],
        },
        {
            "id": "daily_needs_date",
            "kind": "requires",
            "field": "daily_metric",
            "requires_fields": ["date"],
        },
        {
            "id": "scope_alpha_set",
            "kind": "selectable_set",
            "scope": {"resource": "alpha", "grain": "day"},
            "allowed_fields": ["alpha_1", "alpha_2", "date"],
        },
        {
            "id": "lonely_dim",
            "kind": "only_alone",
            "field": "lonely",
        },
        {
            "id": "max_four_axes",
            "kind": "max_selection",
            "limit": 2,
            "fields": ["axis_1", "axis_2", "axis_3"],
        },
    ],
}


def _rules() -> dict:
    return load_rules(json.loads(json.dumps(RULES)))


# RULES contains a selectable_set rule, so validate_selection is strict about
# a missing context (review 26.1 F-6). Tests that exercise the OTHER kinds
# pass this present-but-non-matching context: the scoped rule is legitimately
# inert (scope mismatch), and no ValueError fires.
NEUTRAL_CONTEXT = {"resource": "other", "grain": "day"}


# ---------------------------------------------------------------------------
# Rule loading: valid + loud failures
# ---------------------------------------------------------------------------


class TestRuleLoading:
    def test_valid_rules_load(self):
        assert validate_rules(RULES) == []
        assert load_rules(RULES) is RULES

    def test_missing_schema_version_is_refused(self):
        bad = {"rules": []}
        errors = validate_rules(bad)
        assert errors, "schema_version is required"
        with pytest.raises(FieldCompatibilityError):
            load_rules(bad)

    def test_wrong_schema_version_is_refused(self):
        errors = validate_rules({"schema_version": "2", "rules": []})
        assert errors

    def test_unknown_kind_is_refused(self):
        bad = {
            "schema_version": "1",
            "rules": [{"id": "r1", "kind": "sometimes_incompatible"}],
        }
        with pytest.raises(FieldCompatibilityError):
            load_rules(bad)

    def test_rule_missing_id_is_refused(self):
        bad = {
            "schema_version": "1",
            "rules": [{"kind": "only_alone", "field": "x"}],
        }
        with pytest.raises(FieldCompatibilityError):
            load_rules(bad)

    def test_duplicate_rule_ids_are_refused(self):
        bad = {
            "schema_version": "1",
            "rules": [
                {"id": "dup", "kind": "only_alone", "field": "x"},
                {"id": "dup", "kind": "only_alone", "field": "y"},
            ],
        }
        with pytest.raises(FieldCompatibilityError, match="duplicate rule id"):
            load_rules(bad)

    def test_mutually_exclusive_needs_two_sets(self):
        bad = {
            "schema_version": "1",
            "rules": [
                {"id": "r1", "kind": "mutually_exclusive", "sets": [["a"]]},
            ],
        }
        with pytest.raises(FieldCompatibilityError):
            load_rules(bad)

    def test_max_selection_limit_must_be_positive_integer(self):
        bad = {
            "schema_version": "1",
            "rules": [{"id": "r1", "kind": "max_selection", "limit": 0}],
        }
        with pytest.raises(FieldCompatibilityError):
            load_rules(bad)

    def test_errors_are_deterministic_strings(self):
        bad = {
            "schema_version": "1",
            "rules": [
                {"id": "r1", "kind": "requires", "field": "x"},
                {"id": "r2", "kind": "only_alone"},
            ],
        }
        first = validate_rules(bad)
        second = validate_rules(bad)
        assert first == second
        assert all(isinstance(e, str) for e in first)


# ---------------------------------------------------------------------------
# validate_selection: the 5 rule kinds
# ---------------------------------------------------------------------------


class TestMutuallyExclusive:
    def test_fields_from_two_sets_violate(self):
        violations = validate_selection(
            ["share_a", "attr_x"], _rules(), context=NEUTRAL_CONTEXT
        )
        assert [v.rule_id for v in violations] == ["share_vs_attrs"]
        assert violations[0].kind == "mutually_exclusive"
        assert set(violations[0].fields) == {"share_a", "attr_x"}

    def test_fields_from_one_set_pass(self):
        assert (
            validate_selection(
                ["share_a", "share_b", "date"], _rules(), context=NEUTRAL_CONTEXT
            )
            == []
        )

    def test_non_disjoint_sets_are_refused_at_load(self):
        """Review 26.1 F-5: a field in two sets of the SAME rule makes the
        rule self-contradictory -- refused like a duplicate id."""
        bad = {
            "schema_version": "1",
            "rules": [
                {
                    "id": "overlapping",
                    "kind": "mutually_exclusive",
                    "sets": [["share_a", "shared_field"], ["attr_x", "shared_field"]],
                },
            ],
        }
        errors = validate_rules(bad)
        assert any("disjoint" in e and "shared_field" in e for e in errors)
        with pytest.raises(FieldCompatibilityError, match="disjoint"):
            load_rules(bad)

    def test_disjoint_sets_across_rules_are_fine(self):
        """Only sets WITHIN one rule must be disjoint; two different rules
        may reference the same field."""
        ok = {
            "schema_version": "1",
            "rules": [
                {
                    "id": "r1",
                    "kind": "mutually_exclusive",
                    "sets": [["a", "b"], ["c"]],
                },
                {
                    "id": "r2",
                    "kind": "mutually_exclusive",
                    "sets": [["a"], ["d"]],
                },
            ],
        }
        assert validate_rules(ok) == []


class TestRequires:
    def test_missing_required_field_violates(self):
        violations = validate_selection(
            ["daily_metric"], _rules(), context=NEUTRAL_CONTEXT
        )
        assert [v.rule_id for v in violations] == ["daily_needs_date"]
        assert violations[0].fields == ("date",)

    def test_requirement_satisfied_passes(self):
        assert (
            validate_selection(
                ["daily_metric", "date"], _rules(), context=NEUTRAL_CONTEXT
            )
            == []
        )

    def test_rule_inert_when_trigger_not_selected(self):
        assert validate_selection(["date"], _rules(), context=NEUTRAL_CONTEXT) == []


class TestSelectableSet:
    CONTEXT = {"resource": "alpha", "grain": "day"}

    def test_field_outside_scope_set_violates(self):
        violations = validate_selection(
            ["alpha_1", "beta_1"], _rules(), context=self.CONTEXT
        )
        assert [v.rule_id for v in violations] == ["scope_alpha_set"]
        assert violations[0].fields == ("beta_1",)

    def test_allowed_fields_pass_in_scope(self):
        assert (
            validate_selection(["alpha_1", "alpha_2"], _rules(), context=self.CONTEXT)
            == []
        )

    def test_missing_context_raises_value_error(self):
        """Review 26.1 F-6 (strict by default): selectable_set rules with a
        None/{} context is a CALLER BUG, not a selection violation."""
        with pytest.raises(ValueError, match="selectable_set"):
            validate_selection(["beta_1"], _rules())
        with pytest.raises(ValueError, match="allow_missing_context"):
            validate_selection(["beta_1"], _rules(), context={})

    def test_missing_context_names_the_scoped_rule(self):
        with pytest.raises(ValueError, match="scope_alpha_set"):
            validate_selection(["beta_1"], _rules())

    def test_rule_inert_without_context_when_explicitly_allowed(self):
        """The documented opt-out: the scoped rule is inert, others still run."""
        assert (
            validate_selection(["beta_1"], _rules(), allow_missing_context=True) == []
        )
        violations = validate_selection(
            ["lonely", "beta_1"], _rules(), allow_missing_context=True
        )
        assert [v.rule_id for v in violations] == ["lonely_dim"]

    def test_no_selectable_set_rules_need_no_context(self):
        """Rules without any selectable_set kind stay context-free."""
        rules = load_rules(
            {
                "schema_version": "1",
                "rules": [{"id": "solo", "kind": "only_alone", "field": "lonely"}],
            }
        )
        assert validate_selection(["date"], rules) == []

    def test_rule_inert_when_scope_partially_matches(self):
        context = {"resource": "alpha", "grain": "hour"}
        assert validate_selection(["beta_1"], _rules(), context=context) == []


class TestOnlyAlone:
    def test_combined_lonely_field_violates(self):
        violations = validate_selection(
            ["lonely", "date"], _rules(), context=NEUTRAL_CONTEXT
        )
        assert [v.rule_id for v in violations] == ["lonely_dim"]
        assert violations[0].fields == ("date",)

    def test_lonely_field_alone_passes(self):
        assert validate_selection(["lonely"], _rules(), context=NEUTRAL_CONTEXT) == []


class TestMaxSelection:
    def test_over_limit_within_pool_violates(self):
        violations = validate_selection(
            ["axis_1", "axis_2", "axis_3"], _rules(), context=NEUTRAL_CONTEXT
        )
        assert [v.rule_id for v in violations] == ["max_four_axes"]

    def test_at_limit_passes(self):
        assert (
            validate_selection(
                ["axis_1", "axis_2", "date"], _rules(), context=NEUTRAL_CONTEXT
            )
            == []
        )

    def test_limit_without_pool_counts_whole_selection(self):
        rules = load_rules(
            {
                "schema_version": "1",
                "rules": [{"id": "cap", "kind": "max_selection", "limit": 1}],
            }
        )
        assert validate_selection(["a"], rules) == []
        violations = validate_selection(["a", "b"], rules)
        assert [v.rule_id for v in violations] == ["cap"]


class TestSelectionShapes:
    def test_catalog_contract_selection_dict_is_accepted(self):
        selection = {"metrics": ["daily_metric"], "dimensions": ["date"]}
        assert validate_selection(selection, _rules(), context=NEUTRAL_CONTEXT) == []

    def test_empty_selection_passes(self):
        assert validate_selection([], _rules(), context=NEUTRAL_CONTEXT) == []
        assert validate_selection(None, _rules(), context=NEUTRAL_CONTEXT) == []

    def test_violations_are_sorted_and_deterministic(self):
        selection = ["lonely", "daily_metric", "share_a", "attr_x"]
        first = validate_selection(selection, _rules(), context=NEUTRAL_CONTEXT)
        second = validate_selection(selection, _rules(), context=NEUTRAL_CONTEXT)
        assert first == second
        assert first == sorted(first)
        assert isinstance(first[0], CompatViolation)


# ---------------------------------------------------------------------------
# enforce_selection: typed pre-call refusal with the rule id in error_detail
# ---------------------------------------------------------------------------


class TestEnforceSelection:
    def test_compatible_selection_is_a_noop(self):
        assert (
            enforce_selection(["share_a", "date"], _rules(), context=NEUTRAL_CONTEXT)
            is None
        )

    def test_violation_raises_invalid_request(self):
        with pytest.raises(InvalidRequestError) as exc_info:
            enforce_selection(["share_a", "attr_x"], _rules(), context=NEUTRAL_CONTEXT)
        err = exc_info.value
        assert err.error_class == "invalid_request"
        assert err.retryable is False

    def test_rule_id_is_in_message_and_payload(self):
        with pytest.raises(InvalidRequestError) as exc_info:
            enforce_selection(["lonely", "date"], _rules(), context=NEUTRAL_CONTEXT)
        err = exc_info.value
        assert "lonely_dim" in str(err)
        payload = err.provider_payload
        assert isinstance(payload, dict)
        assert payload["violations"][0]["rule_id"] == "lonely_dim"

    def test_multiple_rule_ids_all_reported(self):
        with pytest.raises(InvalidRequestError) as exc_info:
            enforce_selection(
                ["lonely", "daily_metric"], _rules(), context=NEUTRAL_CONTEXT
            )
        message = str(exc_info.value)
        assert "daily_needs_date" in message
        assert "lonely_dim" in message

    def test_missing_context_raises_value_error_not_invalid_request(self):
        """F-6: a missing context is a caller bug (ValueError), never a
        provider-shaped InvalidRequestError."""
        with pytest.raises(ValueError, match="selectable_set"):
            enforce_selection(["share_a"], _rules())

    def test_allow_missing_context_passes_through(self):
        assert (
            enforce_selection(["share_a"], _rules(), allow_missing_context=True)
            is None
        )


# ---------------------------------------------------------------------------
# load_module_rules: the loader hook
# ---------------------------------------------------------------------------


class TestLoadModuleRules:
    def _write_sources(self, tmp_path: Path, payload) -> Path:
        sources_dir = tmp_path / "catalog_sources"
        sources_dir.mkdir()
        target = sources_dir / "catalog_sources.json"
        if isinstance(payload, str):
            target.write_text(payload, encoding="utf-8")
        else:
            target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def test_absent_file_returns_none(self, tmp_path: Path):
        assert load_module_rules(tmp_path) is None

    def test_absent_block_returns_none(self, tmp_path: Path):
        self._write_sources(tmp_path, {"connector": "neutral", "sources": []})
        assert load_module_rules(tmp_path) is None

    def test_module_local_block_without_schema_version_is_ignored(self, tmp_path: Path):
        """A block WITHOUT the schema_version marker is a module-local
        companion format (consumed by the module's own code): the core
        engine treats it as undeclared -- no-op, never a loud skip."""
        local_block = {
            "_note": "module-local matrix, not the core contract",
            "selectable_sets": {"alpha": ["alpha_1"]},
            "rules": ["free-text invariant, not a core rule object"],
        }
        self._write_sources(tmp_path, {FIELD_COMPATIBILITY_KEY: local_block})
        assert load_module_rules(tmp_path) is None

    def test_valid_block_is_returned(self, tmp_path: Path):
        self._write_sources(tmp_path, {FIELD_COMPATIBILITY_KEY: RULES})
        rules = load_module_rules(tmp_path)
        assert rules is not None
        assert rules["schema_version"] == "1"

    def test_malformed_block_raises_loudly(self, tmp_path: Path):
        self._write_sources(
            tmp_path,
            {FIELD_COMPATIBILITY_KEY: {"schema_version": "1", "rules": [{"id": "x"}]}},
        )
        with pytest.raises(FieldCompatibilityError):
            load_module_rules(tmp_path)

    def test_unparseable_json_raises_loudly(self, tmp_path: Path):
        self._write_sources(tmp_path, "{not json")
        with pytest.raises(FieldCompatibilityError, match="not valid JSON"):
            load_module_rules(tmp_path)


class TestLoaderIntegration:
    """The loader skips a module whose field_compatibility block is malformed."""

    MANIFEST = {
        "schema_version": "1.0",
        "name": "neutral-module",
        "display_name": "Neutral",
        "auth_type": "oauth2",
        "report_profiles": [
            {
                "id": "standard_daily",
                "display_name": "Standard daily",
                "metrics": [],
                "dimensions": [],
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": False,
                    "realtime": False,
                },
            }
        ],
        "canonical_metric_mapping": {},
        "canonical_dimension_mapping": {},
    }

    # The loader only requires a module-level mcp_app attribute (hasattr check).
    CONNECTOR = "mcp_app = object()\n"

    def _make_module(self, tmp_path: Path, compat_block) -> Path:
        module_dir = tmp_path / "neutral-module"
        module_dir.mkdir()
        (module_dir / "manifest.json").write_text(
            json.dumps(self.MANIFEST), encoding="utf-8"
        )
        (module_dir / "connector.py").write_text(self.CONNECTOR, encoding="utf-8")
        if compat_block is not None:
            sources_dir = module_dir / "catalog_sources"
            sources_dir.mkdir()
            (sources_dir / "catalog_sources.json").write_text(
                json.dumps({FIELD_COMPATIBILITY_KEY: compat_block}), encoding="utf-8"
            )
        return tmp_path

    def test_module_without_block_still_loads(self, tmp_path: Path):
        from core.loader import scan_and_load_modules

        loaded = scan_and_load_modules(self._make_module(tmp_path, None))
        assert [m.name for m in loaded] == ["neutral-module"]
        # Review 26.1 F-10: no block => no attached rules.
        assert loaded[0].field_compat_rules is None

    def test_module_with_valid_block_loads_and_attaches_rules(self, tmp_path: Path):
        """Review 26.1 F-10: the loader ATTACHES the parsed rules to the
        LoadedModule instead of discarding them after validation."""
        from core.loader import scan_and_load_modules

        loaded = scan_and_load_modules(self._make_module(tmp_path, RULES))
        assert [m.name for m in loaded] == ["neutral-module"]
        attached = loaded[0].field_compat_rules
        assert attached is not None
        assert attached == RULES
        assert attached["schema_version"] == "1"
        assert [r["id"] for r in attached["rules"]] == [
            r["id"] for r in RULES["rules"]
        ]

    def test_module_with_malformed_block_is_skipped_loudly(self, tmp_path: Path, caplog):
        import logging

        from core.loader import scan_and_load_modules

        modules_dir = self._make_module(
            tmp_path, {"schema_version": "1", "rules": [{"id": "x"}]}
        )
        with caplog.at_level(logging.ERROR, logger="core.loader"):
            loaded = scan_and_load_modules(modules_dir)
        assert loaded == []
        skip_logs = [
            r.getMessage()
            for r in caplog.records
            if "field_compatibility_invalid" in r.getMessage()
        ]
        assert skip_logs, "expected a loud module_skipped log for malformed rules"


# ---------------------------------------------------------------------------
# Versioned schema file
# ---------------------------------------------------------------------------


class TestVersionedSchema:
    SCHEMA_PATH = (
        Path(__file__).parents[2] / "core" / "schemas" / "field-compatibility.schema.json"
    )

    def test_schema_file_exists_and_is_valid(self):
        import jsonschema

        schema = json.loads(self.SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$id"] == (
            "https://toorow.dev/schemas/field-compatibility.schema.json"
        )

    def test_schema_pins_version_1(self):
        schema = json.loads(self.SCHEMA_PATH.read_text(encoding="utf-8"))
        assert schema["properties"]["schema_version"] == {"const": "1"}


# ---------------------------------------------------------------------------
# AD-2 / HG-1: no provider vocabulary in the core file
# ---------------------------------------------------------------------------


class TestNoProviderVocabulary:
    def test_core_file_has_no_provider_names(self):
        source = (
            Path(__file__).parents[2] / "core" / "field_compat.py"
        ).read_text(encoding="utf-8").lower()
        forbidden = [
            "meta",
            "facebook",
            "google",
            "ga4",
            "gsc",
            "tiktok",
            "shopify",
            "stripe",
            "hubspot",
            "klaviyo",
            "linkedin",
            "github",
            "bing",
            "microsoft",
            "amazon",
            "pinterest",
        ]
        hits = [w for w in forbidden if re.search(rf"\b{re.escape(w)}\b", source)]
        assert not hits, f"field_compat.py must contain no provider vocabulary: {hits}"
