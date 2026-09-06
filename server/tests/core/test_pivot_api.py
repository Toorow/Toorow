"""The pivot door refuses a bounded Result prefix as a complete matrix."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch


@contextmanager
def _connection(_identity=None):
    yield object()


def test_a_truncated_result_is_not_pivoted_or_totalled() -> None:
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    payload = {
        "content_hash": "a" * 64,
        "outcome": "success",
        "truncated": True,
        "schema": {
            "fields": [
                {"name": "k_day", "role": "dimension"},
                {"name": "m_spend", "role": "measure", "aggregation": "sum"},
            ]
        },
        "rows": [{"k_day": "2026-08-13", "m_spend": 10}],
    }
    with (
        patch("core.pivot_api._check_auth", new=AsyncMock(return_value=(True, "owner"))),
        patch("core.pivot_api._guard", return_value=("org_1", None)),
        patch("core.query_specs_api.analyze_connection", _connection),
        patch("core.pivot_api.load_result_payload", return_value=payload),
        TestClient(build_asgi_app(), raise_server_exceptions=False) as client,
    ):
        response = client.post(
            "/api/projects/proj_1/analyze/results/qr_1/pivot",
            json={"rows": ["k_day"], "columns": [], "values": ["m_spend"]},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "truncated_result_not_pivotable"
