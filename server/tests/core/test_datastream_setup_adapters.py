"""Read-only adapter proofs for Story 47.3."""

from __future__ import annotations

from inbound.adapters.datastream_setup import (
    observe_channel_contract,
    observe_connector_contract,
    observe_external_bigquery,
    observe_google_sheet,
    observe_staged_file,
)


class _BigQuery:
    """The typed client the adapter is given, with its two calls recorded.

    The ORDER matters and is asserted below: the estimate is taken while the
    evidence is being built, so an observation can never reach the preview step
    without the cost of that read attached to it.
    """

    def __init__(self, *, fields=None, scan_bytes=4096):
        self.calls: list[str] = []
        self._fields = (
            [
                {
                    "name": "event_date",
                    "field_id": "event_date",
                    "type": "DATE",
                    "nullable": False,
                    "mode": "REQUIRED",
                    "description": "Reporting day",
                }
            ]
            if fields is None
            else fields
        )
        self._scan_bytes = scan_bytes

    def get_table_metadata(self, object_ref: str):
        assert object_ref == "analytics.raw.events"
        self.calls.append("get_table_metadata")
        return {
            "fields": self._fields,
            "location": "EU",
            "watermark": "event_date",
            "freshness": "2026-07-29T08:00:00Z",
            "unselectable_field_count": sum(
                1 for field in self._fields if str(field.get("mode", "")).upper() == "REPEATED"
            ),
            "schema_depth_truncated": False,
        }

    def estimate_scan_bytes(self, object_ref: str):
        assert object_ref == "analytics.raw.events"
        self.calls.append("estimate_scan_bytes")
        return self._scan_bytes


class _Sheets:
    """The typed client the sheet adapter is given (Story 57.2).

    `values` is offered deliberately and must never come out the other end: the
    adapter reads the header row and nothing below it.
    """

    def __init__(self, **overrides):
        self._record = {
            "tab_state": "named",
            "tab_title": "Budget",
            "headers": ["Date", "Spend"],
            "header_state": "declared",
            "header_reason": "",
            "locale": "fr_FR",
            "row_count": 4200,
            "column_count": 2,
            "frozen_row_count": 1,
            "tabs": [
                {"object_ref": "sheet_opaque!Q1", "label": "Q1"},
                {"object_ref": "sheet_opaque!Budget", "label": "Budget"},
            ],
            "values": [["must", "not", "persist"]],
        }
        self._record.update(overrides)

    def get_sheet_metadata(self, sheet_ref: str):
        assert sheet_ref == "sheet_opaque"
        return dict(self._record)


def test_bigquery_adapter_uses_typed_read_only_metadata_without_rows() -> None:
    result = observe_external_bigquery({"object_ref": "analytics.raw.events"}, client=_BigQuery())
    assert result["adapter_ref"] == "external_bq.readonly.v1"
    assert result["safe_metadata"]["location"] == "EU"
    assert "rows" not in str(result).lower()


def test_bigquery_adapter_carries_the_scan_estimate_and_what_it_measures() -> None:
    """The cost of the read that has NOT happened yet, and the sentence for it.

    `quota_cost` is the one safe-metadata key that carries a cost, so it is the
    only place this can live -- anything else is dropped in silence by the
    normalizer, and the estimate would vanish between the adapter and the screen.
    """
    client = _BigQuery(scan_bytes=1_048_576)
    result = observe_external_bigquery({"object_ref": "analytics.raw.events"}, client=client)
    cost = result["safe_metadata"]["quota_cost"]
    assert cost["bytes_scanned_estimate"] == 1_048_576
    assert cost["unit"] == "bytes"
    assert cost["measured_by"] == "bigquery_dry_run_query"
    assert "not billed" in cost["measures"]
    assert result["coverage"]["scan_estimate"] == "available"
    assert client.calls == ["get_table_metadata", "estimate_scan_bytes"]


def test_bigquery_adapter_says_when_the_estimate_is_missing_and_never_zeroes_it() -> None:
    """No estimate is an EXCEPTION, not a free read.

    `0` here would read as "this scan costs nothing", which nobody measured. The
    named exception is what the step-5 preview refuses on, and what the screen
    can say before the click.
    """
    result = observe_external_bigquery(
        {"object_ref": "analytics.raw.events"}, client=_BigQuery(scan_bytes=None)
    )
    assert result["safe_metadata"]["quota_cost"] is None
    assert result["coverage"]["scan_estimate"] == "unavailable"
    assert [item["code"] for item in result["exceptions"]] == ["scan_estimate_unavailable"]


