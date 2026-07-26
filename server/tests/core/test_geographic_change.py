from __future__ import annotations

import copy

import pytest
from core.geographic_change import (
    GeographicPreviewBlocked,
    GeographicPreviewStale,
    build_datastream_impact,
    dependency_fingerprint,
    posture_diff,
    validate_confirmation_dependencies,
)
from core.geographic_reporting import LOCAL_MARKETS, GeographicPosture


def _capabilities(*, country: bool = True, fingerprint_marker: str = "v1") -> dict:
    dimensions = ["date"]
    fields = [
        {
            "field_id": "date",
            "canonical_target": "date",
            "kind": "dimension",
        }
    ]
    grains = [["date"]]
    if country:
        dimensions.append("geo_country")
        fields.append(
            {
                "field_id": "geo_country",
                "canonical_target": "country",
                "kind": "dimension",
            }
        )
        grains.append(["date", "geo_country"])
    return {
        "contract_version": "1",
        "fingerprint_marker": fingerprint_marker,
        "module": {"name": "example"},
        "connection_ref_id": "conn-1",
        "fields": fields,
        "reports": [
            {
                "id": "daily",
                "metrics": ["cost"],
                "dimensions": dimensions,
                "supported_grains": grains,
                "quota_cost": {"read_points": 2, "unit": "request"},
                "pagination": {"row_limit": 1000, "max_pages": 3},
            }
        ],
    }


def _intent() -> dict:
    return {
        "contract_version": "1",
        "source": {
            "kind": "connector_pull",
            "writer_kind": "toorow",
            "connection_ref_id": "conn-1",
            "report_id": "daily",
            "selection": {
                "selection_mode": "subset",
                "metrics": ["cost"],
                "dimensions": ["date"],
                "grain": ["date"],
                "filters": [],
            },
        },
        "historical": {"start": None, "end_exclusive": None},
        "schedule": {
            "mode": "daily",
            "interval_minutes": 1440,
            "timezone": "UTC",
            "watermark": {"kind": "date_window", "delay_minutes": 60},
            "late_arrival": {"lookback_minutes": 1440},
            "retry": {
                "max_attempts": 3,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 300,
            },
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 3},
        },
        "destination": {"policy": "managed_raw"},
    }


def _datastream(intent: dict | None = None) -> dict:
    return {
        "id": "ds-1",
        "name": "Daily",
        "module_name": "example",
        "connection_ref_id": "conn-1",
        "current_plan_version_id": "plan-1",
        "current_plan_content_hash": "a" * 64,
        "current_capability_fingerprint": "b" * 64,
        "date_window_days": 30,
        "intent": intent or _intent(),
    }


def test_global_to_local_impact_is_country_complete_and_requires_candidate_backfill() -> None:
    source = _datastream()
    original = copy.deepcopy(source)
    impact = build_datastream_impact(
        source,
        GeographicPosture(),
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR", "DE")),
        _capabilities(),
        earliest_country_date=None,
    )

    assert source == original
    assert impact["country_capability"] is True
    assert impact["compatibility"] == "compatible"
    assert impact["backfill_required"] is True
    assert impact["coverage_state"] == "partial"
    assert impact["max_provider_backfill_days"] is None
    assert impact["backfill_bound_evidence"] == "unavailable"
    assert impact["estimated_cost"]["read_points"] == 2
    assert impact["estimated_volume"]["upper_bound_rows"] == 3_000
    assert impact["blocking_gaps"] == []
    assert impact["backfill_contract"]["publication_path"].endswith("/executions")


def test_global_history_is_not_mislabeled_as_retained_country_history() -> None:
    impact = build_datastream_impact(
        _datastream(),
        GeographicPosture(),
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
        _capabilities(),
        earliest_country_date="2025-01-01",
    )

    assert impact["earliest_available_country_date"] is None
    assert impact["backfill_required"] is True
    assert impact["coverage_state"] == "partial"


