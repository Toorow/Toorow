"""toorow -- tests for the adaptation lifecycle: lock / replay / re-analyse (22.20/23/24).

OFFLINE: replay + re-analyse run the REAL isolated worker (subprocess); lock is
asserted against a patched create_file_source_template seam.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from core.file_source_adaptation import (
    adaptation_signature,
    check_adaptation_drift,
    lock_adaptation_template,
    reanalyse_with_adaptation,
    replay_adaptation,
)
from core.file_source_producer import ReshapeProducerError

# An adaptation that yields the required canonical fields.
_PY = (
    "def adapt(input_bytes, template):\n"
    "    rows = []\n"
    "    for i, line in enumerate(input_bytes.decode('utf-8').splitlines()):\n"
    "        if i == 0:\n"
    "            continue\n"
    "        v, c, d = line.split(',')\n"
    "        rows.append({'mdm_channel': v, 'mdm_cost': c, 'mdm_date': d})\n"
    "    return {'rows': rows, 'rejected': []}\n"
)
_PY_HASH = hashlib.sha256(_PY.encode("utf-8")).hexdigest()
_DATA = b"vendor,cost,date\nGoogle,100,2026-03-01\nMeta,200,2026-03-02"


def _locked_template(py=_PY, required=("mdm_cost", "mdm_date")):
    return {
        "content_hash": "a" * 64,  # locked (Story 22.11)
        "contract": {
            "kind": "adaptation", "class": "actual", "grain": "daily",
            "placement": {"metric": "mdm_cost", "period": "mdm_date",
                          "dimension": ["mdm_channel"]},
            "required_fields": list(required),
            "py_source": py,
            "py_content_hash": hashlib.sha256(py.encode("utf-8")).hexdigest(),
        },
    }


# ---------------------------------------------------------------------------
# 22.23 -- lock an adaptation as a versioned template
# ---------------------------------------------------------------------------


def test_lock_adaptation_calls_create_with_adaptation_kind_and_hash():
    captured = {}

    def _fake_create(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "fst_x", "version": 1, **kwargs}

    with patch("core.file_source_adaptation.create_file_source_template", _fake_create):
        lock_adaptation_template(
            object(), project_id="p", org_id="o", template_code="BESPOKE",
            py_source=_PY, placement_class="actual",
            placement={"metric": "mdm_cost", "period": "mdm_date", "dimension": ["mdm_channel"]},
            required_fields=["mdm_cost", "mdm_date"], grain="daily", created_by="u",
        )
    contract = captured["contract"]
    assert contract["kind"] == "adaptation"
    assert contract["py_source"] == _PY
    assert contract["py_content_hash"] == _PY_HASH  # hash of the locked .py
    assert contract["class"] == "actual"


def test_template_contract_rejects_mismatched_py_hash():
    from core.file_source_template import (
        FileSourceTemplateValidationError,
        validate_template_contract,
    )

    bad = {
        "kind": "adaptation", "class": "actual", "grain": "daily",
        "placement": {"metric": "mdm_cost", "period": "mdm_date"},
        "required_fields": ["mdm_cost"],
        "py_source": _PY, "py_content_hash": "b" * 64,  # wrong hash
    }
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(bad)


# ---------------------------------------------------------------------------
# 22.24 -- replay a locked adaptation, byte-identically, with drift re-gate
# ---------------------------------------------------------------------------


def test_replay_runs_locked_py_and_stamps_placement():
    result = replay_adaptation(_DATA, _locked_template())
    assert len(result.rows) == 2
    assert all(r["_placement_class"] == "actual" for r in result.rows)
    assert result.rows[0]["mdm_cost"] == "100"
    assert result.rows[0]["mdm_date"] == "2026-03-01"


def test_replay_is_byte_identical_upload_equals_email():
    template = _locked_template()
    up = adaptation_signature(_DATA, template, filename="up.csv")
    email = adaptation_signature(_DATA, template, filename="mail.csv")
    assert up == email


def test_replay_refuses_unlocked_template():
    unlocked = {"contract": _locked_template()["contract"]}  # no content_hash
    with pytest.raises(ReshapeProducerError) as exc:
        replay_adaptation(_DATA, unlocked)
    assert exc.value.code == "template_not_locked"


def test_replay_drift_missing_required_reenters_gate():
    # An adaptation that omits the required mdm_date -> drift -> re-enter the gate.
    py_missing = (
        "def adapt(input_bytes, template):\n"
        "    return {'rows': [{'mdm_cost': '100'}], 'rejected': []}\n"
    )
    with pytest.raises(ReshapeProducerError) as exc:
        replay_adaptation(_DATA, _locked_template(py=py_missing))
    assert exc.value.code == "adaptation_drift"


def test_check_adaptation_drift_flags_missing_required():
    template = _locked_template()
    ok = check_adaptation_drift(template, [{"mdm_cost": "1", "mdm_date": "2026-03-01"}])
    assert ok["status"] == "ok"
    drift = check_adaptation_drift(template, [{"mdm_cost": "1"}])
    assert drift["status"] == "needs_revalidation"
    assert drift["missing_required"] == ["mdm_date"]


# ---------------------------------------------------------------------------
# 22.20 -- re-analyse a flagged sample with a host-LLM-proposed adaptation
# ---------------------------------------------------------------------------


def test_reanalyse_passes_gate_when_required_present():
    out = reanalyse_with_adaptation(_locked_template(), _DATA, _PY)
    assert out["ok"] is True
    assert out["gate"]["passed"] is True
    assert out["preview"]["row_count"] == 2


def test_reanalyse_flags_gate_when_required_missing():
    py_missing = (
        "def adapt(input_bytes, template):\n"
        "    return {'rows': [{'mdm_cost': '100'}], 'rejected': []}\n"
    )
    out = reanalyse_with_adaptation(_locked_template(), _DATA, py_missing)
    assert out["ok"] is True
    assert out["gate"]["passed"] is False
    assert out["gate"]["missing_required"] == ["mdm_date"]


def test_reanalyse_surfaces_worker_error():
    bad = "import os\ndef adapt(i, t): return {}"
    out = reanalyse_with_adaptation(_locked_template(), _DATA, bad)
    assert out["ok"] is False
    assert out["error"]["code"] == "forbidden_import"