def test_bigquery_adapter_tells_an_empty_schema_from_a_missing_estimate() -> None:
    """Two emptinesses, two codes. Collapsing them names the wrong repair.

    An object with no readable column is an authorization question answered in
    Google Cloud. A missing estimate is a cost question answered by re-running
    discovery. They are reported apart so the screen can say which one happened.
    """
    result = observe_external_bigquery(
        {"object_ref": "analytics.raw.events"}, client=_BigQuery(fields=[], scan_bytes=99)
    )
    codes = [item["code"] for item in result["exceptions"]]
    assert codes == ["schema_unavailable"]
    assert result["coverage"]["schema"] == "unavailable"
    assert result["coverage"]["scan_estimate"] == "available"


def test_bigquery_adapter_keeps_the_dotted_path_the_mode_and_the_description() -> None:
    """What a field says INSTEAD of an example value.

    A `service.description` leaf keeps its full path, its leaf type, its BigQuery
    mode and the schema's own sentence. None of that is a row, and all of it is
    what an operator needs to decide -- the example value the plan asked for
    cannot travel here, and the masked sample belongs to step 5.
    """
    client = _BigQuery(
        fields=[
            {
                "name": "service.description",
                "field_id": "service.description",
                "type": "STRING",
                "nullable": True,
                "mode": "NULLABLE",
                "description": "Billed service name",
            }
        ]
    )
    result = observe_external_bigquery({"object_ref": "analytics.raw.events"}, client=client)
    field = result["safe_metadata"]["fields"][0]
    assert field["name"] == "service.description"
    assert field["field_id"] == "service.description"
    assert field["type"] == "STRING"
    assert field["mode"] == "NULLABLE"
    assert field["description"] == "Billed service name"


def test_bigquery_adapter_carries_a_repeated_record_through_to_the_evidence() -> None:
    """The whole point of describing a nested field: it must SURVIVE the trip.

    Emitting it from the client is half the fix; the adapter has to keep its
    mode, and the normalizer has to let `mode` through -- which it does, it is
    one of the seven keys `_safe_field` allows.
    """
    from core.datastream_setup_observations import is_container_field, normalize_adapter_evidence

    client = _BigQuery(
        fields=[
            {
                "name": "cost",
                "field_id": "cost",
                "type": "FLOAT",
                "nullable": True,
                "mode": "NULLABLE",
            },
            {
                "name": "credits",
                "field_id": "credits",
                "type": "RECORD",
                "nullable": True,
                "mode": "REPEATED",
            },
        ]
    )
    result = observe_external_bigquery({"object_ref": "analytics.raw.events"}, client=client)
    assert result["coverage"]["unselectable_fields"] == "1"
    normalized = normalize_adapter_evidence(result)
    listed = {field["name"]: field for field in normalized["safe_metadata"]["fields"]}
    assert listed["credits"]["type"] == "RECORD"
    assert listed["credits"]["mode"] == "REPEATED"
    assert is_container_field(listed["credits"]) is True
    assert is_container_field(listed["cost"]) is False


def test_file_adapter_persists_only_schema_hash_and_bounded_count() -> None:
    result = observe_staged_file(
        {"staged_asset_ref": "dsa_opaque"},
        asset_loader=lambda ref: ("daily.csv", b"date,spend\n2026-07-29,12.5\n"),
    )
    assert result["safe_metadata"]["detected_format"] == "csv"
    assert result["safe_metadata"]["staged_asset_ref"] == "dsa_opaque"
    assert "2026-07-29" not in str(result)
    assert result["safe_metadata"]["append_supported"] is False


def test_sheets_adapter_never_persists_values() -> None:
    result = observe_google_sheet({"sheet_ref": "sheet_opaque"}, client=_Sheets())
    assert result["coverage"]["values"] == "not_observed"
    assert "must" not in str(result)


