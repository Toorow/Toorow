"""Unit tests for server/core/flows.py (Story 8.7, AC3, AC7).

Covers the guardrails and behaviour with mocked DB connections (no live Postgres):
  - schema validation errors are actionable (json-path + message),
  - secrets are untouchable (a smuggled token field is rejected by the schema),
  - scope violations return not-found + write an access_denied audit row,
  - idempotency (same doc twice -> changed:false, no audit),
  - datastream upsert goes through the OWNING module functions (single write path),
  - report base/override merge happens at the flows layer.

The live end-to-end acceptance is in tests/conformance/test_flows_conformance.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from core import flows

# ---------------------------------------------------------------------------
# validate_flow — actionable errors + secret rejection
# ---------------------------------------------------------------------------


class TestValidateFlow:
    def _valid_datastream(self) -> dict:
        return {
            "schema_version": "1",
            "kind": "datastream",
            "project_id": "proj_a",
            "name": "GA Standard",
            "module_name": "google-analytics",
        }

    def test_valid_datastream_ok(self):
        ok, errors = flows.validate_flow(self._valid_datastream())
        assert ok is True
        assert errors == []

    def test_versioned_datastream_intent_is_validated_by_shared_contract(self):
        from tests.core.test_datastream_intents import _intent

        doc = {
            "schema_version": "2",
            "kind": "datastream",
            "project_id": "proj_a",
            "name": "Campaign feed",
            "intent": _intent(),
            "idempotency_key": "flow-request-01",
        }
        ok, errors = flows.validate_flow(doc)
        assert ok is True, errors
        doc["intent"]["source"]["credentials"] = {"token": "secret"}
        ok, errors = flows.validate_flow(doc)
        assert ok is False
        assert any("intent" in error["path"] for error in errors)

    def test_missing_kind_is_error(self):
        ok, errors = flows.validate_flow({"schema_version": "1"})
        assert ok is False
        assert errors[0]["path"] == "/kind"

    def test_missing_required_field_actionable(self):
        doc = self._valid_datastream()
        del doc["name"]
        ok, errors = flows.validate_flow(doc)
        assert ok is False
        assert any("obligatoire" in e["message"] for e in errors)

    def test_secret_field_rejected_by_schema(self):
        """GUARDRAIL: a smuggled token field fails validation (additionalProperties)."""
        doc = self._valid_datastream()
        doc["access_token"] = "ya29.SECRET"
        ok, errors = flows.validate_flow(doc)
        assert ok is False
        assert any(
            "secret" in e["message"].lower() or "non autorise" in e["message"] for e in errors
        )

    def test_secret_in_config_rejected(self):
        """A token nested inside config is also rejected (config is closed)."""
        doc = self._valid_datastream()
        doc["config"] = {"date_format": "%Y-%m-%d", "token": "SECRET"}
        ok, errors = flows.validate_flow(doc)
        assert ok is False
        assert any(e["path"].startswith("/config") for e in errors)

    def test_enum_violation_actionable(self):
        doc = self._valid_datastream()
        doc["schedule_mode"] = "hourly"
        ok, errors = flows.validate_flow(doc)
        assert ok is False
        assert any(e["path"] == "/schedule_mode" for e in errors)

    def test_report_requires_base_report_id(self):
        doc = {"schema_version": "1", "kind": "report", "id": "x"}
        ok, errors = flows.validate_flow(doc)
        assert ok is False
        assert any("base_report_id" in e["message"] for e in errors)

    def test_report_metric_definitions_accepted(self):
        """R6: metric_definitions + llm_commentary_guidelines are valid schema fields."""
        doc = {
            "schema_version": "1",
            "kind": "report",
            "id": "gsc/position_movements",
            "base_report_id": "gsc/position_movements",
            "metric_definitions": {
                "clicks": {
                    "definition": "Clics organiques",
                    "unit": "count",
                    "direction": "up_good",
                    "caveats": None,
                },
            },
            "llm_commentary_guidelines": "Reste factuel, cite les chiffres.",
        }
        ok, errors = flows.validate_flow(doc)
        assert ok is True, errors

    def test_report_metric_definition_missing_definition(self):
        doc = {
            "schema_version": "1",
            "kind": "report",
            "id": "m/r",
            "base_report_id": "m/r",
            "metric_definitions": {"clicks": {"unit": "count"}},
        }
        ok, errors = flows.validate_flow(doc)
        assert ok is False

    def test_report_card_template_accepted(self):
        """Story 9.8: card_template is a valid flow.report field (card-id charset)."""
        doc = {
            "schema_version": "1",
            "kind": "report",
            "id": "m/r",
            "base_report_id": "m/r",
            "card_template": "conversions",
        }
        ok, errors = flows.validate_flow(doc)
        assert ok is True, errors

    def test_report_card_template_bad_charset_rejected(self):
        """Story 9.8: card_template is pattern-guarded to the card-id charset."""
        doc = {
            "schema_version": "1",
            "kind": "report",
            "id": "m/r",
            "base_report_id": "m/r",
            "card_template": "Not A Card!",
        }
        ok, errors = flows.validate_flow(doc)
        assert ok is False

    def test_report_card_template_merges_into_override(self):
        """card_template is on the override merge whitelist -> survives get_flow merge."""
        base = {
            "schema_version": "1",
            "kind": "report",
            "id": "m/r",
            "base_report_id": "m/r",
            "metrics": ["clicks"],
        }
        override = {"card_template": "keywords"}
        merged = flows._merge_report(base, override, "m/r", "projA")
        assert merged["card_template"] == "keywords"


# ---------------------------------------------------------------------------
# compute_diff — minimal before/after + idempotency
# ---------------------------------------------------------------------------


class TestComputeDiff:
    def test_no_change_empty_diff(self):
        a = {"name": "x", "enabled": True, "updated_at": "t1"}
        b = {"name": "x", "enabled": True, "updated_at": "t2"}  # only ts differs
        assert flows.compute_diff(a, b) == {}

    def test_change_reported(self):
        a = {"name": "x", "enabled": True}
        b = {"name": "y", "enabled": True}
        diff = flows.compute_diff(a, b)
        assert diff == {"name": {"before": "x", "after": "y"}}

    def test_create_reports_all_keys(self):
        diff = flows.compute_diff(None, {"name": "x", "kind": "datastream"})
        # kind is ignored; name is a change
        assert "name" in diff
        assert "kind" not in diff


# ---------------------------------------------------------------------------
# Scope enforcement (AD-5) — not-found + audit
# ---------------------------------------------------------------------------


class TestScope:
    def test_scope_violation_raises_and_audits(self):
        conn = MagicMock()
        with (
            patch("core.project_access.identity_has_project_access", return_value=False),
            patch("core.flows._audit") as mock_audit,
        ):
            with pytest.raises(flows.FlowScopeError):
                flows._assert_access("proj_x", "mallory", conn)
            mock_audit.assert_called_once()
            args = mock_audit.call_args[0]
            assert args[1] == "access_denied"

    def test_access_ok_no_audit(self):
        conn = MagicMock()
        with (
            patch("core.project_access.identity_has_project_access", return_value=True),
            patch("core.flows._audit") as mock_audit,
        ):
            flows._assert_access("proj_a", "alice", conn)
            mock_audit.assert_not_called()

    def test_strict_role_lookup_unavailable_fails_closed(self):
        from core.project_access import ProjectAccessUnavailable

        conn = MagicMock()
        with patch(
            "core.project_access.identity_has_project_role",
            side_effect=ProjectAccessUnavailable("database unavailable"),
        ):
            with pytest.raises(flows.FlowUnavailableError):
                flows._assert_access("proj_a", "alice", conn, minimum_role="member")

    def test_get_flow_scope_violation(self):
        conn = MagicMock()
        with (
            patch("core.project_access.identity_has_project_access", return_value=False),
            patch("core.flows._audit"),
        ):
            with pytest.raises(flows.FlowScopeError):
                flows.get_flow("proj_x", "datastream", "ds_1", "mallory", conn)


# ---------------------------------------------------------------------------
# upsert_flow — validate-before-write + datastream single write path
# ---------------------------------------------------------------------------


class TestUpsertDatastream:
    def _doc(self, **over):
        d = {
            "schema_version": "1",
            "kind": "datastream",
            "project_id": "proj_a",
            "name": "GA Standard",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_x",
            "enabled": True,
            "schedule_mode": "nightly",
            "refetch_days": 3,
            "date_window_days": 30,
            "mappings": [{"source_field": "sessions", "target_field": "sessions"}],
        }
        d.update(over)
        return d

    def test_invalid_doc_refused_before_db(self):
        conn = MagicMock()
        with pytest.raises(flows.FlowValidationError):
            flows.upsert_flow("proj_a", {"kind": "datastream"}, "alice", conn)
        # No DB write attempted.
        conn.cursor.assert_not_called()

    def test_create_uses_owning_module_functions(self):
        conn = MagicMock()
        created_row = {
            "id": "ds_new",
            "project_id": "proj_a",
            "name": "GA Standard",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_x",
            "report_profile_id": None,
            "enabled": True,
            "schedule_mode": "nightly",
            "refetch_days": 3,
            "date_window_days": 30,
            "config": {},
        }
        with (
            patch("core.project_access.identity_has_project_role", return_value=True),
            patch("core.flows._connection_ref_belongs", return_value=True),
            patch(
                "core.flows._fetch_mappings",
                return_value=[
                    {"source_field": "sessions", "target_field": "sessions", "is_key_column": False}
                ],
            ),
            patch("core.datastreams.get_datastream", return_value=created_row),
            patch("core.datastreams.create_datastream", return_value=created_row) as mock_create,
            patch("core.flows._apply_mappings") as mock_apply,
            patch("core.flows._audit") as mock_audit,
        ):
            result = flows.upsert_flow("proj_a", self._doc(), "alice", conn)

        mock_create.assert_called_once()  # single write path
        mock_apply.assert_called_once()
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][1] == "flow_updated"
        assert result["changed"] is True
        assert result["kind"] == "datastream"

    def test_versioned_flow_delegates_to_intent_service(self):
        from core.geographic_reporting import GeographicPosture

        from tests.core.test_datastream_intents import _catalog, _intent

        conn = MagicMock()
        doc = {
            "schema_version": "2",
            "kind": "datastream",
            "project_id": "proj_a",
            "name": "Campaign feed",
            "intent": _intent(),
            "idempotency_key": "flow-request-01",
        }
        created = {"id": "ds_new", "project_id": "proj_a", "name": "Campaign feed"}
        saved = {"id": "dsp_01", "version_number": 1, "executable": True}
        with (
            patch("core.project_access.identity_has_project_role", return_value=True),
            patch("core.flows._connection_ref_belongs", return_value=True),
            patch(
                "core.geographic_reporting.fetch_project_geographic_posture",
                return_value=GeographicPosture(),
            ),
            patch(
                "core.source_capabilities.get_scoped_source_capabilities",
                return_value=_catalog(),
            ),
            patch("core.datastreams.create_datastream", return_value=created) as create,
            patch("core.datastream_intents.save_datastream_intent", return_value=saved) as save,
            patch(
                "core.datastreams.get_datastream",
                return_value={
                    **created,
                    "versioned": True,
                    "intent_payload": _intent(),
                    "current_plan_version_id": "dsp_01",
                },
            ),
            patch("core.flows._fetch_mappings", return_value=[]),
        ):
            result = flows.upsert_flow(
                "proj_a", doc, "member-1", conn, loaded_modules=[MagicMock()]
            )

        create.assert_called_once()
        save.assert_called_once()
        assert save.call_args.kwargs["idempotency_key"] == "flow-request-01"
        assert save.call_args.kwargs["geographic_posture"] == GeographicPosture()
        assert result["flow"]["schema_version"] == "2"
        assert result["plan_version"]["id"] == "dsp_01"

    def test_connection_ref_not_in_project_rejected(self):
        """GUARDRAIL: connection_ref_id must belong to the project (opaque FK)."""
        conn = MagicMock()
        with (
            patch("core.project_access.identity_has_project_access", return_value=True),
            patch("core.flows._connection_ref_belongs", return_value=False),
        ):
            with pytest.raises(flows.FlowValidationError) as ei:
                flows.upsert_flow("proj_a", self._doc(), "alice", conn)
            assert any("connection_ref_id" in e["path"] for e in ei.value.errors)

    def test_update_idempotent_noop(self):
        """Same doc twice => changed:false, no audit."""
        conn = MagicMock()
        existing = {
            "id": "ds_1",
            "project_id": "proj_a",
            "name": "GA Standard",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_x",
            "report_profile_id": None,
            "enabled": True,
            "schedule_mode": "nightly",
            "refetch_days": 3,
            "date_window_days": 30,
            "config": {},
        }
        mappings = [
            {"source_field": "sessions", "target_field": "sessions", "is_key_column": False}
        ]
        with (
            patch("core.project_access.identity_has_project_role", return_value=True),
            patch("core.flows._connection_ref_belongs", return_value=True),
            patch("core.datastreams.get_datastream", return_value=existing),
            patch("core.flows._fetch_mappings", return_value=mappings),
            patch("core.datastreams.update_datastream") as mock_update,
            patch("core.flows._audit") as mock_audit,
        ):
            result = flows.upsert_flow("proj_a", self._doc(id="ds_1"), "alice", conn)

        assert result["changed"] is False
        mock_update.assert_not_called()
        mock_audit.assert_not_called()

    def test_project_mismatch_rejected(self):
        conn = MagicMock()
        with pytest.raises(flows.FlowValidationError):
            flows.upsert_flow("proj_a", self._doc(project_id="proj_b"), "alice", conn)


# ---------------------------------------------------------------------------
# _apply_mappings -- Fix [HIGH #4]: removal path
# ---------------------------------------------------------------------------


class TestApplyMappings:
    """Tests for the mapping removal fix (review-epic-8 #4)."""

    def test_remove_mapping_calls_delete(self):
        """Dropping a source_field from desired triggers a DELETE on the DB."""
        from core.flows import _apply_mappings

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        current = [
            {"source_field": "sessions", "target_field": "sessions"},
            {"source_field": "cost", "target_field": "cost"},
        ]
        # desired drops 'cost'
        desired = [{"source_field": "sessions", "target_field": "sessions"}]

        with patch("core.datamodel.upsert_mapping") as mock_upsert:
            _apply_mappings("ds_1", desired, current, "alice", conn)

        # DELETE was issued for 'cost'
        delete_sql_call = cur.execute.call_args
        assert delete_sql_call is not None
        assert "DELETE" in delete_sql_call[0][0]
        srcs_arg = delete_sql_call[0][1][1]
        assert "cost" in srcs_arg
        # No upsert needed (sessions unchanged)
        mock_upsert.assert_not_called()

    def test_remove_mapping_diff_reflects_removal(self):
        """After removing a mapping via upsert_flow, changed:True and diff contains mappings."""
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        existing = {
            "id": "ds_1",
            "project_id": "proj_a",
            "name": "GA Standard",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_x",
            "report_profile_id": None,
            "enabled": True,
            "schedule_mode": "nightly",
            "refetch_days": 3,
            "date_window_days": 30,
            "config": {},
        }
        # current mappings have two entries
        current_mappings = [
            {"source_field": "sessions", "target_field": "sessions", "is_key_column": False},
            {"source_field": "cost", "target_field": "cost", "is_key_column": False},
        ]
        # after_mappings only has one (cost was removed)
        after_mappings = [
            {"source_field": "sessions", "target_field": "sessions", "is_key_column": False},
        ]

        with (
            patch("core.project_access.identity_has_project_role", return_value=True),
            patch("core.flows._connection_ref_belongs", return_value=True),
            patch("core.datastreams.get_datastream", return_value=existing),
            patch(
                "core.flows._fetch_mappings",
                side_effect=[
                    current_mappings,  # before
                    after_mappings,  # after_flow read
                ],
            ),
            patch("core.datastreams.update_datastream", return_value=existing),
            patch("core.flows._apply_mappings") as mock_apply,
            patch("core.flows._audit"),
        ):
            doc = {
                "schema_version": "1",
                "kind": "datastream",
                "id": "ds_1",
                "project_id": "proj_a",
                "name": "GA Standard",
                "module_name": "google-analytics",
                "connection_ref_id": "conn_x",
                "enabled": True,
                "schedule_mode": "nightly",
                "refetch_days": 3,
                "date_window_days": 30,
                # Only sessions, cost dropped
                "mappings": [{"source_field": "sessions", "target_field": "sessions"}],
            }
            result = flows.upsert_flow("proj_a", doc, "alice", conn)

        # _apply_mappings was called -- it handles the delete pass internally
        mock_apply.assert_called_once()
        # The diff should detect the mapping change (mappings changed before vs after)
        assert result["changed"] is True

    def test_add_mapping_no_delete(self):
        """Adding a new mapping does not DELETE anything."""
        from core.flows import _apply_mappings

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        current = [{"source_field": "sessions", "target_field": "sessions"}]
        desired = [
            {"source_field": "sessions", "target_field": "sessions"},
            {"source_field": "revenue", "target_field": "revenue"},
        ]

        with patch("core.datamodel.upsert_mapping") as mock_upsert:
            _apply_mappings("ds_1", desired, current, "alice", conn)

        # No DELETE was called (cursor.execute not invoked for delete path)
        cur.execute.assert_not_called()
        # Upsert called for revenue
        mock_upsert.assert_called_once()
        assert mock_upsert.call_args[1]["source_field"] == "revenue"


# ---------------------------------------------------------------------------
# CREATE atomicity -- Fix [MEDIUM #5]: no orphan on mapping failure
# ---------------------------------------------------------------------------


class TestCreateAtomicity:
    """Tests for the CREATE atomicity fix (review-epic-8 #5)."""

    def _doc(self):
        return {
            "schema_version": "1",
            "kind": "datastream",
            "project_id": "proj_a",
            "name": "GA Standard",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_x",
            "enabled": True,
            "schedule_mode": "nightly",
            "refetch_days": 3,
            "date_window_days": 30,
            "mappings": [{"source_field": "cost", "target_field": "cost"}],
        }

    def test_create_rollback_on_mapping_failure(self):
        """If _apply_mappings raises, conn.rollback() is called and error propagates."""
        conn = MagicMock()
        created_row = {
            "id": "ds_new",
            "project_id": "proj_a",
            "name": "GA Standard",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_x",
            "report_profile_id": None,
            "enabled": True,
            "schedule_mode": "nightly",
            "refetch_days": 3,
            "date_window_days": 30,
            "config": {},
        }

        with (
            patch("core.project_access.identity_has_project_role", return_value=True),
            patch("core.flows._connection_ref_belongs", return_value=True),
            patch("core.datastreams.create_datastream", return_value=created_row),
            patch("core.flows._apply_mappings", side_effect=RuntimeError("DB write failed")),
            patch("core.flows._audit"),
        ):
            with pytest.raises(RuntimeError, match="DB write failed"):
                flows.upsert_flow("proj_a", self._doc(), "alice", conn)

        # Rollback must have been called (no orphaned datastream row)
        conn.rollback.assert_called_once()
        # Commit must NOT have been called
        conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Report merge (base pack + override at the flows layer)
# ---------------------------------------------------------------------------


class TestReportMerge:
    def test_merge_override_wins(self):
        base = {
            "schema_version": "1",
            "kind": "report",
            "id": "m/r",
            "base_report_id": "m/r",
            "display_name": "Base",
            "metrics": ["clicks"],
            "date_window": {"default_days": 90, "min_days": 21},
            "narrative_prompt": "base prompt",
        }
        override = {
            "date_window": {"default_days": 30},
            "metric_definitions": {"clicks": {"definition": "Clics"}},
        }
        merged = flows._merge_report(base, override, "m/r", "proj_a")
        # date_window shallow-merges: default_days overridden, min_days preserved
        assert merged["date_window"]["default_days"] == 30
        assert merged["date_window"]["min_days"] == 21
        # metric_definitions come from override (R6)
        assert merged["metric_definitions"]["clicks"]["definition"] == "Clics"
        # untouched base fields preserved
        assert merged["metrics"] == ["clicks"]
        assert merged["project_id"] == "proj_a"

    def test_merge_no_override_returns_base(self):
        base = {
            "schema_version": "1",
            "kind": "report",
            "id": "m/r",
            "base_report_id": "m/r",
            "metrics": ["clicks"],
        }
        merged = flows._merge_report(base, None, "m/r", "proj_a")
        assert merged["metrics"] == ["clicks"]
        assert merged["project_id"] == "proj_a"

    def test_public_report_read_api_exists(self):
        """AI-02 guard (review-9-3 F-7): cards.py / main.py use the PUBLIC flows names.

        The public wrappers + the one-step fetch_report_override_merged helper must
        exist so callers never reach into the underscore-prefixed report-read helpers.
        """
        assert flows.base_report_doc is flows._base_report_doc
        assert flows.fetch_report_override is flows._fetch_report_override
        assert flows.merge_report is flows._merge_report
        assert callable(flows.fetch_report_override_merged)

    def test_fetch_report_override_merged_wraps_three_steps(self):
        """The public one-step helper returns the base merged with its override."""
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None  # no stored override
        with patch.object(
            flows,
            "_base_report_doc",
            return_value={
                "schema_version": "1",
                "kind": "report",
                "id": "m/r",
                "base_report_id": "m/r",
                "metrics": ["clicks"],
            },
        ):
            merged = flows.fetch_report_override_merged("proj_a", "m/r", [], conn)
        assert merged["metrics"] == ["clicks"]
        assert merged["project_id"] == "proj_a"
