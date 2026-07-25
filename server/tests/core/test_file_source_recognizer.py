"""toorow -- tests for confidence-scored tolerant column recognition (Story 22.17).

OFFLINE / pure: recognize renamed, reordered, other-language headers against a
template's canonical fields, with a confidence score, ignoring extra columns.
"""

from __future__ import annotations

from core.file_source_recognizer import normalize_name, recognize_columns


def _template(**over):
    contract = {
        "kind": "catalog", "class": "planned",
        "required_fields": ["mdm_net_cost", "mdm_media_date"],
        "optional_fields": ["mdm_channel"],
        "aliases": {
            "mdm_net_cost": ["net cost", "bruttokosten gesamt", "cout net"],
            "mdm_media_date": ["date", "media date", "datum", "tag"],
            "mdm_channel": ["vendor", "vermarkter", "regie", "canal"],
        },
    }
    contract.update(over)
    return {"contract": contract}


def test_normalize_folds_accents_case_and_separators():
    assert normalize_name("Coût-Net  ") == "cout net"
    assert normalize_name("BRUTTOKOSTEN_GESAMT") == "bruttokosten gesamt"


def test_recognizes_other_language_reordered_and_ignores_extras():
    # German headers, reordered, plus an extra column with no canonical meaning.
    source_columns = ["Datum", "Vermarkter", "Bruttokosten Gesamt", "Notiz"]
    result = recognize_columns(_template(), source_columns)

    assert result["mapping"]["Datum"] == "mdm_media_date"
    assert result["mapping"]["Vermarkter"] == "mdm_channel"
    assert result["mapping"]["Bruttokosten Gesamt"] == "mdm_net_cost"
    # The extra column is IGNORED (unmatched), never force-mapped.
    assert "Notiz" not in result["mapping"]
    notiz = next(f for f in result["fields"] if f["source_column"] == "Notiz")
    assert notiz["status"] == "unmatched"
    assert notiz["canonical_target"] is None


def test_every_matched_field_carries_a_confidence():
    result = recognize_columns(_template(), ["Bruttokosten Gesamt", "Datum"])
    for f in result["fields"]:
        assert 0.0 <= f["confidence"] <= 1.0
        assert "name_confidence" in f
    matched = next(f for f in result["fields"] if f["source_column"] == "Bruttokosten Gesamt")
    assert matched["status"] == "matched"
    assert matched["confidence"] > 0.7  # a strong alias hit


def test_exact_alias_scores_full_name_confidence():
    result = recognize_columns(_template(), ["net cost"])
    f = result["fields"][0]
    assert f["canonical_target"] == "mdm_net_cost"
    assert f["name_confidence"] == 1.0


def test_duplicate_target_is_flagged_ambiguous():
    # Two source columns both resolve confidently to the same canonical field.
    result = recognize_columns(
        _template(), ["Bruttokosten Gesamt", "net cost"]
    )
    codes = {a["code"] for a in result["ambiguities"]}
    assert "duplicate_target" in codes
    dup = next(a for a in result["ambiguities"] if a["code"] == "duplicate_target")
    assert dup["canonical_target"] == "mdm_net_cost"
    assert dup["source_columns"] == ["Bruttokosten Gesamt", "net cost"]


def test_confidence_fuses_profile_evidence_when_sample_given():
    # With a sample, the confidence fuses the name score with the reused
    # profile_fields physical/sample confidence.
    sample = [
        {"Bruttokosten Gesamt": "100.50", "Datum": "2026-03-01"},
        {"Bruttokosten Gesamt": "200.00", "Datum": "2026-03-02"},
        {"Bruttokosten Gesamt": "300.00", "Datum": "2026-03-03"},
    ]
    result = recognize_columns(_template(), ["Bruttokosten Gesamt", "Datum"], sample)
    cost = next(f for f in result["fields"] if f["source_column"] == "Bruttokosten Gesamt")
    assert "profile_confidence" in cost  # profile evidence attached
    assert 0.0 <= cost["confidence"] <= 1.0


def test_unrelated_headers_all_unmatched():
    result = recognize_columns(_template(), ["Foo", "Bar", "Zzz"])
    assert result["mapping"] == {}
    assert all(f["status"] == "unmatched" for f in result["fields"])
