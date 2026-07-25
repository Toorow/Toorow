from __future__ import annotations

import pytest


def test_oauth_discovery_requires_pinned_endpoints_and_pkce(oauth):
    class DiscoveryTransport:
        def get(self, url):
            assert url == oauth.WELL_KNOWN_URL
            return {
                "authorization_endpoint": oauth.AUTHORIZE_URL,
                "token_endpoint": oauth.TOKEN_URL,
                "revocation_endpoint": oauth.REVOKE_URL,
                "code_challenge_methods_supported": ["S256"],
            }

    document = oauth.discover_authorization_server(DiscoveryTransport())
    assert document["code_challenge_methods_supported"] == ["S256"]


def test_quota_headers_and_complexity_stop_further_pagination(connector):
    quota = connector._parse_rate_limit(
        {
            "API-Version": "2026-07",
            "RateLimit-Policy": "limit=5000; window=60",
            "RateLimit": "limit=5000, remaining=0, reset=12",
            "Retry-After": "12",
        }
    )
    assert quota["policy_values"] == {"limit": 5000, "window": 60}
    assert quota["rate_limit_values"]["remaining"] == 0
    assert connector.PLAN_BUDGETS["enterprise"]["concurrency"] == 250
    with pytest.raises(connector.MondayGraphQLError) as exc:
        connector.ensure_budget(quota)
    assert exc.value.code == "rate_limited"

    with pytest.raises(connector.MondayGraphQLError):
        connector.ensure_budget({"complexity": {"after": 0, "reset_in_x_seconds": 60}})


def test_subitems_are_flattened_at_their_own_snapshot_grain(connector):
    items = [
        {
            "id": "parent",
            "name": "Parent",
            "column_values": [],
            "subitems": [
                {
                    "id": "child",
                    "name": "Child",
                    "parent_item": {"id": "parent"},
                    "column_values": [],
                }
            ],
        }
    ]
    rows = connector._normalise_items("board", items)
    assert [(row["item_id"], row["parent_item_id"]) for row in rows] == [
        ("parent", ""),
        ("child", "parent"),
    ]


def test_every_graphql_surface_requests_complexity(connector):
    for query in (
        connector._DISCOVERY_QUERY,
        connector._SNAPSHOT_QUERY,
        connector._NEXT_ITEMS_QUERY,
        connector._UPDATES_QUERY,
    ):
        assert "complexity { query before after reset_in_x_seconds }" in query
