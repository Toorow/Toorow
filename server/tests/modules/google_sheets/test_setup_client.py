"""The typed setup client, pinned offline (Story 57.2).

There is no Google account for this story and there will not be one, so a double
stands where `httpx.Client` stands. What these tests prove is OUR half of the
contract: that a tab is not a workbook, that exactly ONE value range is requested
and it is one line long, that `includeGridData` is never asked for, that the
header row is reported as declared / assumed / uncertain rather than asserted,
and that no type is produced. What the real API answers is the provider's half,
and a double supposes it.

The pattern is copied from `server/tests/modules/bigquery/test_setup_client.py`
(57.1): the module is importable, so the monkeypatch lands on the real symbol.

Path note: the story names `server/tests/modules/google-sheets/`. A hyphen is not
a legal package name, and the sibling suite for this connector already lives at
`server/tests/modules/google_sheets/`, which is where this file sits.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

_MODULE = (
    Path(__file__).resolve().parents[3] / "modules" / "google-sheets" / "connector.py"
)


def _load_connector():
    spec = importlib.util.spec_from_file_location("google_sheets_setup_connector", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


connector = _load_connector()
SheetsSetupReader = connector.SheetsSetupReader

# A workbook with two tabs. The second one is the trap: a client that answers
# with `sheets[0]` passes every other assertion in this file.
_WORKBOOK = {
    "properties": {"title": "Media plan", "locale": "fr_FR", "timeZone": "Europe/Paris"},
    "sheets": [
        {
            "properties": {
                "sheetId": 0,
                "title": "Q1",
                "index": 0,
                "sheetType": "GRID",
                "gridProperties": {"rowCount": 200, "columnCount": 5, "frozenRowCount": 0},
            }
        },
        {
            "properties": {
                "sheetId": 1,
                "title": "Budget",
                "index": 1,
                "sheetType": "GRID",
                "gridProperties": {"rowCount": 4200, "columnCount": 3, "frozenRowCount": 1},
            }
        },
    ],
}

# Row 1 is a header; rows 2 and beyond are VALUES, and none of them may travel.
_TAB_VALUES = {
    "'Budget'!1:1": [["Date", "Spend"]],
    "'Q1'!1:1": [["Week", "Impressions"]],
}


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _RecordingClient:
    """Every URL asked for, recorded. That record IS the proof of this story."""

    requests: list[str] = []

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, headers=None, params=None):
        recorded = url if not params else f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
        _RecordingClient.requests.append(recorded)
        if "/values/" in url:
            sheet_range = url.split("/values/", 1)[1]
            # The double offers rows 2..N deliberately: a client that widens its
            # range would take them, and the assertions below would see them.
            rows = list(_TAB_VALUES.get(sheet_range, []))
            rows.append(["must", "not", "persist"])
            return _Response({"range": sheet_range, "values": rows})
        return _Response(_WORKBOOK)


@pytest.fixture()
def recorded(monkeypatch):
    _RecordingClient.requests = []
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    monkeypatch.setattr(
        connector, "SHEETS_API_BASE", "https://sheets.invalid/v4", raising=True
    )

    class _Token:
        @staticmethod
        def get_fresh_token(_connection_id, provider=None):
            return "test-token-not-a-real-grant"

    import sys

    import core

    # BOTH, and the second is the one that bites: once another test in the same
    # session has imported the real submodule, `from core import nango_client`
    # reads the ATTRIBUTE on the package and never consults `sys.modules`. With
    # only the first line this file passed alone and failed in a full run.
    monkeypatch.setitem(sys.modules, "core.nango_client", _Token)
    monkeypatch.setattr(core, "nango_client", _Token, raising=False)
    return _RecordingClient.requests


def _value_requests(recorded):
    return [url for url in recorded if "/values/" in url]


def test_a_tab_is_not_a_workbook(recorded):
    """THE ASSERTION THIS STORY EXISTS FOR: `Budget` is read, never `Q1`.

    A workbook reference with no tab used to be handed straight to the client,
    which would have answered with the first tab -- a choice nobody made, and a
    four-tab workbook would then describe four different schemas depending on
    their order.
    """
    metadata = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Budget")
    assert metadata["tab_state"] == "named"
    assert metadata["headers"] == ["Date", "Spend"]
    assert "Week" not in metadata["headers"]
    # The siblings are listed too, so changing tab is a choice, not a retype.
    assert [tab["object_ref"] for tab in metadata["tabs"]] == ["SHEET_ID!Q1", "SHEET_ID!Budget"]
    assert metadata["row_count"] == 4200
    assert metadata["column_count"] == 3
    assert metadata["locale"] == "fr_FR"


def test_exactly_one_value_range_is_asked_for_and_it_is_one_line(recorded):
    """DESCRIBING IS NOT PULLING, and this is the assertion that bites.

    Widen the range to `A:Z`, or ask a second one, and this test goes red. It is
    the only thing standing between "read the structure" and "read the sheet".
    """
    SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Budget")
    assert _value_requests(recorded) == ["https://sheets.invalid/v4/spreadsheets/SHEET_ID/values/'Budget'!1:1"]


def test_no_request_ever_asks_for_grid_data(recorded):
    """`includeGridData` is what repatriates cells. It is never sent."""
    SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Budget")
    assert recorded, "the describe made no request at all"
    assert not any("includeGridData" in url for url in recorded)
    assert any("fields=properties.title" in url for url in recorded)


def test_a_reference_with_no_tab_lists_the_tabs_and_reads_no_value(recorded):
    """An incomplete designation is stated, WITH the choice that repairs it.

    Listing tabs reads titles from the metadata call, never a cell -- so the
    boundary is untouched: zero value ranges are requested on this path.
    """
    metadata = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID")
    assert metadata["tab_state"] == "unnamed"
    assert metadata["headers"] == []
    assert metadata["tabs"] == [
        {"object_ref": "SHEET_ID!Q1", "label": "Q1"},
        {"object_ref": "SHEET_ID!Budget", "label": "Budget"},
    ]
    assert _value_requests(recorded) == []


def test_a_tab_the_workbook_does_not_carry_reads_no_value(recorded):
    """The workbook WAS read -- access works -- and no value range follows."""
    metadata = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Forecast")
    assert metadata["tab_state"] == "not_found"
    assert metadata["headers"] == []
    # The tabs it DOES carry are listed beside the refusal, so the repair is a
    # choice rather than a second guess at a name.
    assert [tab["label"] for tab in metadata["tabs"]] == ["Q1", "Budget"]
    assert _value_requests(recorded) == []


def test_a_workbook_with_no_id_at_all_makes_no_request(recorded):
    """Nothing named, nothing asked -- not even the metadata call."""
    metadata = SheetsSetupReader("conn-test").get_sheet_metadata("")
    assert metadata["tab_state"] == "unnamed"
    assert metadata["tabs"] == []
    assert recorded == []


def test_a_chart_sheet_is_not_offered_as_a_tab(recorded, monkeypatch):
    """A tab with no grid has no header row; offering it leads nowhere."""
    charted = {
        "properties": _WORKBOOK["properties"],
        "sheets": _WORKBOOK["sheets"]
        + [{"properties": {"sheetId": 2, "title": "Chart", "sheetType": "OBJECT"}}],
    }
    monkeypatch.setattr(connector, "_fetch_sheet_metadata", lambda *a, **k: charted)
    metadata = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID")
    assert [tab["label"] for tab in metadata["tabs"]] == ["Q1", "Budget"]


def test_the_header_row_says_how_much_it_is_a_fact(recorded, monkeypatch):
    """Declared, assumed, uncertain -- and the reason when it is uncertain."""
    declared = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Budget")
    assert declared["header_state"] == "declared"
    assert declared["frozen_row_count"] == 1

    # `Q1` freezes nothing, so row 1 is an assumption and the screen says so.
    assumed = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Q1")
    assert assumed["header_state"] == "assumed"

    monkeypatch.setitem(_TAB_VALUES, "'Q1'!1:1", [["Date", "Date"]])
    duplicated = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Q1")
    assert duplicated["header_state"] == "uncertain"
    assert duplicated["header_reason"] == "duplicate_headers"

    monkeypatch.setitem(_TAB_VALUES, "'Q1'!1:1", [["Date", "", "Spend"]])
    holed = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Q1")
    assert holed["header_state"] == "uncertain"
    assert holed["header_reason"] == "header_row_incomplete"
    # NO COLUMN NAME IS FABRICATED for the empty cell: no `Column B`, no `col_2`.
    assert holed["headers"] == ["Date", "Spend"]

    # An empty row 1 -- with data rows below it, which the double always adds.
    # Only row 1 is ever read, so the sheet still reads as headerless.
    monkeypatch.setitem(_TAB_VALUES, "'Q1'!1:1", [[]])
    bare = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Q1")
    assert bare["header_state"] == "uncertain"
    assert bare["header_reason"] == "header_row_empty"


def test_the_client_states_no_type_and_carries_no_value(recorded):
    """No inference, and nothing out of row 2 -- the double offers both."""
    metadata = SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Budget")
    assert "type" not in metadata
    assert "types" not in metadata
    assert "must" not in str(metadata)
    assert "persist" not in str(metadata)


def test_a_rate_limit_is_the_connector_s_own_error(recorded, monkeypatch):
    """The taxonomy is `_fetch_sheet_values`'s, so the worker breaker catches it."""
    from core.quota import RateLimitError

    class _Limited(_RecordingClient):
        def get(self, url, headers=None, params=None):
            _RecordingClient.requests.append(url)
            response = _Response({}, status_code=429)
            response.headers = {"Retry-After": "30"}
            return response

    monkeypatch.setattr(httpx, "Client", _Limited)
    with pytest.raises(RateLimitError) as raised:
        SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Budget")
    assert raised.value.platform == "google-sheets"
    assert raised.value.retry_after == 30


def test_a_missing_workbook_is_not_found_and_not_an_empty_sheet(recorded, monkeypatch):
    class _Missing(_RecordingClient):
        def get(self, url, headers=None, params=None):
            _RecordingClient.requests.append(url)
            return _Response({}, status_code=404)

    monkeypatch.setattr(httpx, "Client", _Missing)
    with pytest.raises(FileNotFoundError):
        SheetsSetupReader("conn-test").get_sheet_metadata("SHEET_ID!Budget")