def test_missing_country_capability_is_visible_and_blocks_confirmation() -> None:
    impact = build_datastream_impact(
        _datastream(),
        GeographicPosture(),
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
        _capabilities(country=False),
        earliest_country_date=None,
    )

    assert impact["compatibility"] == "blocked"
    assert impact["coverage_state"] == "unavailable"
    assert impact["blocking_gaps"][0]["code"] == "geographic_country_unavailable"
    with pytest.raises(GeographicPreviewBlocked):
        validate_confirmation_dependencies(
            {"dependency_fingerprint": "same", "impact": {"datastreams": [impact]}},
            "same",
        )


def test_tracked_set_only_change_reclassifies_without_backfill() -> None:
    current_intent = _intent()
    current_intent["geographic"] = {
        "mode": LOCAL_MARKETS,
        "country_codes": ["FR"],
        "posture_fingerprint": "c" * 64,
        "compilation_status": "country_complete",
        "effective_country_field": "geo_country",
        "impact": {
            "grain_before": ["date"],
            "grain_after": ["date", "geo_country"],
            "added_dimensions": ["geo_country"],
            "tracked_country_count": 1,
            "quota_cost": {},
            "cardinality_estimate": "provider_country_cardinality",
        },
    }
    impact = build_datastream_impact(
        _datastream(current_intent),
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("DE", "FR")),
        _capabilities(),
        earliest_country_date="2026-01-01",
    )

    assert impact["backfill_required"] is False
    assert impact["provider_pull_required"] is False
    assert impact["coverage_state"] == "complete"


def test_local_to_global_preserves_history_and_needs_no_backfill() -> None:
    impact = build_datastream_impact(
        _datastream(),
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
        GeographicPosture(),
        _capabilities(),
        earliest_country_date="2025-01-01",
    )

    assert impact["backfill_required"] is False
    assert impact["coverage_state"] == "consolidated"
    assert impact["retained_history_preserved"] is True


def test_dependency_fingerprint_is_order_stable_and_detects_capability_drift() -> None:
    posture = GeographicPosture()
    one = dependency_fingerprint(
        posture,
        [
            {
                "datastream_id": "b",
                "plan_version_id": "2",
                "plan_content_hash": "x",
                "capability_fingerprint": "y",
            },
            {
                "datastream_id": "a",
                "plan_version_id": "1",
                "plan_content_hash": "x",
                "capability_fingerprint": "y",
            },
        ],
    )
    two = dependency_fingerprint(
        posture,
        [
            {
                "datastream_id": "a",
                "plan_version_id": "1",
                "plan_content_hash": "x",
                "capability_fingerprint": "y",
            },
            {
                "datastream_id": "b",
                "plan_version_id": "2",
                "plan_content_hash": "x",
                "capability_fingerprint": "y",
            },
        ],
    )
    drifted = dependency_fingerprint(
        posture,
        [
            {
                "datastream_id": "a",
                "plan_version_id": "1",
                "plan_content_hash": "x",
                "capability_fingerprint": "z",
            },
            {
                "datastream_id": "b",
                "plan_version_id": "2",
                "plan_content_hash": "x",
                "capability_fingerprint": "y",
            },
        ],
    )

    assert one == two
    assert drifted != one
    with pytest.raises(GeographicPreviewStale):
        validate_confirmation_dependencies(
            {"dependency_fingerprint": one, "impact": {"datastreams": []}}, drifted
        )


def test_dependency_fingerprint_detects_mapping_source_schema_drift() -> None:
    posture = GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",))
    base = {
        "datastream_id": "ds-1",
        "plan_version_id": "plan-1",
        "plan_content_hash": "a" * 64,
        "capability_fingerprint": "b" * 64,
        "mapping_version_id": "map-1",
        "source_schema_hash": "c" * 64,
    }
    changed = {**base, "source_schema_hash": "d" * 64}

    assert dependency_fingerprint(posture, [base]) != dependency_fingerprint(posture, [changed])


