"""toorow -- tests for multi-variant ingestion via a declared discriminator (22.18).

OFFLINE / pure. Proves an FR file and a DE file with DIFFERENT column names land
in ONE datastream: both recognize to the SAME canonical fields (via the 22.17
recognizer's multilingual aliases), the discriminator value lands as a
market/country dimension on every row, and an undetectable discriminator is
FLAGGED for confirmation rather than mislabeled.
"""

from __future__ import annotations

import pytest
from core.file_source_producer import (
    evaluate_variant_discriminator,
    produce,
    stamp_placement,
)
from core.file_source_recognizer import recognize_columns

# A template accepting FR + DE variants: aliases cover both languages; a filename
# discriminator promotes the country code to the mdm_market dimension.
_TEMPLATE = {"contract": {
    "kind": "catalog", "class": "actual",
    "required_fields": ["mdm_net_cost", "mdm_media_date"],
    "optional_fields": ["mdm_channel"],
    "aliases": {
        "mdm_net_cost": ["cout net", "net cost", "bruttokosten gesamt", "kosten"],
        "mdm_media_date": ["date", "media date", "datum", "tag"],
        "mdm_channel": ["regie", "vendor", "vermarkter", "canal"],
    },
    "discriminator": {"dimension": "mdm_market", "source": "filename",
                      "pattern": r"_(?P<value>[A-Z]{2})_"},
}}


def _csv(rows):
    return "\n".join(",".join(r) for r in rows).encode("utf-8")


def test_fr_and_de_variants_map_to_same_canonical_fields():
    fr_cols = ["Date", "Régie", "Coût net"]
    de_cols = ["Datum", "Vermarkter", "Bruttokosten Gesamt"]
    fr = recognize_columns(_TEMPLATE, fr_cols)
    de = recognize_columns(_TEMPLATE, de_cols)

    # Different column names, SAME canonical targets.
    assert set(fr["mapping"].values()) == set(de["mapping"].values())
    assert fr["mapping"]["Coût net"] == "mdm_net_cost"
    assert de["mapping"]["Bruttokosten Gesamt"] == "mdm_net_cost"
    assert fr["mapping"]["Date"] == "mdm_media_date"
    assert de["mapping"]["Datum"] == "mdm_media_date"


def test_discriminator_value_lands_on_every_row_per_variant():
    fr_data = _csv([["Date", "Régie", "Coût net"], ["2026-03-01", "Google", "100"]])
    de_data = _csv([["Datum", "Vermarkter", "Bruttokosten Gesamt"],
                    ["2026-03-01", "Google", "200"]])

    fr = stamp_placement(
        produce(fr_data, _TEMPLATE, {"Régie": "mdm_channel", "Coût net": "mdm_net_cost",
                                     "Date": "mdm_media_date"}),
        _TEMPLATE, filename="plan_FR_2026.csv",
    )
    de = stamp_placement(
        produce(de_data, _TEMPLATE, {"Vermarkter": "mdm_channel",
                                     "Bruttokosten Gesamt": "mdm_net_cost",
                                     "Datum": "mdm_media_date"}),
        _TEMPLATE, filename="plan_DE_2026.csv",
    )
    assert all(r["mdm_market"] == "FR" for r in fr.rows)
    assert all(r["mdm_market"] == "DE" for r in de.rows)
    # Both variants carry the same canonical measure + class.
    assert all("mdm_net_cost" in r and r["_placement_class"] == "actual" for r in fr.rows + de.rows)


def test_detectable_filename_discriminator():
    out = evaluate_variant_discriminator(_TEMPLATE, filename="plan_DE_2026.csv")
    assert out["detected"] is True
    assert out["flagged"] is False
    assert out["dimension"] == "mdm_market"
    assert out["value"] == "DE"


def test_undetectable_discriminator_flags_for_confirmation():
    # The filename carries no country code -> undetectable -> FLAG, not mislabel.
    out = evaluate_variant_discriminator(_TEMPLATE, filename="plan_2026.csv")
    assert out["detected"] is False
    assert out["flagged"] is True
    assert out["reason"] == "undetectable_discriminator"
    assert out["value"] is None  # never a fabricated / mislabeled value


def test_cell_discriminator_from_metadata_variant():
    template = {"contract": {
        "kind": "catalog", "class": "planned",
        "required_fields": ["mdm_net_cost"],
        "discriminator": {"dimension": "mdm_market", "source": "cell", "cell": "Market"},
    }}
    detected = evaluate_variant_discriminator(template, metadata={"Market": "FR"})
    assert detected == {"dimension": "mdm_market", "value": "FR", "detected": True,
                        "flagged": False, "reason": None}
    missing = evaluate_variant_discriminator(template, metadata={"Other": "x"})
    assert missing["flagged"] is True


def test_no_discriminator_is_not_a_variant_datastream():
    template = {"contract": {"kind": "catalog", "class": "planned",
                             "required_fields": ["mdm_net_cost"]}}
    out = evaluate_variant_discriminator(template, filename="anything.csv")
    assert out["flagged"] is False
    assert out["dimension"] is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