def test_sheets_adapter_states_the_tab_the_header_and_the_refusal_to_type() -> None:
    """FOUR COVERAGE KEYS, and the one that matters is `types`.

    A spreadsheet declares no schema, so `unknown` on its own reads as "the read
    failed to see a type". `types: not_inferred` is what lets the screen say the
    other thing: nothing was read to guess one, and that is a refusal.
    """
    result = observe_google_sheet({"sheet_ref": "sheet_opaque"}, client=_Sheets())
    assert result["coverage"]["tab"] == "named"
    assert result["coverage"]["header"] == "declared"
    assert result["coverage"]["types"] == "not_inferred"
    assert result["coverage"]["grid"] == "available"
    assert result["exceptions"] == []


def test_sheets_adapter_reports_a_bucket_and_never_a_row_count() -> None:
    """`rowCount` is the height of the GRID, not the number of filled rows."""
    result = observe_google_sheet({"sheet_ref": "sheet_opaque"}, client=_Sheets())
    assert result["safe_metadata"]["row_count_bucket"] == "1000-9999"
    assert "4200" not in str(result["safe_metadata"])
    # No grid, no bucket -- rather than `0`, which reads as an empty sheet.
    without_grid = observe_google_sheet(
        {"sheet_ref": "sheet_opaque"}, client=_Sheets(row_count=None)
    )
    assert "row_count_bucket" not in without_grid["safe_metadata"]
    assert without_grid["coverage"]["grid"] == "unavailable"


def test_sheets_adapter_states_a_mode_it_does_not_pretend_to_know() -> None:
    """`UNKNOWN`, not `NULLABLE`: a spreadsheet declares no nullability.

    And it must not be mistaken for a container by the shared rule, which is the
    one way this value could silently make every column unselectable.
    """
    from core.datastream_setup_observations import is_container_field

    result = observe_google_sheet({"sheet_ref": "sheet_opaque"}, client=_Sheets())
    field = result["safe_metadata"]["fields"][0]
    assert field == {
        "name": "Date",
        "field_id": "Date",
        "type": "unknown",
        "nullable": True,
        "mode": "UNKNOWN",
    }
    assert is_container_field(field) is False


def test_sheets_adapter_tells_its_five_refusals_apart() -> None:
    """Five reasons a sheet describes nothing, five codes, five repairs.

    Collapsing them names the wrong repair: an unnamed tab is a reference to
    complete, a missing tab is a name to check, a duplicate is the sheet to fix,
    and an empty first row is a sheet WITHOUT a header -- not a broken sheet.
    """

    def codes(**overrides):
        result = observe_google_sheet({"sheet_ref": "sheet_opaque"}, client=_Sheets(**overrides))
        return [item["code"] for item in result["exceptions"]]

    assert codes(tab_state="unnamed", headers=[]) == ["sheet_tab_not_named"]
    assert codes(tab_state="not_found", headers=[]) == ["tab_not_found"]
    assert codes(header_state="uncertain", header_reason="duplicate_headers") == [
        "duplicate_headers"
    ]
    assert codes(header_state="uncertain", header_reason="header_row_incomplete") == [
        "header_row_incomplete"
    ]
    assert codes(header_state="uncertain", header_reason="header_row_empty", headers=[]) == [
        "sheet_headers_unavailable"
    ]


def test_sheets_adapter_carries_the_addressable_tabs_through_the_normalizer() -> None:
    """THE TAB BECOMES A CHOICE. Two keys, no value, and a stated count.

    A list of tabs is titles, and a title is not a cell -- the metadata call
    that reads it never asks for grid data. What matters here is that it
    SURVIVES the normalizer, which is where a new key silently dies.
    """
    from core.datastream_setup_observations import normalize_adapter_evidence

    result = observe_google_sheet({"sheet_ref": "sheet_opaque"}, client=_Sheets())
    normalized = normalize_adapter_evidence(result)
    assert normalized["safe_metadata"]["objects"] == [
        {"object_ref": "sheet_opaque!Q1", "label": "Q1"},
        {"object_ref": "sheet_opaque!Budget", "label": "Budget"},
    ]
    assert normalized["coverage"]["objects_observed"] == "2"
    assert normalized["coverage"]["object_list"] == "complete"


