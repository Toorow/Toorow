"""Un echec reparable doit dire OU aller -- et le vocabulaire n'existe qu'une fois.

CE QUI A ETE RECONCILIE LE 2026-08-01, et pourquoi ce n'etait pas un arbitrage.
`datastream-workbench-and-wizard.md:107` -- document ratifie -- ecrit :

    << Authentication repair links to `Sources`; schema/mapping drift links to
       `Mapping`; policy failures link to the owning Project/Governance object. >>

et son tableau d'ecarts ajoute, pour l'acces manquant :

    << Select a usable Source Account; hand off to `Sources` and resume when
       access is missing. >>

La cible nomme donc TROIS destinations. Le livre en implementait une :
`_ALLOWED_USER_ACTIONS` valait `{"reconnect"}` et coercait tout le reste a None.
Un ecart entre l'ecrit et le livre est une reconciliation a faire, pas une
decision a demander -- et le symptome etait muet : une action legitime coercee a
None laisse l'ecran sans bouton, exactement comme un echec irreparable.

CE QUE CE FICHIER TIENT, et qu'aucun test voisin ne pouvait tenir :

  1. le vocabulaire est ecrit UNE fois (`pull_errors.USER_ACTIONS`) et lu, jamais
     retranscrit -- une seconde copie vieillit d'un cote en silence ;
  2. toute action qu'un connecteur emet est dans ce vocabulaire, donc arrive
     jusqu'a l'ecran au lieu d'etre effacee en chemin ;
  3. les deux familles restent DISTINGUABLES : une derive de catalogue n'a pas
     d'action (personne ne la repare depuis l'ecran), un defaut de configuration
     en a une. C'est ce qui garde pur le signal `pull_invalid_request_drift`.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from core import pull_errors
from core.datastream_diagnosis import _ALLOWED_USER_ACTIONS

MODULES_DIR = pathlib.Path(__file__).resolve().parents[2] / "modules"


def test_the_diagnosis_reads_the_vocabulary_it_does_not_keep_a_copy():
    """L'allowlist du diagnostic EST celle de la taxonomie, a l'identite pres."""
    assert _ALLOWED_USER_ACTIONS == frozenset(pull_errors.USER_ACTIONS)
    assert pull_errors.RECONNECT in _ALLOWED_USER_ACTIONS, (
        "reconnect ne doit jamais disparaitre : c'est la seule action que le livre "
        "connaissait, et des pulls en base portent deja cette valeur"
    )


def test_every_action_a_connector_emits_survives_the_allowlist():
    """Une action emise hors vocabulaire est effacee a l'affichage, en silence.

    C'est le defaut que le garde attrape : le connecteur croit avoir dit ou aller,
    `datastream_diagnosis` coerce a None (ligne 308), et l'ecran est muet. Le
    balayage lit les 38 connecteurs a la source plutot que d'importer -- une
    valeur ecrite en dur passerait aussi bien qu'une constante.
    """
    emitted: dict[str, str] = {}
    for path in sorted(MODULES_DIR.glob("*/connector.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign):
                    targets = [stmt.target]
                else:
                    targets = getattr(stmt, "targets", [])
                if not any(getattr(t, "id", "") == "user_action" for t in targets):
                    continue
                value = stmt.value
                if isinstance(value, ast.Constant) and value.value is None:
                    continue  # << aucune action >> est une reponse legitime
                if isinstance(value, ast.Attribute):
                    emitted[f"{path.parent.name}.{node.name}"] = getattr(
                        pull_errors, value.attr, f"<inconnu:{value.attr}>"
                    )
                elif isinstance(value, ast.Constant):
                    emitted[f"{path.parent.name}.{node.name}"] = value.value

    assert emitted, "aucune action emise par aucun connecteur -- l'instrument est casse"
    unknown = {k: v for k, v in emitted.items() if v not in _ALLOWED_USER_ACTIONS}
    assert not unknown, (
        "ces actions seront coercees a None par datastream_diagnosis, donc invisibles "
        f"a l'ecran alors que le connecteur croit les avoir dites : {unknown}"
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, pull_errors.INVALID_REQUEST),
        (405, pull_errors.INVALID_REQUEST),
        (422, pull_errors.INVALID_REQUEST),
        (425, pull_errors.PROVIDER_TRANSIENT),
        (500, pull_errors.PROVIDER_TRANSIENT),
    ],
)
def test_generic_http_semantics_are_carried_by_core(status, expected):
    """405, 422 et 425 n'ont aucun sens propre a un fournisseur.

    Ils etaient redeclares module par module (amazon-ads, klaviyo) faute que core
    les connaisse -- et un module qui n'y avait pas pense les laissait
    `unclassified`, donc rejoues jusqu'au dead_letter alors que rejouer une
    methode interdite ne peut rien donner d'autre.
    """
    assert pull_errors.classify_http_error(status).error_class == expected


def test_404_is_deliberately_left_to_the_modules():
    """Le 404 n'a PAS de sens generique, et la mesure le prouve.

    Les sept `_STATUS_OVERRIDES` du depot le partagent trois contre trois :
    amazon-ads / doubleverify / thetradedesk lisent << la ressource n'existe pas >>,
    google-business-profile / strava / youtube-analytics lisent << tu n'as pas
    l'acces >>. Les deux sont justes pour leur API, et surtout elles n'appellent
    pas la meme ACTION. Le trancher dans core imposerait la mauvaise a la moitie
    de la flotte -- ce test existe pour que personne ne << finisse le travail >>
    en croyant reparer un oubli.
    """
    assert pull_errors.classify_http_error(404).error_class == pull_errors.UNCLASSIFIED

    verdicts: dict[str, str] = {}
    for path in sorted(MODULES_DIR.glob("*/connector.py")):
        source = path.read_text(encoding="utf-8")
        if "_STATUS_OVERRIDES" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            target = (
                node.target
                if isinstance(node, ast.AnnAssign)
                else (node.targets[0] if isinstance(node, ast.Assign) and node.targets else None)
            )
            if getattr(target, "id", "") != "_STATUS_OVERRIDES":
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            for key, value in zip(node.value.keys, node.value.values):
                if ast.literal_eval(key) == 404:
                    verdicts[path.parent.name] = ast.literal_eval(value)

    assert len(set(verdicts.values())) > 1, (
        "les modules se sont mis d'accord sur le sens du 404 -- alors la question "
        f"de le porter dans core se REPOSE, et ce test doit etre rejuge : {verdicts}"
    )
