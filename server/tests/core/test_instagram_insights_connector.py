from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest


def _load_connector():
    path = Path(__file__).resolve().parents[2] / "modules" / "instagram-insights" / "connector.py"
    spec = importlib.util.spec_from_file_location("instagram_insights_behavior", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


connector = _load_connector()


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://graph.instagram.com/test"),
    )


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        return None


def test_discovery_returns_the_opaque_professional_account_id():
    client = StubClient(
        [_response(200, {"id": "1784", "username": "toorow", "account_type": "BUSINESS"})]
    )
    accounts = connector.discover_accounts("connection", _client=client, _token_value="secret")
    assert accounts[0]["id"] == "1784"
    assert accounts[0]["label"] == "@toorow"


def test_account_daily_keeps_unavailable_metrics_absent():
    today = datetime.now(UTC).date().isoformat()
    point = {"value": 12, "end_time": f"{today}T08:00:00+0000"}
    client = StubClient([_response(200, {"data": [{"name": "reach", "values": [point]}]})])
    rows = connector.fetch_account_daily(client, "secret", "1784", today, today)
    assert rows[0]["account_reach"] == 12
    assert "account_profile_views" not in rows[0]


def test_media_performance_excludes_stories_and_keeps_lifetime_values():
    client = StubClient(
        [
            _response(
                200,
                {
                    "data": [
                        {
                            "id": "post-1",
                            "timestamp": "2026-07-29T18:30:00+0000",
                            "media_type": "VIDEO",
                            "media_product_type": "REELS",
                        },
                        {
                            "id": "story-1",
                            "timestamp": "2026-07-29T19:00:00+0000",
                            "media_type": "IMAGE",
                            "media_product_type": "STORY",
                        },
                    ]
                },
            ),
            _response(
                200,
                {
                    "data": [
                        {"name": "shares", "values": [{"value": 7}]},
                        {"name": "comments", "values": [{"value": 3}]},
                    ]
                },
            ),
        ]
    )
    rows = connector.fetch_media_performance(client, "secret", "1784", "2026-07-29", "2026-07-29")
    assert [(row["media_id"], row["media_shares"], row["media_comments"]) for row in rows] == [
        ("post-1", 7, 3)
    ]
    assert len(client.calls) == 2


def test_meta_subcode_463_is_auth_revoked():
    with pytest.raises(connector.pull_errors.AuthRevokedError):
        connector._raise_response(_response(400, {"error": {"code": 190, "error_subcode": 463}}))


def test_missing_selected_account_is_typed():
    with pytest.raises(connector.InstagramAccountNotSelectedError):
        connector._require_account(None)
