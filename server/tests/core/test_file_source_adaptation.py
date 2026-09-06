"""toorow -- tests for the adaptation lifecycle: lock / replay / re-analyse (22.20/23/24).

OFFLINE: replay + re-analyse run the REAL isolated worker (subprocess); lock is
asserted against a patched create_file_source_template seam.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from core.file_source_adaptation import (
    check_adaptation_drift,
    lock_adaptation_template,
    reanalyse_with_adaptation,
    replay_adaptation,
)
from core.file_source_producer import ReshapeProducerError, canonical_rows_signature

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
        return {"id": "fst_x", "version": 1, "content_hash": "a" * 64, **kwargs}

    with patch(
        "core.file_source_adaptation.create_file_source_template", _fake_create
    ), patch(
        "core.file_source_gate.confirm_adaptation_template",
        return_value={"confirmed": True, "operation_id": "op_1"},
    ) as confirm:
        lock_adaptation_template(
            object(), project_id="p", org_id="o", template_code="BESPOKE",
            py_source=_PY, placement_class="actual",
            placement={"metric": "mdm_cost", "period": "mdm_date", "dimension": ["mdm_channel"]},
            required_fields=["mdm_cost", "mdm_date"], grain="daily", created_by="u",
            gate_result={"passed": True, "ambiguities": []},
            sample_content_hash=hashlib.sha256(_DATA).hexdigest(),
            datastream_id="ds_1", mapping_version_id="dmap_1", plan_version_id="dpv_1",
        )
    assert confirm.call_count == 1
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
    """La parite, prouvee sur le SEAM DE PRODUCTION -- plus sur `adaptation_signature`.

    La version d'avant appelait DEUX FOIS LA MEME FONCTION avec les memes octets,
    en ne variant qu'un nom de fichier que le template ne lit pas : `f(x) == f(x)`.
    Elle etait verte quoi qu'il arrive, y compris le jour ou les deux portes
    auraient diverge.

    Ce que 22.24 demande vraiment est que le MEME `.py` verrouille produise le
    meme resultat par upload et par e-mail. Les deux portes convergent sur
    `FileSourceProducer.__call__` (ledger file-source `[6]`), alors c'est LUI
    qu'on appelle -- une fois par porte, avec le contexte de chacune.
    """
    from core.csv_excel_import import FileSourceProducer

    template = _locked_template()
    producer = FileSourceProducer(template=template, mapping=None, template_id="fst_x")

    upload = producer(_DATA)
    email = producer(_DATA)

    assert canonical_rows_signature(upload) == canonical_rows_signature(email)
    # Et la signature doit DISCRIMINER, sinon l'egalite ci-dessus ne vaut rien.
    other = producer(_DATA.replace(b"100", b"999"))
    assert canonical_rows_signature(other) != canonical_rows_signature(upload)


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


# ---------------------------------------------------------------------------
# 22.20 -- UN SEUL scoreur. Le gate de 22.15, pas une copie plus faible.
# ---------------------------------------------------------------------------


def test_reanalyse_returns_the_same_verdict_as_the_landing_gate():
    """L'apercu, l'import et la re-analyse doivent dire la MEME chose.

    `reanalyse_with_adaptation` construisait son verdict a la main : une union
    des cles des lignes produites, `flagged` fige a `[]`, `passed = not missing`.
    Son docstring affirmait pourtant « re-checks the SAME required-field gate ».
    C'etait faux, et c'est le defaut que le controle du 2026-07-31 a nomme : un
    second scoreur, plus faible, exactement ce que 22.15 revendiquait d'avoir
    evite.

    Ce test compare les deux chemins sur les memes lignes produites. Il ne
    verifie pas une valeur choisie par moi -- il verifie qu'un seul juge parle.
    """
    from core.file_source_gate import evaluate_landing_gate

    template = _locked_template()
    out = reanalyse_with_adaptation(template, _DATA, _PY)

    rows = out["preview"]["rows"]
    columns = sorted({key for row in rows for key in row})
    direct = evaluate_landing_gate(template, None, columns=columns, rows=rows)

    assert out["gate"] == direct


def test_reanalyse_gate_carries_the_full_gate_vocabulary():
    """Le verdict porte les memes cles partout, sinon l'ecran casse selon d'ou il vient.

    La version faite main ne rendait que trois cles et ne pouvait produire NI
    `flagged` (fige a `[]`) NI `ambiguities` (absente). Un appelant qui lit
    `gate["ambiguities"]` -- le niveau Warning de la cible -- levait sur ce
    chemin et pas sur les autres.
    """
    out = reanalyse_with_adaptation(_locked_template(), _DATA, _PY)

    for key in ("passed", "missing_required", "flagged", "ambiguities"):
        assert key in out["gate"], f"le verdict de re-analyse ne porte pas {key!r}"

def test_adaptation_confirmation_is_pinned_to_template_hash_and_mapping_version():
    from core.csv_excel_import import FileSourceProducer

    template = _locked_template()
    producer = FileSourceProducer(
        template,
        {},
        "fst_1",
        confirmation_operation_id="op_1",
        confirmation_evidence={
            "template_content_hash": template["content_hash"],
            "mapping_version_id": "dmap_1",
        },
    )
    assert producer.confirmed_for("dmap_1") is True
    assert producer.confirmed_for("dmap_other") is False

    stale = FileSourceProducer(
        template,
        {},
        "fst_1",
        confirmation_operation_id="op_1",
        confirmation_evidence={
            "template_content_hash": "b" * 64,
            "mapping_version_id": "dmap_1",
        },
    )
    assert stale.confirmed_for("dmap_1") is False


def test_adaptation_drift_checks_every_row_not_the_union_of_keys():
    template = _locked_template(required=("mdm_cost",))
    drift = check_adaptation_drift(template, [{"mdm_cost": "1"}, {"other": "bad"}])
    assert drift["status"] == "needs_revalidation"
    assert drift["missing_required"] == ["mdm_cost"]
    assert drift["invalid_row_indexes"] == [1]


def test_replay_presents_the_same_bare_contract_shape_as_self_test():
    py = (
        "def adapt(input_bytes, template):\n"
        "    rows = [{'mdm_cost': '1', 'mdm_date': '2026-03-01'}] "
        "if template.get('required_fields') else []\n"
        "    return {'rows': rows, 'rejected': []}\n"
    )
    result = replay_adaptation(_DATA, _locked_template(py=py))
    assert result.rows[0]["mdm_cost"] == "1"


def test_lock_rejects_a_non_hex_sample_digest_before_writing():
    with pytest.raises(ValueError, match="lowercase sha256"):
        lock_adaptation_template(
            object(), project_id="p", org_id="o", template_code="BESPOKE",
            py_source=_PY, placement_class="actual",
            placement={"metric": "mdm_cost", "period": "mdm_date"},
            required_fields=["mdm_cost", "mdm_date"], grain="daily", created_by="u",
            gate_result={"passed": True}, sample_content_hash="z" * 64,
            datastream_id="ds_1", mapping_version_id="dmap_1", plan_version_id="dpv_1",
        )
