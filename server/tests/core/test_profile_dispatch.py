"""Strict manifest-declared profile dispatch (Story 12.1)."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from core.loader import dispatch_pull
from core.main import _public_profile_summaries, get_module_pull_fn, list_modules
from core.queue import _resolve_datastream_profile


def _pull():
    return "legacy"


def _declared():
    return "declared"


def _loaded_module():
    connector = SimpleNamespace(pull=_pull, pull_declared=_declared)
    manifest = {
        "source_capabilities": {
            "reports": [
                {
                    "id": "selectable",
                    "selection_mode": "exact_bundle",
                    "availability": {"status": "selectable"},
                    "dispatch": {"callable": "pull_declared"},
                },
                {
                    "id": "blocked",
                    "selection_mode": "exact_bundle",
                    "availability": {
                        "status": "unavailable",
                        "reason_code": "follow_up_required",
                        "follow_up": "12-fix-profile",
                    },
                },
            ]
        }
    }
    return SimpleNamespace(
        name="example",
        connector_module=connector,
        manifest=manifest,
        capabilities_available=True,
    )


def test_explicit_profile_uses_only_descriptor_callable():
    with patch("core.main._loaded_modules", [_loaded_module()]):
        assert get_module_pull_fn("example", "selectable") is _declared


def test_explicit_unknown_or_unavailable_profile_does_not_fall_back():
    with patch("core.main._loaded_modules", [_loaded_module()]):
        assert get_module_pull_fn("example", "unknown") is None
        assert get_module_pull_fn("example", "blocked") is None


def test_no_profile_keeps_legacy_default_pull():
    with patch("core.main._loaded_modules", [_loaded_module()]):
        assert get_module_pull_fn("example") is _pull


class _ProfileCursor:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        if self.error:
            raise self.error

    def fetchone(self):
        return self.row


class _ProfileConnection:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error

    def cursor(self):
        return _ProfileCursor(self.row, self.error)


def test_explicit_datastream_profile_lookup_never_degrades_to_legacy_default():
    assert _resolve_datastream_profile(_ProfileConnection(), None) is None
    assert _resolve_datastream_profile(_ProfileConnection(row=None), "ds-a") == ""
    assert (
        _resolve_datastream_profile(_ProfileConnection(error=RuntimeError("db down")), "ds-a") == ""
    )

def test_loader_dispatch_rejects_unknown_profile_without_calling_default():
    default_pull = Mock(return_value={"path": "default"})
    declared_pull = Mock(return_value={"path": "declared"})
    loaded = SimpleNamespace(
        name="example",
        connector_module=SimpleNamespace(
            pull=default_pull,
            pull_declared=declared_pull,
        ),
        manifest={
            "report_profiles": [
                {"id": "selectable", "extraction_path": "custom_pull"}
            ],
            "source_capabilities": {
                "reports": [
                    {
                        "id": "selectable",
                        "availability": {"status": "selectable"},
                        "dispatch": {"callable": "pull_declared"},
                    }
                ]
            },
        },
    )

    with pytest.raises(ValueError, match="unknown profile"):
        dispatch_pull(
            module_name="example",
            profile_id="unknown",
            loaded_modules=[loaded],
            connection_id="connection-a",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="project-a",
            pull_id="pull-a",
        )

    default_pull.assert_not_called()
    declared_pull.assert_not_called()


def test_public_profile_summary_preserves_public_shape_and_availability():
    manifest = {
        "report_profiles": [
            {
                "id": "daily",
                "display_name": "Daily",
                "metrics": ["spend"],
                "dimensions": ["date"],
                "extraction_path": "custom_pull",
                "verification_expected_rows_per_day": 10,
                "extraction_capabilities": {
                    "row_limit": 100,
                    "filters_supported": True,
                    "regex_filters": True,
                    "realtime": False,
                },
                "_private_note": "do not expose",
            }
        ],
        "source_capabilities": {
            "reports": [
                {
                    "id": "daily",
                    "availability": {
                        "status": "unavailable",
                        "reason_code": "configuration_required",
                        "follow_up": "story-config",
                    },
                }
            ]
        },
    }

    summary = _public_profile_summaries(manifest)[0]
    assert summary["extraction_path"] == "custom_pull"
    assert summary["verification_expected_rows_per_day"] == 10
    assert summary["extraction_capabilities"]["regex_filters"] is True
    assert summary["availability"] == {
        "status": "unavailable",
        "reason_code": "configuration_required",
        "follow_up": "story-config",
    }
    assert "_private_note" not in summary

def test_list_modules_points_to_detailed_capability_catalog():
    loaded = _loaded_module()
    loaded.manifest["report_profiles"] = [
        {
            "id": "selectable",
            "display_name": "Selectable",
            "metrics": [],
            "dimensions": [],
            "extraction_capabilities": {
                "row_limit": None,
                "filters_supported": False,
                "realtime": False,
            },
        }
    ]
    with (
        patch("core.main._loaded_modules", [loaded]),
        patch("core.main._resolve_project", return_value="project-a"),
        patch("core.db.get_connection"),
        patch("core.module_enablement.is_module_enabled", return_value=True),
    ):
        result = list_modules("project-a")

    assert result["data"]["capability_catalog"] == {
        "tool": "get_source_capabilities",
        "endpoint": "/api/source-capabilities",
        "required_scope": ["project_id", "connection_ref_id"],
    }


# ---------------------------------------------------------------------------
# Story 25.8 — loader dispatch_pull passes selection= for catalog_driven, and
# stays bit-identical (no selection= kwarg) for exact_bundle.
# ---------------------------------------------------------------------------


def _meta_manifest():
    import json as _json
    from pathlib import Path as _Path

    p = _Path(__file__).parents[2] / "modules" / "meta-ads" / "manifest.json"
    return _json.loads(p.read_text(encoding="utf-8"))


def test_dispatch_pull_catalog_driven_passes_resolved_selection():
    """dispatch_pull resolves the catalog default and passes selection= to the pull."""
    catalog_pull = Mock(return_value={"row_count": 1})
    loaded = SimpleNamespace(
        name="meta-ads",
        connector_module=SimpleNamespace(pull_catalog_daily=catalog_pull),
        manifest=_meta_manifest(),
    )
    dispatch_pull(
        module_name="meta-ads",
        profile_id="catalog_daily",
        loaded_modules=[loaded],
        connection_id="c",
        date_from="2026-07-01",
        date_to="2026-07-01",
        project_id="p",
        pull_id="pull-cat",
    )
    catalog_pull.assert_called_once()
    selection = catalog_pull.call_args.kwargs["selection"]
    assert "spend" in selection["metrics"]
    assert selection["source_fields"]["spend"] == "spend"


def test_dispatch_pull_exact_bundle_passes_no_selection_kwarg():
    """exact_bundle dispatch is bit-identical: no selection= kwarg is passed."""
    bundle_pull = Mock(return_value={"row_count": 1})
    loaded = SimpleNamespace(
        name="meta-ads",
        connector_module=SimpleNamespace(pull_campaign_daily=bundle_pull),
        manifest=_meta_manifest(),
    )
    dispatch_pull(
        module_name="meta-ads",
        profile_id="campaign_daily",
        loaded_modules=[loaded],
        connection_id="c",
        date_from="2026-07-01",
        date_to="2026-07-01",
        project_id="p",
        pull_id="pull-cd",
    )
    bundle_pull.assert_called_once()
    assert "selection" not in bundle_pull.call_args.kwargs
