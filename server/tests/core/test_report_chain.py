"""Unit tests for server/core/report_chain.py (Story 8.9).

Tests:
  - get_report_chain: ok / no_stream / not_in_dictionary paths
  - override merge respected via flows layer
  - R6 metric_definitions + llm_commentary_guidelines passthrough
  - validation summary (ok_count, warnings)
  - _fetch_target_fields / _fetch_datastreams_by_target helpers

All DB calls are mocked via MagicMock cursors (no live Postgres required).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.report_chain import (
    _fetch_datastreams_by_target,
    _fetch_target_fields,
    get_report_chain,
)

# ---------------------------------------------------------------------------
# Helper: build a mock psycopg connection + cursor
# ---------------------------------------------------------------------------


def _make_conn(rows_by_query: dict | None = None):
    """Build a mock psycopg connection.

    rows_by_query maps a SQL fragment (substring) to a list of tuples.
    If None, returns empty results for all queries.
    """
    rows_by_query = rows_by_query or {}

    cur_mock = MagicMock()
    cur_mock.__enter__ = lambda s: s
    cur_mock.__exit__ = MagicMock(return_value=False)

    execute_calls: list[str] = []

    def fake_execute(sql, params=None):
        execute_calls.append(sql)
        # Match by SQL fragment.
        for fragment, rows in rows_by_query.items():
            if fragment in sql:
                cur_mock.fetchall.return_value = rows
                cur_mock.fetchone.return_value = rows[0] if rows else None
                # Build description from first row (list of 2-tuples (name, type))
                if rows:
                    n_cols = len(rows[0])
                    cur_mock.description = [
                        (f"col{i}", None) for i in range(n_cols)
                    ]
                else:
                    cur_mock.description = []
                return
        # Default: empty
        cur_mock.fetchall.return_value = []
        cur_mock.fetchone.return_value = None
        cur_mock.description = []

    cur_mock.execute = fake_execute

    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cur_mock
    conn_mock._execute_calls = execute_calls
    return conn_mock


# ---------------------------------------------------------------------------
# _fetch_target_fields
# ---------------------------------------------------------------------------


class TestFetchTargetFields:
    def test_empty_metrics_returns_empty(self):
        conn = _make_conn()
        result = _fetch_target_fields([], conn)
        assert result == {}

    def test_returns_dict_keyed_by_name(self):
        # cursor returns 2 rows: (name, display_name, data_type, field_kind, measure)
        rows = [
            ("clicks", "Clics", "integer", "metric", "sum"),
            ("impressions", "Impressions", "integer", "metric", "sum"),
        ]
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = rows
        cur.description = [
            ("name", None), ("display_name", None), ("data_type", None),
            ("field_kind", None), ("measure", None),
        ]
        cur.execute = MagicMock()

        conn = MagicMock()
        conn.cursor.return_value = cur

        result = _fetch_target_fields(["clicks", "impressions"], conn)
        assert "clicks" in result
        assert "impressions" in result
        assert result["clicks"]["display_name"] == "Clics"
        assert result["impressions"]["measure"] == "sum"

    def test_missing_metric_absent_from_result(self):
        # Only clicks is in DB, cost_per_click is not.
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [("clicks", "Clics", "integer", "metric", "sum")]
        cur.description = [
            ("name", None), ("display_name", None), ("data_type", None),
            ("field_kind", None), ("measure", None),
        ]
        cur.execute = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cur

        result = _fetch_target_fields(["clicks", "cost_per_click"], conn)
        assert "clicks" in result
        assert "cost_per_click" not in result


# ---------------------------------------------------------------------------
# _fetch_datastreams_by_target
# ---------------------------------------------------------------------------


class TestFetchDatastreamsByTarget:
    def test_returns_grouped_by_target(self):
        rows = [
            # (target_field, id, name, module_name, enabled, last_date, last_status)
            ("clicks", "ds_001", "Meta Ads", "meta-ads", True, "2026-07-10", "ok"),
            ("clicks", "ds_002", "GA4", "google-analytics", True, "2026-07-09", "partial"),
            ("impressions", "ds_001", "Meta Ads", "meta-ads", True, "2026-07-10", "ok"),
        ]
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = rows
        cur.execute = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cur

        result = _fetch_datastreams_by_target("proj_001", conn)

        assert "clicks" in result
        assert len(result["clicks"]) == 2
        assert result["clicks"][0]["id"] == "ds_001"
        assert result["clicks"][0]["enabled"] is True
        assert result["clicks"][0]["last_extract"]["status"] == "ok"
        assert result["clicks"][1]["name"] == "GA4"
        assert "impressions" in result
        assert len(result["impressions"]) == 1

    def test_empty_project_returns_empty(self):
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.execute = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cur

        result = _fetch_datastreams_by_target("proj_empty", conn)
        assert result == {}


# ---------------------------------------------------------------------------
# get_report_chain — integration paths
# ---------------------------------------------------------------------------


_SENTINEL = object()  # used to distinguish "not passed" from None / []


def _make_merged_doc(metrics=_SENTINEL, metric_definitions=None, llm_guidelines=None):
    return {
        "schema_version": "1",
        "kind": "report",
        "id": "google-search-console/overview_daily",
        "base_report_id": "google-search-console/overview_daily",
        "display_name": "Vue d'ensemble quotidienne",
        "metrics": (
            ["clicks", "impressions", "average_position"]
            if metrics is _SENTINEL
            else metrics
        ),
        "metric_definitions": metric_definitions,
        "llm_commentary_guidelines": llm_guidelines,
        "project_id": "proj_001",
    }


_TF_ROWS = [
    ("clicks", "Clics", "integer", "metric", "sum"),
    ("impressions", "Impressions", "integer", "metric", "sum"),
    ("average_position", "Position moyenne", "decimal", "metric", "average"),
]
_TF_DESCRIPTION = [
    ("name", None), ("display_name", None), ("data_type", None),
    ("field_kind", None), ("measure", None),
]

_DS_ROWS = [
    # (target_field, id, name, module_name, enabled, last_date, last_status)
    ("clicks", "ds_001", "GSC Stream", "google-search-console", True, "2026-07-10", "ok"),
    ("impressions", "ds_001", "GSC Stream", "google-search-console", True, "2026-07-10", "ok"),
    # average_position has NO enabled stream -> will be 'no_stream'
]


def _mock_conn_for_chain(tf_rows=_TF_ROWS, ds_rows=_DS_ROWS):
    """Build a mock conn that returns tf_rows for target_fields queries
    and ds_rows for datastream_mappings queries."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)

    call_count = [0]

    def fake_execute(sql, params=None):
        call_count[0] += 1
        if "target_fields" in sql or "ANY" in sql:
            cur.fetchall.return_value = tf_rows
            cur.description = _TF_DESCRIPTION
        elif "datastream_mappings" in sql or "datastreams" in sql:
            cur.fetchall.return_value = ds_rows
            cur.description = []
        elif "report_overrides" in sql:
            cur.fetchone.return_value = None
            cur.fetchall.return_value = []
            cur.description = []
        else:
            cur.fetchall.return_value = []
            cur.fetchone.return_value = None
            cur.description = []

    cur.execute = fake_execute
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