def test_a_long_tab_list_is_shortened_and_says_so_instead_of_vanishing() -> None:
    """The 57.1 repair, inherited: shorten the list, never drop the key.

    Before `objects` entered `_TRIMMABLE_LIST_KEYS`, a workbook with hundreds of
    tabs would have had the whole key thrown away, and the screen would have
    said "this workbook has no tab" -- a fabricated emptiness.
    """
    from core.datastream_setup_observations import normalize_adapter_evidence

    many = [
        {"object_ref": f"sheet_opaque!Tab {index}", "label": f"Tab {index}"}
        for index in range(260)
    ]
    result = observe_google_sheet({"sheet_ref": "sheet_opaque"}, client=_Sheets(tabs=many))
    normalized = normalize_adapter_evidence(result)
    listed = normalized["safe_metadata"]["objects"]
    assert 0 < len(listed) < 260
    assert normalized["coverage"]["objects_observed"] == "260"
    assert normalized["coverage"]["objects_listed"] == str(len(listed))
    assert normalized["coverage"]["object_list"] == "truncated"
    assert {"code": "object_list_truncated"} in normalized["exceptions"]


def test_sheets_adapter_leaves_the_field_bound_to_the_normalizer() -> None:
    """No `[:200]` here: cutting twice hides what the second cut removed.

    The normalizer owns the bound AND reports it (`fields_observed` /
    `fields_listed`), so a 260-column sheet must reach it whole.
    """
    from core.datastream_setup_observations import normalize_adapter_evidence

    wide = [f"column_{index}" for index in range(260)]
    result = observe_google_sheet({"sheet_ref": "sheet_opaque"}, client=_Sheets(headers=wide))
    assert len(result["safe_metadata"]["fields"]) == 260
    normalized = normalize_adapter_evidence(result)
    assert normalized["coverage"]["fields_observed"] == "260"
    assert normalized["coverage"]["field_list"] == "truncated"


def test_connector_adapter_projects_generic_contract_evidence() -> None:
    result = observe_connector_contract(
        {"report_ref": "daily"},
        contract={
            "reports": [
                {
                    "id": "daily",
                    "supported_grains": [["date"]],
                    "history": {"days": 90},
                    "cadence": {"modes": ["daily"]},
                    "quota_cost": {"units": 1},
                }
            ],
            "fields": [
                {"field_id": "date", "kind": "date"},
                {"field_id": "spend", "kind": "metric"},
            ],
        },
    )
    assert result["coverage"] == {"contract": "available", "report": "available"}
    assert result["safe_metadata"]["history"] == {"days": 90}


# ---------------------------------------------------------------------------
# Story 57.3 -- the channel contract. Offline by construction: the adapter takes
# its state as an argument and calls nothing. No Mailgun account exists, none is
# asked for, and no test here sends or receives anything.
# ---------------------------------------------------------------------------


def _channel_state(**overrides):
    """A verified deployment with an undeclared contract -- the ordinary start.

    `capability` carries the capability EVIDENCE a caller might hold, including
    the strings this test then proves cannot come out the other end.
    """
    state = {
        "channel": "webhook",
        "domain": "configured",
        "inbound_domain": "feeds.toorow-test.invalid",
        "address_form": None,
        "capability": "not_addressable_yet",
        "format": "not_declared",
        "template": None,
        "sender": "token_only",
        "allowed_senders": [],
        "arrival": "not_declared",
        "expected_interval_minutes": None,
    }
    state.update(overrides)
    return state


def test_channel_contract_states_its_four_clauses_distinctly() -> None:
    """Four clauses, four keys, four answers -- not one rolled-up verdict.

    They have four different repairs: a platform administrator configures the
    domain, materialization allocates the address, `Classify and map` binds the
    Template, and the operator declares the arrival. A single "incomplete" would
    send every one of them to the wrong place.
    """
    result = observe_channel_contract({"channel": "webhook"}, channel_state=_channel_state())

    assert result["adapter_ref"] == "managed_feed.channel_contract.v1"
    coverage = result["coverage"]
    assert coverage["domain"] == "configured"
    assert coverage["capability"] == "not_addressable_yet"
    assert coverage["format"] == "not_declared"
    assert coverage["sender"] == "token_only"
    assert coverage["arrival"] == "not_declared"


def test_channel_contract_without_a_delivery_is_empty_and_never_a_failure() -> None:
    """Nothing has arrived is the NORMAL state, and it carries no failure text."""
    result = observe_channel_contract({"channel": "webhook"}, channel_state=_channel_state())

    assert result["safe_metadata"]["fields"] == []
    assert result["coverage"]["delivery"] == "none"
    assert "no_delivery_received_yet" in [item["code"] for item in result["exceptions"]]
    assert "could not read" not in str(result).lower()
    assert "adapter_unavailable" not in str(result)