def test_posture_diff_is_canonical() -> None:
    result = posture_diff(
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("DE", "FR")),
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR", "US")),
    )
    # Story 37.8: the recorded diff is a MARKET diff, code deltas included.
    assert result["previous_mode"] == LOCAL_MARKETS
    assert result["new_mode"] == LOCAL_MARKETS
    assert result["added_country_codes"] == ["US"]
    assert result["removed_country_codes"] == ["DE"]
    assert [market["id"] for market in result["added_markets"]] == ["US"]
    assert [market["id"] for market in result["removed_markets"]] == ["DE"]
    assert result["relabelled_markets"] == []
    assert result["recomposed_markets"] == []
    assert result["moved_country_codes"] == []


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.connection.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        if not self.connection.results:
            return None
        return self.connection.results.pop(0)

    def fetchall(self):
        if not self.connection.results:
            return []
        return self.connection.results.pop(0)


class _Connection:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_preview_persists_only_governance_artifact(monkeypatch) -> None:
    from core import geographic_change

    previous = GeographicPosture()
    target = GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",))
    impact_row = build_datastream_impact(
        _datastream(), previous, target, _capabilities(), earliest_country_date=None
    )
    monkeypatch.setattr(
        geographic_change,
        "_current_dependencies",
        lambda *_args: (
            previous,
            [_datastream()],
            {"ds-1": _capabilities()},
            "d" * 64,
        ),
    )
    inserted = (
        "gprev-1",
        "project-1",
        previous.as_dict(),
        target.as_dict(),
        "d" * 64,
        {"datastreams": [impact_row]},
        "pending",
        None,
        "actor",
        None,
        None,
    )
    # Results are popped in call order: idempotency lookup, then the Story 37.9
    # market used-by registry read (no binding declared here), then the INSERT.
    conn = _Connection([None, [], inserted])

    result = geographic_change.create_geographic_change_preview(
        project_id="project-1",
        target=target,
        identity="actor",
        idempotency_key="preview-key",
        conn=conn,
        loaded_modules=[],
    )

    assert result["id"] == "gprev-1"
    assert conn.commits == 1
    sql = " ".join(statement for statement, _params in conn.calls).lower()
    assert "insert into app.geographic_change_previews" in sql
    assert "update app.project_preferences" not in sql
    assert "update app.datastreams" not in sql


def test_confirmation_uses_one_commit_and_caller_owned_plan_transactions(
    monkeypatch,
) -> None:
    from core import geographic_change

    previous = GeographicPosture()
    target = GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",))
    datastream = _datastream()
    impact = build_datastream_impact(
        datastream, previous, target, _capabilities(), earliest_country_date=None
    )
    preview = {
        "id": "gprev-1",
        "project_id": "project-1",
        "previous_posture": previous.as_dict(),
        "target_posture": target.as_dict(),
        "dependency_fingerprint": "d" * 64,
        "impact": {"datastreams": [impact]},
        "status": "pending",
        "confirmation": None,
    }
    monkeypatch.setattr(geographic_change, "_fetch_preview_for_update", lambda *_args: preview)
    monkeypatch.setattr(
        geographic_change,
        "_current_dependencies",
        lambda *_args: (
            previous,
            [datastream],
            {"ds-1": _capabilities()},
            "d" * 64,
        ),
    )
    saves = []

    def fake_save(**kwargs):
        saves.append(kwargs)
        return {"id": "plan-2", "version_number": 2}

    monkeypatch.setattr("core.datastream_intents.save_datastream_intent", fake_save)
    persisted = []
    monkeypatch.setattr(
        "core.geographic_reporting.persist_project_geographic_posture",
        lambda project_id, posture, conn: persisted.append((project_id, posture, conn)),
    )
    audits = []
    monkeypatch.setattr(
        "core.audit.insert_audit_row", lambda *args, **kwargs: audits.append(kwargs)
    )
    conn = _Connection([])

    result = geographic_change.confirm_geographic_change(
        preview_id="gprev-1",
        project_id="project-1",
        identity="actor",
        backfill_decision="request",
        conn=conn,
        loaded_modules=[],
    )

    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert saves[0]["commit"] is False
    assert saves[0]["geographic_posture"] == target
    assert persisted == [("project-1", target, conn)]
    assert audits[0]["metadata"]["backfill_decision"] == "request"
    assert result["plan_versions"][0]["plan_version_id"] == "plan-2"
    assert result["backfill_actions"][0]["candidate_publication_required"] is True