class TestGetReportChain:
    def test_ok_path_all_metrics_fed(self):
        """All 3 metrics have target_fields; clicks+impressions have enabled streams -> ok."""
        merged = _make_merged_doc(metrics=["clicks", "impressions"])
        conn = _mock_conn_for_chain(
            tf_rows=[
                ("clicks", "Clics", "integer", "metric", "sum"),
                ("impressions", "Impressions", "integer", "metric", "sum"),
            ],
            ds_rows=[
                ("clicks", "ds_001", "GSC", "gsc", True, "2026-07-10", "ok"),
                ("impressions", "ds_001", "GSC", "gsc", True, "2026-07-10", "ok"),
            ],
        )

        with (
            patch("core.flows._base_report_doc", return_value=merged),
            patch("core.flows._fetch_report_override", return_value=None),
            patch("core.flows._merge_report", return_value=merged),
            patch("core.report_chain._fetch_target_fields", return_value={
                "clicks": {"name": "clicks", "display_name": "Clics",
                           "data_type": "integer", "measure": "sum"},
                "impressions": {"name": "impressions", "display_name": "Impressions",
                                "data_type": "integer", "measure": "sum"},
            }),
            patch("core.report_chain._fetch_datastreams_by_target", return_value={
                "clicks": [{"id": "ds_001", "name": "GSC", "module": "gsc",
                            "enabled": True,
                            "last_extract": {"date": "2026-07-10", "status": "ok"}}],
                "impressions": [{"id": "ds_001", "name": "GSC", "module": "gsc",
                                 "enabled": True,
                            "last_extract": {"date": "2026-07-10", "status": "ok"}}],
            }),
        ):
            chain = get_report_chain("proj_001", "gsc", "overview_daily", conn)

        assert chain is not None
        assert chain["report_id"] == "gsc/overview_daily"
        metrics = {m["metric"]: m for m in chain["metrics"]}
        assert metrics["clicks"]["status"] == "ok"
        assert metrics["impressions"]["status"] == "ok"
        assert chain["validation"]["ok_count"] == 2
        assert chain["validation"]["warnings"] == []

    def test_no_stream_path(self):
        """average_position is in dictionary but has no datastream mapping."""
        merged = _make_merged_doc(metrics=["average_position"])
        conn = MagicMock()  # won't be called for data fetching (mocked below)

        with (
            patch("core.flows._base_report_doc", return_value=merged),
            patch("core.flows._fetch_report_override", return_value=None),
            patch("core.flows._merge_report", return_value=merged),
            patch("core.report_chain._fetch_target_fields", return_value={
                "average_position": {
                    "name": "average_position",
                    "display_name": "Position moyenne",
                    "data_type": "decimal",
                    "measure": "average",
                },
            }),
            patch("core.report_chain._fetch_datastreams_by_target", return_value={}),
        ):
            chain = get_report_chain("proj_001", "gsc", "overview", conn)

        assert chain is not None
        m = chain["metrics"][0]
        assert m["metric"] == "average_position"
        assert m["status"] == "no_stream"
        assert m["target_field"] is not None
        assert m["datastreams"] == []
        assert chain["validation"]["ok_count"] == 0
        assert len(chain["validation"]["warnings"]) == 1
        assert "Aucun flux actif" in chain["validation"]["warnings"][0]
        assert "average_position" in chain["validation"]["warnings"][0] or \
               "Position moyenne" in chain["validation"]["warnings"][0]

    def test_not_in_dictionary_path(self):
        """Metric 'cost_per_click' does not exist in target_fields."""
        merged = _make_merged_doc(metrics=["cost_per_click"])
        conn = MagicMock()

        with (
            patch("core.flows._base_report_doc", return_value=merged),
            patch("core.flows._fetch_report_override", return_value=None),
            patch("core.flows._merge_report", return_value=merged),
            patch("core.report_chain._fetch_target_fields", return_value={}),
            patch("core.report_chain._fetch_datastreams_by_target", return_value={}),
        ):
            chain = get_report_chain("proj_001", "gsc", "cost_report", conn)

        assert chain is not None
        m = chain["metrics"][0]
        assert m["metric"] == "cost_per_click"
        assert m["status"] == "not_in_dictionary"
        assert m["target_field"] is None
        assert m["datastreams"] == []
        assert chain["validation"]["ok_count"] == 0
        assert len(chain["validation"]["warnings"]) == 1
        assert "dictionnaire" in chain["validation"]["warnings"][0].lower()

    def test_mixed_statuses(self):
        """clicks=ok, average_position=no_stream, unknown_metric=not_in_dictionary."""
        merged = _make_merged_doc(
            metrics=["clicks", "average_position", "unknown_metric"]
        )
        conn = MagicMock()

        with (
            patch("core.flows._base_report_doc", return_value=merged),
            patch("core.flows._fetch_report_override", return_value=None),
            patch("core.flows._merge_report", return_value=merged),
            patch("core.report_chain._fetch_target_fields", return_value={
                "clicks": {"name": "clicks", "display_name": "Clics",
                           "data_type": "integer", "measure": "sum"},
                "average_position": {"name": "average_position",
                                     "display_name": "Position moyenne",
                                     "data_type": "decimal", "measure": "average"},
            }),
            patch("core.report_chain._fetch_datastreams_by_target", return_value={
                "clicks": [{"id": "ds_001", "name": "GSC", "module": "gsc",
                            "enabled": True,
                            "last_extract": {"date": "2026-07-10", "status": "ok"}}],
                # no entry for average_position
            }),
        ):
            chain = get_report_chain("proj_001", "gsc", "mixed", conn)

        assert chain is not None
        by_metric = {m["metric"]: m for m in chain["metrics"]}
        assert by_metric["clicks"]["status"] == "ok"
        assert by_metric["average_position"]["status"] == "no_stream"
        assert by_metric["unknown_metric"]["status"] == "not_in_dictionary"
        assert chain["validation"]["ok_count"] == 1
        assert len(chain["validation"]["warnings"]) == 2

    def test_override_merge_respected(self):
        """When a project override adds metric_definitions, they appear in the chain."""
        base = {
            "schema_version": "1",
            "kind": "report",
            "id": "gsc/overview",
            "base_report_id": "gsc/overview",
            "display_name": "Vue base",
            "metrics": ["clicks"],
            "metric_definitions": None,
            "llm_commentary_guidelines": None,
            "project_id": "proj_001",
        }
        override_doc = {
            "metric_definitions": {
                "clicks": {
                    "definition": "Nombre de clics organiques",
                    "unit": "clics",
                    "good_direction": "up",
                }
            },
            "llm_commentary_guidelines": "Analyse SEO en FR.",
        }
        merged = {**base,
                  "metric_definitions": override_doc["metric_definitions"],
                  "llm_commentary_guidelines": override_doc["llm_commentary_guidelines"]}
        conn = MagicMock()

        with (
            patch("core.flows._base_report_doc", return_value=base),
            patch("core.flows._fetch_report_override", return_value=override_doc),
            patch("core.flows._merge_report", return_value=merged),
            patch("core.report_chain._fetch_target_fields", return_value={
                "clicks": {"name": "clicks", "display_name": "Clics",
                           "data_type": "integer", "measure": "sum"},
            }),
            patch("core.report_chain._fetch_datastreams_by_target", return_value={
                "clicks": [{"id": "ds_001", "name": "GSC", "module": "gsc",
                            "enabled": True,
                            "last_extract": {"date": "2026-07-10", "status": "ok"}}],
            }),
        ):
            chain = get_report_chain("proj_001", "gsc", "overview", conn)

        assert chain is not None
        assert chain["llm_commentary_guidelines"] == "Analyse SEO en FR."
        assert chain["metric_definitions"] is not None
        assert "clicks" in chain["metric_definitions"]
        # Definition should be reflected in the metric entry
        m = chain["metrics"][0]
        assert m["definition"] is not None
        assert m["definition"]["unit"] == "clics"

    def test_report_not_found_returns_none(self):
        """When no base pack and no override, returns None."""
        conn = MagicMock()

        with (
            patch("core.flows._base_report_doc", return_value=None),
            patch("core.flows._fetch_report_override", return_value=None),
            patch("core.reports.find_report", return_value=None),
        ):
            chain = get_report_chain("proj_001", "unknown_module", "unknown", conn)

        assert chain is None

    def test_empty_metrics_list(self):
        """Report with empty metrics list returns chain with no entries."""
        merged = _make_merged_doc(metrics=[])
        conn = MagicMock()

        with (
            patch("core.flows._base_report_doc", return_value=merged),
            patch("core.flows._fetch_report_override", return_value=None),
            patch("core.flows._merge_report", return_value=merged),
            patch("core.report_chain._fetch_target_fields", return_value={}),
            patch("core.report_chain._fetch_datastreams_by_target", return_value={}),
        ):
            chain = get_report_chain("proj_001", "gsc", "empty", conn)

        assert chain is not None
        assert chain["metrics"] == []
        assert chain["validation"]["ok_count"] == 0
        assert chain["validation"]["warnings"] == []
