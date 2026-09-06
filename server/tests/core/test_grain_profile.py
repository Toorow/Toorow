"""Measured cardinality for grain columns (grain_profile, 2026-09-05).

Offline: the measurer is a function, and these tests hand one in. What is
proven is the banding, the write-back into the payload, the columns chosen and
the fail-soft posture -- a failed read never invents a small number.
"""

from __future__ import annotations

from core import grain_profile


def _payload(grain, fields):
    return {
        "grain": list(grain),
        "fields": [
            {"field_id": name, "profile": {"cardinality_signal": signal}} for name, signal in fields
        ],
    }


def test_a_distinct_count_is_classed_on_the_compilers_own_bands():
    assert grain_profile.signal_for(1) == "low"
    assert grain_profile.signal_for(20) == "low"
    assert grain_profile.signal_for(21) == "medium"
    assert grain_profile.signal_for(500) == "medium"
    assert grain_profile.signal_for(9_999) == "high"
    assert grain_profile.signal_for(10_001) == "unique"


def test_only_unknown_grain_columns_are_measured_and_written_back():
    payload = _payload(
        ["channel_id", "country", "date"],
        [("channel_id", "unknown"), ("country", "unknown"), ("date", "medium"), ("views", "unknown")],
    )
    asked: list[list[str]] = []

    def measure(columns):
        asked.append(list(columns))
        return {"channel_id": 1, "country": 27}

    written = grain_profile.enrich_unknown_grain_signals(payload, measure)

    # The date already carries a signal; `views` is not a grain column.
    assert asked == [["channel_id", "country"]]
    assert written == {"channel_id": "low", "country": "medium"}
    by_id = {f["field_id"]: f["profile"]["cardinality_signal"] for f in payload["fields"]}
    assert by_id == {"channel_id": "low", "country": "medium", "date": "medium", "views": "unknown"}


def test_a_declared_domain_is_already_a_known_cardinality():
    payload = _payload(["age_group"], [("age_group", "unknown")])
    payload["fields"][0]["allowed_values"] = ["13-17", "18-24", "25-34"]

    assert grain_profile.unknown_grain_columns(payload) == []


def test_a_failed_read_leaves_every_signal_unknown():
    payload = _payload(["channel_id"], [("channel_id", "unknown")])

    def measure(_columns):
        raise RuntimeError("warehouse unreachable")

    assert grain_profile.enrich_unknown_grain_signals(payload, measure) == {}
    assert payload["fields"][0]["profile"]["cardinality_signal"] == "unknown"


def test_a_column_the_measurer_could_not_count_stays_unknown():
    payload = _payload(["channel_id", "country"], [("channel_id", "unknown"), ("country", "unknown")])

    written = grain_profile.enrich_unknown_grain_signals(payload, lambda _c: {"country": 3})

    assert written == {"country": "low"}
    by_id = {f["field_id"]: f["profile"]["cardinality_signal"] for f in payload["fields"]}
    assert by_id["channel_id"] == "unknown"


def test_a_measured_domain_is_kept_as_values_and_priced_at_its_exact_count():
    payload = _payload(["channel_id", "country"], [("channel_id", "unknown"), ("country", "unknown")])

    written = grain_profile.enrich_unknown_grain_signals(
        payload, lambda _c: {"channel_id": ["UC_one"], "country": 27}
    )

    assert written == {"channel_id": "low", "country": "medium"}
    by_id = {f["field_id"]: f["profile"] for f in payload["fields"]}
    assert by_id["channel_id"]["allowed_values"] == ["UC_one"]
    assert "allowed_values" not in by_id["country"]