def test_live_coverage_follows_current_published_plan_pointer() -> None:
    from core.geographic_reporting import fetch_project_geographic_coverage

    posture = GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",))
    rows = [
        (
            "ds-complete",
            "Complete",
            "plan-1",
            {"geographic": {"compilation_status": "country_complete"}},
            "plan-1",
            {"geographic": {"compilation_status": "country_complete"}},
        ),
        (
            "ds-partial",
            "Partial",
            "plan-2",
            {"geographic": {"compilation_status": "country_complete"}},
            "plan-1",
            {"geographic": {"compilation_status": "global"}},
        ),
    ]
    coverage = fetch_project_geographic_coverage("project-1", posture, _Connection([rows]))
    assert coverage["status"] == "partial"
    assert [item["state"] for item in coverage["datastreams"]] == ["complete", "partial"]

    rows[1] = (*rows[1][:-1], {"geographic": {"compilation_status": "country_complete"}})
    retained = fetch_project_geographic_coverage("project-1", posture, _Connection([rows]))
    assert retained["status"] == "complete"


def test_global_coverage_is_consolidated_without_query() -> None:
    from core.geographic_reporting import fetch_project_geographic_coverage

    conn = _Connection([])
    coverage = fetch_project_geographic_coverage("project-1", GeographicPosture(), conn)
    assert coverage == {"status": "consolidated", "datastreams": []}
    assert conn.calls == []


# ---------------------------------------------------------------------------
# Story 37.8 -- regrouping is a semantic reclassification, never a backfill
# ---------------------------------------------------------------------------


def _market_posture(*markets: tuple[str, str, tuple[str, ...]]) -> GeographicPosture:
    from core.geographic_reporting import Market

    return GeographicPosture(
        mode=LOCAL_MARKETS,
        markets=tuple(
            Market(id=market_id, label=label, country_codes=codes)
            for market_id, label, codes in markets
        ),
    )


def test_regrouping_only_is_detected_when_the_tracked_set_is_unchanged() -> None:
    from core.geographic_change import is_regrouping_only

    split = _market_posture(("fr", "France", ("FR",)), ("mc", "Monaco", ("MC",)))
    merged = _market_posture(("fr", "France", ("FR", "MC")))
    widened = _market_posture(("fr", "France", ("FR", "MC")), ("de", "Germany", ("DE",)))

    assert is_regrouping_only(split, merged) is True
    assert is_regrouping_only(split, split) is False
    assert is_regrouping_only(split, widened) is False
    assert is_regrouping_only(GeographicPosture(), merged) is False


def test_regrouping_impact_requires_no_backfill_and_no_provider_pull() -> None:
    split = _market_posture(("fr", "France", ("FR",)), ("mc", "Monaco", ("MC",)))
    merged = _market_posture(("fr", "France", ("FR", "MC")))

    impact = build_datastream_impact(
        _datastream(),
        split,
        merged,
        _capabilities(),
        # No retained country history at all -- a widening would demand a backfill;
        # a pure regroup must not, because nothing new has to be extracted.
        earliest_country_date=None,
        reclassification_only=True,
    )

    assert impact["reclassification_only"] is True
    assert impact["backfill_required"] is False
    assert impact["provider_pull_required"] is False
    assert impact["raw_rewrite_required"] is False
    assert impact["retained_history_preserved"] is True


def test_posture_diff_records_market_regrouping_not_only_codes() -> None:
    split = _market_posture(("fr", "France", ("FR",)), ("mc", "Monaco", ("MC",)))
    merged = _market_posture(("fr", "France + Monaco", ("FR", "MC")))

    diff = posture_diff(split, merged)

    assert diff["added_country_codes"] == []
    assert diff["removed_country_codes"] == []
    assert [item["id"] for item in diff["removed_markets"]] == ["mc"]
    assert diff["relabelled_markets"] == [
        {"id": "fr", "previous_label": "France", "new_label": "France + Monaco"}
    ]
    assert diff["recomposed_markets"] == [
        {
            "id": "fr",
            "label": "France + Monaco",
            "added_country_codes": ["MC"],
            "removed_country_codes": [],
        }
    ]
