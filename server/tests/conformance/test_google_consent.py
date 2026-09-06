"""Un connecteur Google installe est-il ATTEIGNABLE par le consentement ?

POURQUOI CE FICHIER EXISTE. Mesure du 2026-07-31 (AI-94) : dix modules declarent
`auth_type: "google_direct"` -- c'est-a-dire « je n'existe que si l'ecran de
consentement Google demande mon scope » -- et le consentement n'en demandait que
sept. `google-ad-manager`, `google-business-profile` et `youtube-analytics`
etaient installes, catalogues, testes, et hors d'atteinte : aucune porte
d'entree en production, jamais.

CE QUI L'A RENDU INVISIBLE, ET C'EST LE VRAI DEFAUT. Deux constantes ecrites a
la main se surveillaient MUTUELLEMENT :

    google_oauth.GOOGLE_STACK_SCOPES        (ce qu'on demande)
    connection_tools.GOOGLE_SCOPE_CONNECTORS (quel scope ouvre quel connecteur)

et un test assertait « les deux tiennent, dans les deux sens ». Il ne pouvait pas
echouer : un connecteur absent des DEUX est absent de facon parfaitement
coherente. L'instrument comparait deux declarations entre elles, jamais une
declaration a la REALITE INSTALLEE -- exactement la classe d'AI-85 (« un critere
ferme sur un symbole que rien n'appelle ») et d'AI-88 (« un seam arme nulle
part »).

Deux manifestes affirmaient meme que leur scope etait deja la :
`google-business-profile` (« Reuses the existing business.manage scope ») et
`youtube-analytics` (« Scope youtube.readonly already present -- no new scope »).
Ni l'un ni l'autre n'etait dans aucun consentement. Une affirmation dans un
commentaire n'est pas une mesure, et personne ne l'avait confrontee.

CE QUE CE FICHIER PROUVE, hors ligne, sans compte ni reseau : le troisieme
sommet du triangle. La reference n'est plus une constante, c'est le disque --
`server/modules/*/manifest.json`.

CE QU'IL NE PROUVE PAS, dit franchement : que Google accorde reellement ces
scopes au client OAuth du deploiement, ni que le nom du scope est celui que
l'API attend. Cela demande un consentement humain reel (AI-08, human-gate) ;
aucun test hors ligne ne peut le rendre.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

#: server/tests/conformance/ -> server/ -> server/modules/
_MODULES_DIR = Path(__file__).resolve().parents[2] / "modules"

#: La valeur d'`auth_type` qui engage le consentement Google direct (AD-21).
GOOGLE_DIRECT = "google_direct"


def _installed_manifests() -> dict[str, dict]:
    """Tous les manifestes du disque, par nom de module. La reference."""
    found: dict[str, dict] = {}
    for folder in sorted(_MODULES_DIR.iterdir()):
        path = folder / "manifest.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        found[data.get("name") or folder.name] = data
    return found


def _google_direct_modules() -> dict[str, dict]:
    return {
        name: manifest
        for name, manifest in _installed_manifests().items()
        if manifest.get("auth_type") == GOOGLE_DIRECT
    }


def _declared_scopes(manifest: dict) -> list[str]:
    """Les scopes que le manifeste declare, quelle que soit l'orthographe.

    `scope` (une chaine) est la forme historique de quatre modules ; `scopes`
    (une liste) existe parce qu'un connecteur peut en exiger plusieurs --
    youtube-analytics lit deux APIs distinctes.
    """
    auth = manifest.get("auth") or {}
    if not isinstance(auth, dict):
        return []
    single = auth.get("scope")
    multiple = auth.get("scopes")
    out: list[str] = []
    if isinstance(single, str) and single.strip():
        out.append(single.strip())
    if isinstance(multiple, list):
        out.extend(str(s).strip() for s in multiple if str(s).strip())
    return out


def _consent() -> tuple[tuple[str, ...], dict[str, str]]:
    from core.connection_tools import GOOGLE_SCOPE_CONNECTORS
    from core.google_oauth import GOOGLE_STACK_SCOPES

    return GOOGLE_STACK_SCOPES, GOOGLE_SCOPE_CONNECTORS


def test_every_google_direct_module_is_reachable_by_the_consent():
    """Le test qui manquait : la reference est le disque, pas l'autre constante.

    Un module `google_direct` qu'aucun scope demande n'ouvre ne peut PAS obtenir
    d'identifiant en production. Il n'est pas « en attente de cablage » : il est
    inatteignable, et rien ne le disait.
    """
    scopes, mapping = _consent()
    unreachable = []
    for name in sorted(_google_direct_modules()):
        opened_by = [s for s in scopes if mapping.get(s) == name]
        if not opened_by:
            unreachable.append(name)

    assert not unreachable, (
        f"{len(unreachable)} module(s) declarent auth_type='{GOOGLE_DIRECT}' et "
        f"aucun scope du consentement ne les ouvre : {unreachable}. "
        "Un humain ne peut donc PAS les connecter. Declarez leur scope dans leur "
        "manifeste (`auth.scope` ou `auth.scopes`) ET ajoutez-le a "
        "GOOGLE_STACK_SCOPES + GOOGLE_SCOPE_CONNECTORS."
    )


def test_no_scope_is_requested_for_a_connector_that_is_not_installed():
    """L'autre sens : demander un scope que personne ne sert elargit le consentement.

    Un scope demande sans connecteur installe fait cocher a la personne une
    autorisation dont le produit ne fait rien -- et un consentement plus large
    que le besoin est un cout de confiance, pas un detail.
    """
    scopes, mapping = _consent()
    installed = _installed_manifests()
    orphans = []
    for scope in scopes:
        connector = mapping.get(scope)
        if connector is None:
            orphans.append(f"{scope} (aucun connecteur declare dans la carte)")
        elif connector not in installed:
            orphans.append(f"{scope} -> '{connector}' n'est pas installe")
        elif installed[connector].get("auth_type") != GOOGLE_DIRECT:
            orphans.append(
                f"{scope} -> '{connector}' a auth_type="
                f"{installed[connector].get('auth_type')!r}, pas '{GOOGLE_DIRECT}'"
            )

    assert not orphans, f"scopes demandes sans destinataire reel : {orphans}"


def test_declared_scopes_match_the_consent():
    """Declaration <-> consentement, dans les deux sens et par module.

    Le manifeste est la declaration du connecteur ; le consentement est ce que
    le produit demande. Un ecart entre les deux est le defaut d'AI-94, et il
    doit rougir ici plutot que d'attendre un clic en production.
    """
    scopes, mapping = _consent()
    mismatches = []
    for name, manifest in sorted(_google_direct_modules().items()):
        declared = _declared_scopes(manifest)
        if not declared:
            mismatches.append(f"{name}: auth_type='{GOOGLE_DIRECT}' mais ne declare aucun scope")
            continue
        for scope in declared:
            if scope not in scopes:
                mismatches.append(f"{name}: declare {scope} qui n'est PAS demande")
            elif mapping.get(scope) != name:
                mismatches.append(
                    f"{name}: declare {scope}, mais la carte l'attribue a "
                    f"{mapping.get(scope)!r}"
                )

    assert not mismatches, f"declaration et consentement divergent : {mismatches}"


def test_a_module_outside_google_direct_is_never_in_the_map():
    """`bigquery` lit avec les identifiants du DEPLOIEMENT, pas avec un jeton de personne.

    Son manifeste le dit et l'explique (`auth_type: 'none'`, ADC / compte de
    service). Le balayer dans le consentement demanderait un scope au nom d'une
    personne pour un acces qui n'est pas le sien.
    """
    _, mapping = _consent()
    installed = _installed_manifests()
    intruders = [
        connector
        for connector in set(mapping.values())
        if connector in installed and installed[connector].get("auth_type") != GOOGLE_DIRECT
    ]
    assert not intruders, f"connecteurs non-google_direct presents dans la carte : {intruders}"


def test_the_auth_block_does_not_contradict_auth_type():
    """Un bloc `auth` present doit dire la meme chose que `auth_type`.

    C'est la contradiction que le schema de manifeste documente avoir deja
    corrigee une fois (review-15-9 F-7) : `auth_type` generique en face d'un
    `auth.type='google_direct'`. On la verifie plutot que de la supposer reglee.
    """
    contradictions = []
    for name, manifest in sorted(_installed_manifests().items()):
        auth = manifest.get("auth") or {}
        if not isinstance(auth, dict) or not auth:
            continue
        declared_type = auth.get("type") or auth.get("auth_path")
        if declared_type == GOOGLE_DIRECT and manifest.get("auth_type") != GOOGLE_DIRECT:
            contradictions.append(
                f"{name}: auth.type='{GOOGLE_DIRECT}' mais auth_type="
                f"{manifest.get('auth_type')!r}"
            )
        if manifest.get("auth_type") == GOOGLE_DIRECT and declared_type not in (
            None,
            GOOGLE_DIRECT,
        ):
            contradictions.append(
                f"{name}: auth_type='{GOOGLE_DIRECT}' mais auth.type={declared_type!r}"
            )
    assert not contradictions, f"le bloc auth contredit auth_type : {contradictions}"


def test_every_requested_scope_is_named_in_words_for_the_person():
    """La console montre a la personne CE QU'ELLE ACCORDE, pas une URL.

    Quatrieme table ecrite a la main de la meme famille : `_GOOGLE_SCOPE_LABELS`
    (admin_api), avec au-dessus le commentaire « Extend when new scopes are added
    to GOOGLE_STACK_SCOPES ». Mesure du 2026-08-01 : 4 libelles pour 11 scopes.
    Trois manquaient AVANT AI-94 (dfareporting, display-video, doubleclicksearch)
    -- cm360, dv360 et sa360 s'affichaient donc deja en URL brute.

    Une instruction en commentaire n'est pas un garde-fou : celui-ci l'est.
    """
    from core.google_oauth_api import _GOOGLE_SCOPE_LABELS  # noqa: PLC0415

    scopes, _ = _consent()
    unnamed = [s for s in scopes if s not in _GOOGLE_SCOPE_LABELS]
    assert not unnamed, (
        f"{len(unnamed)}/{len(scopes)} scopes demandes s'affichent en URL brute "
        f"dans la console : {unnamed}"
    )


@pytest.mark.parametrize(
    "connector",
    ["google-ad-manager", "google-business-profile", "youtube-analytics"],
)
def test_the_three_connectors_of_ai94_are_reachable(connector: str):
    """Les trois nommes par AI-94, epingles un par un.

    Un test agrege peut redevenir vert en perdant un module de sa liste ; ces
    trois-la sont ecrits en toutes lettres, donc le retrait de l'un se voit.
    """
    installed = _installed_manifests()
    assert connector in installed, f"{connector} n'est plus installe"
    scopes, mapping = _consent()
    opened_by = [s for s in scopes if mapping.get(s) == connector]
    assert opened_by, f"{connector} n'est ouvert par aucun scope du consentement"
