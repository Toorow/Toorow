"""Une garde n'interdit que ce qu'elle EMPIRE.

MESURE, 2026-08-13. Le flux « audience by age and gender » ne pouvait etre
modifie en RIEN : toute preparation recompile la projection entiere et la refuse
pour un depassement de cardinalite preexistant -- y compris le changement qui
reparerait le flux. Epingler une identite partagee, geste qui n'ajoute pas une
ligne au scan, etait refuse par le meme mur. Un cul-de-sac qui ne se repare que
par la suppression.

« Ce changement est-il valide » n'est pas « ce plan est-il executable » : le
second est une propriete du FLUX, le premier une propriete du GESTE.
"""

from __future__ import annotations

from core.datastream_change import _what_got_worse


class _Cursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _sql, _params=None):
        return None

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)


#  Les empreintes ont la forme que le schema du plan exige : 64 hexadecimaux.
#  Une fixture invalide ferait echouer la compilation de l'ETAT ANTERIEUR, donc
#  passer par le repli « rien a comparer » -- et le test aurait prouve le repli
#  en croyant prouver la regle.
_HASH = "a" * 64


def _mapping_row(payload):
    return ("dmap_before", "dsp_before", _HASH, _HASH, True, payload)


def _payload(grain, signals):
    return {
        "grain": list(grain),
        "fields": [
            {
                "field_id": name,
                "physical_type": "string",
                "profile": {"cardinality_signal": signal},
                "binding": {"status": "confirmed"},
                "suggestion": {"semantic_role": "dimension", "aggregation": "none"},
            }
            for name, signal in signals.items()
        ],
    }


BEFORE = _payload(("a", "b"), {"a": "unique", "b": "high"})


def test_a_change_that_worsens_nothing_is_not_refused():
    """Le flux depassait deja : ce n'est pas ce changement qui l'a fait."""
    conn = _Conn(_mapping_row(BEFORE))
    #  EXACTEMENT ce que l'etat anterieur porte deja : deux depassements sur les
    #  deux memes colonnes, et les memes chiffres. Le flux depassait avant ce
    #  geste, il depasse apres, et ce n'est pas ce geste qui l'a fait.
    after = {
        "issues": [
            {"code": "cardinality_over_limit", "field_ids": ["a", "b"]},
            {"code": "scan_over_limit", "field_ids": ["a", "b"]},
        ],
        "estimate": {
            "estimated_grain_cardinality": 1_000_000_000,
            "estimated_scan_bytes": 64_000_000_000,
        },
    }
    worse = _what_got_worse(conn, datastream_id="ds", project_id="proj", after=after)
    assert worse == []


def test_a_new_finding_is_a_degradation_and_is_NAMED():
    conn = _Conn(_mapping_row(BEFORE))
    after = {
        "issues": [
            {"code": "cardinality_over_limit", "field_ids": ["a"]},
            {"code": "non_additive_measure", "field_ids": ["rate"]},
        ],
        "estimate": {"estimated_grain_cardinality": 1, "estimated_scan_bytes": 1},
    }

    worse = _what_got_worse(conn, datastream_id="ds", project_id="proj", after=after)

    assert any(line.startswith("non_additive_measure appears on rate") for line in worse)


def test_an_estimate_that_RISES_is_a_degradation_and_carries_both_figures():
    conn = _Conn(_mapping_row(BEFORE))
    worse = _what_got_worse(
        conn,
        datastream_id="ds",
        project_id="proj",
        after={"issues": [], "estimate": {"estimated_grain_cardinality": 10**15}},
    )

    #  Les DEUX chiffres, pas seulement le nouveau : « ca depasse » ne dit pas
    #  de combien le geste a pousse.
    assert any(line.startswith("grain rows rise from") and "1000000000000000" in line
               for line in worse)


def test_an_unreadable_before_never_blocks_a_repair():
    """Refuser une reparation faute d'avoir pu lire l'etat anterieur reinstaurerait
    exactement le cul-de-sac que cette fonction retire."""
    worse = _what_got_worse(
        _Conn(None), datastream_id="ds", project_id="proj", after={"issues": [], "estimate": {}}
    )

    assert worse == []
