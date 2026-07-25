from __future__ import annotations

import duckdb


def test_snapshot_supersedes_board_state_without_duplicates(connector, monkeypatch):
    from core import warehouse_write

    connection = duckdb.connect(":memory:")

    class NonClosingConnection:
        def execute(self, *args, **kwargs):
            return connection.execute(*args, **kwargs)

        def executemany(self, *args, **kwargs):
            return connection.executemany(*args, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(
        warehouse_write,
        "open_raw_writer",
        lambda _path, project_id=None: NonClosingConnection(),
    )
    schema = {
        "board_id": "b1",
        "fingerprint": "schema-v1",
        "columns": [{"id": "status", "type": "status", "known_type": True}],
    }
    first = [
        {
            "board_id": "b1",
            "item_id": "i1",
            "item_name": "Before",
            "group_id": "g1",
            "parent_item_id": "",
            "updated_at": "2026-07-21T10:00:00Z",
            "column_values": [{"id": "status", "text": "Before", "raw_value": {"index": 0}}],
        }
    ]
    second = [
        dict(
            first[0],
            item_name="After",
            updated_at="2026-07-22T10:00:00Z",
            column_values=[{"id": "status", "text": "After", "raw_value": {"index": 1}}],
        )
    ]
    connector._land_snapshot(
        first, schema, pull_id="p1", project_id="project", duckdb_path=":memory:"
    )
    connector._land_snapshot(
        second, schema, pull_id="p2", project_id="project", duckdb_path=":memory:"
    )
    raw_count = connection.execute("select count(*) from raw_monday_board_snapshot").fetchone()[0]
    rows = connection.execute(
        """select item_name, pull_id from raw_monday_board_snapshot
        qualify row_number() over (
          partition by project_id, board_id, item_id, column_id order by pull_id desc
        ) = 1"""
    ).fetchall()
    connection.close()
    assert raw_count == 2
    assert rows == [("After", "p2")]


def test_runtime_schema_fingerprint_detects_column_drift(connector):
    before = connector.build_board_catalog(
        {"id": "b1", "columns": [{"id": "status", "title": "Status", "type": "status"}]}
    )
    after = connector.build_board_catalog(
        {
            "id": "b1",
            "columns": [
                {"id": "status", "title": "Status", "type": "status"},
                {"id": "owner", "title": "Owner", "type": "people"},
            ],
        }
    )
    assert before["fingerprint"] != after["fingerprint"]


def test_webhook_challenge_secret_and_retry_deduplication(connector):
    assert connector.handle_webhook({"challenge": "abc"}) == {"challenge": "abc"}
    seen = set()
    payload = {"event": {"type": "update_column_value", "pulseId": 42, "value": {"x": 1}}}
    accepted = connector.handle_webhook(
        payload, expected_secret="secret", supplied_secret="secret", seen_ids=seen
    )
    duplicate = connector.handle_webhook(
        payload, expected_secret="secret", supplied_secret="secret", seen_ids=seen
    )
    assert accepted["duplicate"] is False
    assert duplicate == {"duplicate": True, "event_id": accepted["event_id"]}
