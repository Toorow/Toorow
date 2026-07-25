from __future__ import annotations

import inspect

import pytest


class Response:
    def __init__(self, payload, headers=None, status_code=200):
        self._payload = payload
        self.headers = headers or {"API-Version": "2026-07"}
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return next(self.responses)


def test_graphql_pins_and_verifies_api_version(connector):
    client = Client([Response({"data": {"me": {"id": "1"}}})])
    data, quota = connector.graphql_request(client, "token", "query { me { id } }")
    assert data == {"me": {"id": "1"}}
    assert client.requests[0][1]["headers"]["API-Version"] == "2026-07"
    assert quota["api_version"] == "2026-07"
    with pytest.raises(connector.ApiVersionMismatch):
        connector.graphql_request(
            Client([Response({"data": {}}, headers={"API-Version": "2026-04"})]),
            "token",
            "query { me { id } }",
        )


def test_graphql_http_200_errors_are_typed_and_quota_is_parsed(connector):
    payload = {
        "errors": [
            {
                "message": "Complexity budget exhausted",
                "extensions": {"code": "COMPLEXITY_BUDGET_EXHAUSTED", "retry_in_seconds": 12},
            }
        ]
    }
    response = Response(
        payload,
        headers={"API-Version": "2026-07", "RateLimit": "limit=5000, remaining=0, reset=12"},
    )
    with pytest.raises(connector.MondayGraphQLError) as exc:
        connector.graphql_request(Client([response]), "token", "query { boards { id } }")
    assert exc.value.code == "rate_limited"
    assert exc.value.retry_after == 12


def test_discovery_tolerates_missing_main_workspace_and_keeps_board_ids_opaque(connector):
    payload = {
        "data": {
            "me": {"account": {"id": "a1", "name": "Acme"}},
            "workspaces": [{"id": "w2", "name": "Product"}],
            "boards": [{"id": "9007199254740993", "name": "Roadmap", "workspace_id": None}],
        }
    }
    accounts = connector.discover_accounts(
        "cxn", _token="token", _client=Client([Response(payload)])
    )
    assert accounts[0]["account_id"] == "a1"
    assert accounts[0]["boards"][0]["id"] == "9007199254740993"
    assert accounts[0]["boards"][0]["workspace_id"] is None


def test_items_page_cursor_preserves_unknown_typed_column_json(connector):
    first = {
        "data": {
            "boards": [
                {
                    "id": "b1",
                    "name": "Board",
                    "columns": [
                        {
                            "id": "mystery",
                            "title": "Mystery",
                            "type": "future_type",
                            "settings_str": "{}",
                        }
                    ],
                    "groups": [{"id": "g1", "title": "Group"}],
                    "items_page": {
                        "cursor": "next",
                        "items": [
                            {
                                "id": "i1",
                                "name": "One",
                                "group": {"id": "g1"},
                                "parent_item": None,
                                "updated_at": "2026-07-22T10:00:00Z",
                                "column_values": [
                                    {
                                        "id": "mystery",
                                        "type": "future_type",
                                        "text": "x",
                                        "value": '{"x":1}',
                                    }
                                ],
                            }
                        ],
                    },
                }
            ]
        }
    }
    second = {"data": {"next_items_page": {"cursor": None, "items": []}}}
    rows, schema = connector.fetch_board_snapshot(
        Client([Response(first), Response(second)]), "token", "b1"
    )
    assert len(rows) == 1
    assert rows[0]["column_values"][0]["raw_value"] == {"x": 1}
    assert rows[0]["column_values"][0]["known_type"] is False
    assert schema["fingerprint"]


def test_dispatch_callables_are_synchronous_queue_contract(connector):
    kwargs = dict(
        connection_id="cxn",
        date_from="2026-07-01",
        date_to="2026-07-22",
        project_id="p",
        pull_id="pull",
    )
    for name in ("pull_board_snapshot", "pull_board_events"):
        fn = getattr(connector, name)
        assert not inspect.iscoroutinefunction(fn)
        inspect.signature(fn).bind(**kwargs)