def test_channel_contract_takes_the_delivered_shape_and_invents_no_mode() -> None:
    """A tabular file declares no NULLABLE/REQUIRED/REPEATED, so none is stated.

    Adding a fabricated `mode: "NULLABLE"` here would make the wizard's Mode
    column read as the file's own declaration. It is not; the column stays empty.
    """
    delivery = {
        "adapter_ref": "managed_feed.received_file.readonly.v1",
        "safe_metadata": {
            "content_hash": "d" * 64,
            "schema_hash": "e" * 64,
            "detected_format": "csv",
            "fields": [{"name": "spend", "field_id": "spend", "type": "number", "nullable": True}],
            "row_count_bucket": "100-999",
            "append_supported": False,
        },
        "coverage": {"schema": "available", "rows": "bounded_count_only"},
        "exceptions": [],
    }
    result = observe_channel_contract(
        {"channel": "inbound_email"},
        channel_state=_channel_state(channel="inbound_email"),
        first_delivery=delivery,
    )

    assert result["coverage"]["delivery"] == "observed"
    assert result["safe_metadata"]["detected_format"] == "csv"
    assert result["safe_metadata"]["row_count_bucket"] == "100-999"
    assert [field.get("mode") for field in result["safe_metadata"]["fields"]] == [None]
    assert "no_delivery_received_yet" not in [item["code"] for item in result["exceptions"]]


def test_channel_contract_tells_its_emptinesses_apart() -> None:
    """A deployment with no domain is not a channel with no delivery.

    Two absences, two codes, two authorities: the first is repaired by a platform
    administrator, the second by whoever sends the first file.
    """
    unconfigured = observe_channel_contract(
        {"channel": "webhook"},
        channel_state=_channel_state(domain="not_configured", inbound_domain=None),
    )
    configured = observe_channel_contract(
        {"channel": "webhook"}, channel_state=_channel_state()
    )

    unconfigured_codes = [item["code"] for item in unconfigured["exceptions"]]
    configured_codes = [item["code"] for item in configured["exceptions"]]
    assert "inbound_domain_not_configured" in unconfigured_codes
    assert "inbound_domain_not_configured" not in configured_codes
    assert "no_delivery_received_yet" in configured_codes
    assert unconfigured_codes != configured_codes


def test_channel_contract_carries_no_capability_out_of_the_state_it_read() -> None:
    """DESCRIBING A CHANNEL MUST NOT MOVE THE THING THAT OPENS IT.

    The state a caller holds may sit next to a token, a hash or a suffix. None of
    them is a clause of the contract, so none of them is projected -- the adapter
    names what it emits instead of forwarding what it was handed.
    """
    result = observe_channel_contract(
        {"channel": "inbound_email"},
        channel_state=_channel_state(
            token="must_not_persist",
            token_hash="f" * 64,
            safe_suffix="must_not_persist",
            full_secret="ds_must_not_persist@feeds.toorow-test.invalid",
        ),
    )

    assert "must" not in str(result).lower()
    assert "f" * 64 not in str(result)


def test_channel_contract_never_invents_an_arrival_expectation() -> None:
    """`not_declared` is the answer, and `1440` is not.

    This is the screen half of the defect removed from `datastream_activation`:
    an interval nobody gave must read as absent everywhere it is read, or the
    monitor gets armed on a promise nobody made.
    """
    undeclared = observe_channel_contract(
        {"channel": "webhook"}, channel_state=_channel_state()
    )
    declared = observe_channel_contract(
        {"channel": "webhook"},
        channel_state=_channel_state(arrival="declared", expected_interval_minutes=180),
    )

    assert undeclared["coverage"]["arrival"] == "not_declared"
    assert undeclared["coverage"]["expected_interval_minutes"] == "not_declared"
    assert "1440" not in str(undeclared)
    assert declared["coverage"]["arrival"] == "declared"
    assert declared["coverage"]["expected_interval_minutes"] == "180"
    assert "arrival_expectation_not_declared" not in [
        item["code"] for item in declared["exceptions"]
    ]