def test_a_column_of_the_landing_is_counted_as_a_column_and_malformed_names_never_reach_sql(monkeypatch):
    from core import relation_shape, warehouse

    seen: list[tuple[str, list]] = []
    monkeypatch.setattr(warehouse, "_db_mode", lambda: "duckdb")
    monkeypatch.setattr(warehouse, "_duckdb_mart_prefix", lambda _p: "marts.")
    monkeypatch.setattr(relation_shape, "_columns", lambda *_a: {"channel_id", "country", "date", "views"})

    def query(sql, params):
        seen.append((sql, params))
        if sql.startswith("SELECT COUNT(DISTINCT channel_id)"):
            return [{"channel_id": 1, "country": 27}]
        if sql.startswith("SELECT DISTINCT channel_id"):
            return [{"v": "UC_one"}]
        if sql.startswith("SELECT DISTINCT country"):
            return [{"v": f"C{i}"} for i in range(27)]
        return []

    monkeypatch.setattr(warehouse, "_query_duckdb", query)

    counted = grain_profile.measure_distinct(
        "proj_EXAMPLE", "raw_video_daily", ["channel_id", "country", "bad name; DROP"]
    )

    # Small domains come back as their values; the malformed name never reaches SQL;
    # the dataset is the writer's own (`query_execution._dataset_for`).
    assert counted == {"channel_id": ["UC_one"], "country": [f"C{i}" for i in range(27)]}
    assert seen[0] == (
        "SELECT COUNT(DISTINCT channel_id) AS channel_id, COUNT(DISTINCT country) AS country "
        "FROM raw_proj_EXAMPLE.raw_video_daily",
        [],
    )
    assert all("bad name" not in sql for sql, _p in seen)


def test_a_dimension_the_landing_carries_as_breakdown_rows_is_counted_as_rows(monkeypatch):
    """`raw_youtube_breakdown` carries `country` as (breakdown_dimension, breakdown_value)
    rows, not as a column; asking for the column voided the whole measurement."""
    from core import relation_shape, warehouse

    seen: list[tuple[str, list]] = []
    monkeypatch.setattr(warehouse, "_db_mode", lambda: "bigquery")
    monkeypatch.setattr(
        relation_shape, "_columns",
        lambda *_a: {"channel_id", "date", "breakdown_dimension", "breakdown_value", "views"},
    )

    def query(sql, params):
        seen.append((sql, params))
        if sql.startswith("SELECT COUNT(DISTINCT channel_id)"):
            return [{"channel_id": 1, "date": 41}]
        if sql.startswith("SELECT DISTINCT channel_id"):
            return [{"v": "UC_one"}]
        if sql.startswith("SELECT DISTINCT date"):
            return [{"v": f"2026-08-{i:02d}"} for i in range(1, 42)]
        if params == ["country"] and "DISTINCT breakdown_value" in sql and "COUNT" not in sql:
            return [{"v": f"C{i}"} for i in range(27)]
        if params == ["device_type"]:
            return []
        return []

    monkeypatch.setattr(warehouse, "_query_bigquery", query)

    counted = grain_profile.measure_distinct(
        "proj_EXAMPLE", "raw_youtube_breakdown", ["channel_id", "country", "date", "device_type"]
    )

    # The real columns in one statement; each breakdown dimension in its own; a
    # dimension with no row stays uncounted rather than "zero distinct".
    assert set(counted) == {"channel_id", "date", "country"}
    assert counted["channel_id"] == ["UC_one"]
    assert len(counted["date"]) == 41 and len(counted["country"]) == 27
    assert seen[0][0].startswith(
        "SELECT COUNT(DISTINCT channel_id) AS channel_id, COUNT(DISTINCT date) AS date "
        "FROM `raw_proj_EXAMPLE`.raw_youtube_breakdown"
    )
    breakdown_calls = [(s, p) for s, p in seen if p]
    assert [p for _s, p in breakdown_calls] == [["country"], ["device_type"]]
    assert all("WHERE breakdown_dimension = @p0" in s for s, _p in breakdown_calls)


def test_a_shared_identity_travels_under_its_canonical_target_across_sources():
    """Search Console `page` and GA4 `pagePath` both name the canonical `page`."""
    from core.mdm_common_keys import shared_identity_name

    assert shared_identity_name({"field_id": "pagePath", "binding": {"canonical_target": "page"}}) == "page"
    assert shared_identity_name({"field_id": "page", "binding": {"canonical_target": "page"}}) == "page"
    assert shared_identity_name({"field_id": "channel_id", "binding": {}}) == "channel_id"
