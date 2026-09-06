"""AI-270 -- la seed de renommage est une PROJECTION des manifestes, pas une copie.

CE QU'ELLE EMPECHE. `dbt/seeds/connector_metric_names.csv` porte
(connecteur, nom_landé, nom_canonique) et le staging s'en sert pour que le fait
ne voie qu'un seul nom par metrique. Cette table existe DEJA ailleurs : c'est
`canonical_metric_mapping` dans chacun des 39 manifestes.

Deux copies d'un meme fait est la forme que ce depot paie le plus cher -- une
metrique renommee dans son manifeste et pas dans la seed produirait un fait qui
lit deux metriques la ou il y en a une, ce que la seed existe precisement pour
empecher. Le garde compare donc les deux a chaque passage.

IL NE VERIFIE PAS QUE LA SEED EST BELLE, il verifie qu'elle est A JOUR : la
comparaison porte sur l'ensemble derive, octet pour octet, pas sur un compte.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "generate_metric_name_map.py"
_SEED = _ROOT / "dbt" / "seeds" / "connector_metric_names.csv"


def _derive():
    sys.path.insert(0, str(_ROOT / "scripts"))
    try:
        import generate_metric_name_map as gen  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    return gen


def test_the_seed_matches_what_the_manifests_declare():
    gen = _derive()
    derived = gen.render(gen.derive())
    actual = _SEED.read_text(encoding="utf-8")

    assert derived == actual, (
        "connector_metric_names.csv no longer matches the manifests it is derived "
        "from. A metric renamed in a manifest and not here makes the fact read TWO "
        "metrics where there is one -- exactly what this seed exists to prevent.\n"
        "Run: python scripts/generate_metric_name_map.py --write"
    )


def test_it_is_the_script_that_answers_and_not_a_stale_file():
    """Le garde doit tomber quand la seed derive, pas seulement quand elle manque.

    Ecrit apres avoir vu deux gardes de ce depot rester verts sur une portee de
    ZERO. Une seed vidée ou tronquée doit faire ECHOUER la comparaison.
    """
    gen = _derive()
    rows = gen.derive()

    assert rows, "aucun renommage derive des 39 manifestes -- la derivation est cassee"
    # La seed rendue depuis un sous-ensemble ne doit PAS egaler celle rendue depuis
    # tout : sans quoi la comparaison du test precedent ne discrimine rien.
    assert gen.render(rows[:-1]) != gen.render(rows)


def test_no_identity_rename_is_carried():
    """Une paire qui ne renomme rien noierait les vingt qui font quelque chose.

    Et elle serait TROMPEUSE : la jointure du staging est un LEFT JOIN avec
    COALESCE, donc une paire absente veut deja dire << garde le nom tel qu'il a
    atterri >>. Une paire identite dirait la meme chose en plus long.
    """
    gen = _derive()
    identities = [r for r in gen.derive() if r[1] == r[2]]

    assert not identities, f"paires identite dans la seed : {identities[:5]}"


def test_the_five_measured_defects_are_covered():
    """Les cas qui ont motive la seed, nommes un par un.

    Mesures le 2026-08-16 sur les donnees REELLEMENT atterries, pas sur le code :
    ces cinq connecteurs posaient un nom brut la ou le fait attend le canonique.
    Si l'un d'eux disparait de la seed, la raison d'etre de ce fichier disparait
    avec lui sans que rien ne le dise.
    """
    gen = _derive()
    pairs = {(c, landed): canon for c, landed, canon in gen.derive()}

    for connector, landed, expected in (
        ("x-ads", "billed_charge_local_micro", "cost"),
        ("sa360", "metrics.cost_micros", "cost"),
        ("amazon-dsp", "totalCost", "cost"),
        ("adobe-analytics", "visits", "sessions"),
        ("brevo", "sent", "messages_sent"),
    ):
        assert pairs.get((connector, landed)) == expected, (
            f"{connector} n'a plus de renommage {landed!r} -> {expected!r}"
        )


@pytest.mark.skipif(not _SCRIPT.exists(), reason="script absent")
def test_the_script_reports_drift_rather_than_hiding_it():
    """Sans `--write`, il doit SORTIR EN ERREUR sur une seed perimee.

    Un generateur qui rend 0 quoi qu'il arrive ne peut pas servir de gate.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True, text=True, cwd=str(_ROOT), encoding="utf-8", errors="replace",
    )
    # La seed est a jour ici, donc rc=0 et il le DIT.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "current" in result.stdout
