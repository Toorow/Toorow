"""« Aucun objet nomme » et « personne n a dit lesquels en nomment » sont opposes.

CE QUI A TRANCHE LE CAS, LE 2026-08-16, ET C EST UNE MESURE. `calendar_only`
derivait de `mdm_canonical_fields.object_kind`. Or `governance.md` fait venir
cette colonne **de personne** -- *"A field that qualifies an object is a
declaration somebody makes, not a value derived from a data type"* -- et la
projection de plateforme la laisse NULL par regle, pas par oubli. Compte sur une
base ou tout le vocabulaire est provisionne :

    272 champs canoniques de plateforme, 0 portant un `object_kind`

Donc `not named_objects` etait VRAI pour tout flux de toute instance.
`IdentityCandidatesPanel` prenait toujours son retour anticipe, et le panneau qui
existe pour faire epingler une identite partagee n offrait JAMAIS l epinglage. Un
ecran qui refuse toujours ne refuse rien : il est absent.

CE QUI N A PAS ETE FAIT, ET POURQUOI. Deriver un `object_kind` d un type de
donnee ou d un nom de champ aurait rendu le verdict << vrai >> -- en inventant la
decision de conception que le document ratifie refuse explicitement. Le faux zero
se dit maintenant comme une ignorance (`named_objects_state`), ce qui est la
regle que ce module s applique deja partout ailleurs : `unavailable` ne se lit
jamais comme un zero.
"""

from __future__ import annotations

import pytest
from core import datastream_workbench as workbench

_VERSION_ID = "dmv_EXAMPLE0000000000000000"


def _versions(field_names: list[str]) -> list[dict]:
    return [
        {
            "id": _VERSION_ID,
            "mapping_payload": {
                "fields": [
                    {"field_id": name, "binding": {"status": "included"}}
                    for name in field_names
                ]
            },
        }
    ]


@pytest.fixture
def stubbed(monkeypatch):
    """Only the vocabulary varies; everything else is held still on purpose."""

    def _wire(vocabulary: list[dict]):
        monkeypatch.setattr(
            "core.canonical_field_registry.list_visible_canonical_fields",
            lambda conn, *, project_id: vocabulary,
        )
        monkeypatch.setattr(workbench, "_fields_named_elsewhere", lambda *a, **k: {})
        monkeypatch.setattr(
            workbench, "_crossings_unlocked", lambda *a, **k: {"crossings": [], "count": 0}
        )

    return _wire


def _field(name: str, object_kind: str | None) -> dict:
    return {
        "id": f"mdm_{name.upper():0<26}"[:30],
        "canonical_name": name,
        "concept_kind": "dimension",
        "scope": "platform",
        "object_kind": object_kind,
    }


def test_a_vocabulary_that_qualifies_no_object_cannot_answer_the_question(stubbed):
    """LE DEFAUT MESURE : ce cas est celui de TOUTE instance aujourd hui."""
    stubbed([_field("date", None), _field("campaign_id", None)])

    answer = workbench._identity_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", _versions(["date", "campaign_id"]), _VERSION_ID
    )

    assert answer["named_objects_state"] == "undeterminable"
    # Et surtout : le refus ne se prononce PAS, donc le panneau reste ouvert.
    assert answer["calendar_only"] is False


def test_a_vocabulary_that_qualifies_objects_makes_an_absence_meaningful(stubbed):
    """Le verdict n est pas supprime, il est CONDITIONNE."""
    stubbed([_field("date", None), _field("campaign_id", "campaign")])

    answer = workbench._identity_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", _versions(["date"]), _VERSION_ID
    )

    assert answer["named_objects_state"] == "known"
    assert answer["named_objects"] == []
    # Le flux ne porte que `date`, et le vocabulaire SAIT qualifier un objet.
    assert answer["calendar_only"] is True


def test_a_datastream_that_names_an_object_is_never_calendar_only(stubbed):
    stubbed([_field("date", None), _field("campaign_id", "campaign")])

    answer = workbench._identity_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", _versions(["date", "campaign_id"]), _VERSION_ID
    )

    assert answer["named_objects"] == ["campaign"]
    assert answer["calendar_only"] is False
    assert answer["named_objects_state"] == "known"
