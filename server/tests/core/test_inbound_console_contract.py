"""The console's TypeScript interfaces are DERIVED from a server sample -- AI-278.

THE DEFECT THIS CLOSES, and why three corrected lines would not have closed it.

`inboundDeliveryApi.ts` declared `source_columns?: string[]`. The server has
returned a list of OBJECTS since the screen was mounted on 2026-08-08:

    [{"name": ..., "index": ..., "detected_type": ..., "null_count": ...,
      "source_label": ..., "masked_samples": [...]}]

`WorkbenchDeliveryPanel` rendered them with `.join(", ")`:

    node -e 'console.log([{name:"date"},{name:"clicks"}].join(", "))'
    -> [object Object], [object Object]

Three things that should have caught it did not:

  * `tsc --noEmit` sees a hand-written interface, not the server. A type that is
    simply FALSE is not a compile error -- it is a lie the compiler enforces.
  * The UI test stubbed `source_columns: ["date", "campaign", "clicks"]`, a
    shape the server has never produced. Green, for the wrong reason.
  * The server test asserts `[c["name"] for c in context["source_columns"]]`,
    so BOTH sides were internally consistent and disagreed with each other.

So the repair is not "fix the three lines". It is: record the shape the server
ACTUALLY serialises, in one file, and make both sides fail when they drift from
it. This file derives the shape from the real functions; the console's own test
(`WorkbenchDeliveryPanel.test.tsx`) drives the component with the SAME file, so
a console fixture can no longer describe a payload the server cannot send.

WHY A SHAPE AND NOT A RECORDED PAYLOAD. Ids, hashes and timestamps change every
run; pinning them would make this file fail for reasons that are not drift. What
is pinned is exactly what a TypeScript interface claims: the field names and
their JSON types.

ASCII-only source (AI-03).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

#: The one file both sides read. It lives with the console because the console
#: is what it constrains; the server is what fills it.
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "ui"
    / "admin"
    / "src"
    / "datastreams"
    / "workbench"
    / "__contracts__"
    / "inbound-server-shapes.json"
)

_CSV = b"date,clicks,respondent_email\n2026-01-15,10,alice@example.com\n"


def _shape(value):
    """The JSON type of *value*, recursively -- names and types, never values.

    A list collapses to the shape of its FIRST element under `__item__`: a
    contract about a list is a contract about what one entry looks like, and
    recording three identical entries would only make the file longer.
    """
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return {"__item__": _shape(value[0])} if value else {"__item__": "empty"}
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return type(value).__name__


def _store(tmp_path):
    from core.inbound_quarantine import LocalFsQuarantineStore

    return LocalFsQuarantineStore(root=str(tmp_path))


def _conn():
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("plan_v1", "map_v1", "active", "proj_EXAMPLE")
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _mapping_repair_context(tmp_path, monkeypatch) -> dict:
    """The payload `readMappingRepairContext` receives, from the real function."""
    import core.inbound_raw_imports as iri
    from core.inbound_mapping_entry import get_mapping_repair_context

    store = _store(tmp_path)
    uri = store.put(
        partition="p",
        message_id="m",
        filename="report.csv",
        data=_CSV,
        content_type="text/csv",
    ).uri
    monkeypatch.setattr(
        iri,
        "get_raw_import",
        lambda conn, **kw: {
            "raw_import_id": "inbraw_EXAMPLE",
            "datastream_id": "ds_EXAMPLE",
            "state": "REJECTED",
            "error_code": "declared_type_mismatch",
            "filename": "report.csv",
            "media_type_detected": "text/csv",
            "content_hash": "a" * 64,
            "scan_verdict": {"accepted": False},
            "quarantine_uri": uri,
        },
    )
    return get_mapping_repair_context(
        _conn(),
        raw_import_id="inbraw_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        store=store,
    )


class _RoutingCursor:
    """A cursor that answers each statement BY WHAT IT ASKS, not by its turn.

    ROUTED, NOT ORDERED, and the first version of this file shows why. A
    `side_effect` list is coupled to statement ORDER: adding the parser-failure
    query in the middle of `_read_metrics` shifted every later answer by one, a
    string reached `int(...)`, the reader caught the `ValueError` and returned
    its `unavailable` branch -- and the contract silently recorded THAT as the
    server's shape. The test still passed. A fixture that records the failure
    path while looking healthy is precisely the defect this file exists to stop.
    """

    def __init__(self):
        self._next_all: list = []
        self._next_one: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        del params
        text = " ".join(str(sql).split())
        if "FROM app.inbound_receipts" in text and "GROUP BY state" in text:
            self._next_all = [("LANDED", 3)]
        elif "FROM app.inbound_raw_imports" in text and "GROUP BY state" in text:
            self._next_all = [("LANDED", 2), ("FAILED", 1)]
        elif "repeated" in text:
            self._next_one = (1, 1)
        elif "max(created_at)" in text:
            self._next_one = ("2026-08-10T00:00:00+00:00",)
        elif "min(created_at)" in text:
            self._next_one = ("2026-08-10T00:00:00+00:00",)
        elif "percentile_cont" in text:
            self._next_one = (3, 1.5, 4.0)
        elif "error_code = ANY" in text:
            self._next_one = (2,)
        elif "managed_feed_import_ledger" in text:
            self._next_one = ("mfl_EXAMPLE", "2026-08-10T00:00:00+00:00")
        else:  # pragma: no cover -- a new statement must be routed, not guessed
            raise AssertionError(
                f"`_read_metrics` issued a statement this contract does not "
                f"route, so its answer would be a MagicMock: {text}"
            )

    def fetchall(self):
        return self._next_all

    def fetchone(self):
        return self._next_one


def _health_metrics() -> dict:
    """The `metrics` block of `/health`, from the real reader."""
    from core.inbound_health import _read_metrics

    cur = _RoutingCursor()
    conn = MagicMock()
    conn.cursor.return_value = cur
    metrics = _read_metrics(conn, datastream_id="ds_EXAMPLE")
    assert "unavailable" not in metrics, (
        "`_read_metrics` fell into its exception branch while recording the "
        "contract. The shape captured would be the FAILURE shape, and the "
        "console would be pinned to a payload the server sends only when it is "
        "broken."
    )
    return metrics


def _derived_contract(tmp_path, monkeypatch) -> dict:
    return {
        "mapping_repair_context": _shape(_mapping_repair_context(tmp_path, monkeypatch)),
        "health_metrics": _shape(_health_metrics()),
    }


def test_the_recorded_contract_matches_what_the_server_serialises(
    tmp_path, monkeypatch
):
    """The single guard. It fails on EITHER side moving without the other.

    Regenerate deliberately, and read the diff before you do:
        python -m pytest tests/core/test_inbound_console_contract.py --update-contract
    """
    derived = _derived_contract(tmp_path, monkeypatch)
    assert CONTRACT.exists(), (
        f"the console contract file is missing: {CONTRACT}. It is what stops a "
        "console fixture from describing a payload the server cannot send."
    )
    recorded = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert derived == recorded, (
        "the server's serialised shape and the recorded console contract "
        "disagree.\n\n"
        f"  server   : {json.dumps(derived, indent=2, sort_keys=True)}\n\n"
        f"  recorded : {json.dumps(recorded, indent=2, sort_keys=True)}\n\n"
        "If the server changed on purpose, update the contract file AND the "
        "TypeScript interface in `inboundDeliveryApi.ts` in the same change -- "
        "that pair going out of step is the `[object Object]` defect."
    )


def test_source_columns_carry_the_three_things_ac1_asks_for(tmp_path, monkeypatch):
    """38.16 AC1: << source columns / types / safe samples >>.

    All three were computed and serialised, and only the names were rendered.
    Asserting the SHAPE here is what lets the console test render them.
    """
    context = _mapping_repair_context(tmp_path, monkeypatch)
    columns = context["source_columns"]
    assert columns, "no column was read from the retained bytes"

    first = columns[0]
    assert isinstance(first, dict), (
        "a source column is an OBJECT. The console typed this `string[]` and "
        "rendered `[object Object], [object Object]` on every real answer."
    )
    assert first["name"] == "date"
    assert first["detected_type"], "AC1 asks for the type, and it must be read"
    assert "masked_samples" in first, "AC1 asks for safe samples"
    # The samples are MASKED before they leave the server -- a raw provider cell
    # never reaches the console, whichever door it uses.
    blob = json.dumps(context)
    assert "alice@example.com" not in blob
    assert "2026-01-15" not in blob


def test_the_typescript_interface_declares_every_recorded_field(tmp_path, monkeypatch):
    """The interface is checked against the contract, not against good intentions.

    `tsc` cannot tell that a hand-written type is false. This can: every field
    the server sends for a source column must be named in the TS interface, so
    the two cannot drift the way `string[]` did.
    """
    del tmp_path, monkeypatch
    recorded = json.loads(CONTRACT.read_text(encoding="utf-8"))
    column_fields = set(
        recorded["mapping_repair_context"]["source_columns"]["__item__"]
    )

    api = (
        CONTRACT.parents[1] / "inboundDeliveryApi.ts"
    ).read_text(encoding="utf-8")
    start = api.index("export interface MappingSourceColumn")
    declared_block = api[start : api.index("}", start)]

    missing = sorted(
        field for field in column_fields if f"{field}" not in declared_block
    )
    assert not missing, (
        "field(s) the server sends on every source column that "
        "`MappingSourceColumn` does not declare: " + ", ".join(missing)
    )
    assert "string[]" not in api.split("source_columns")[1][:80], (
        "`source_columns` is typed as a list of strings again -- that is the "
        "exact declaration that rendered `[object Object]`"
    )


@pytest.fixture(autouse=True)
def _maybe_update(tmp_path, monkeypatch):
    """Write the contract when explicitly asked, never as a side effect.

    A guard that repairs itself on every run is not a guard. Regeneration is one
    environment variable, and it is meant to be run while reading the diff:

        TOOROW_UPDATE_INBOUND_CONTRACT=1 python -m pytest \\
            tests/core/test_inbound_console_contract.py -p no:randomly

    An env var rather than a pytest flag because `pytest_addoption` only takes
    effect in `conftest.py`, and this guard must not require an edit to a file
    every other suite shares.
    """
    if os.environ.get("TOOROW_UPDATE_INBOUND_CONTRACT") == "1":
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT.write_text(
            json.dumps(_derived_contract(tmp_path, monkeypatch), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    yield
