# -*- coding: utf-8 -*-
"""Le cliquet du lecteur d'adresses de la console -- deux defauts de la meme classe.

CE QUE CE FICHIER EMPECHE DE REVENIR. `console_addresses.read_workspaces` est ce
qui repond a << cette adresse s'ouvre-t-elle ? >> quand une alerte DQ nomme la
reparation qu'elle propose. Il lisait le registre de navigation avec deux
hypotheses que le registre ne tient plus, et les deux le rendaient AVEUGLE plutot
que rouge -- il rendait zero onglet, et un controle d'onglet passait :

  1. **l'ordre des cles** -- `tabs:` devait suivre `type:` immediatement. Mesure
     2026-09-05 : un `label:` ajoute entre les deux dans CHAQUE contrat de CHAQUE
     espace a fait lire `ObjectContract(type='datastream', tabs=())` alors que
     `navigation/data.ts` en declarait huit, et trois tests de conformite sont
     devenus rouges en disant qu'une alerte DQ nommait un onglet `mapping`
     inexistant ;
  2. **les alias inter-fichiers** -- `_resolve_aliases` tournait fichier par
     fichier. Depuis AD-42 le registre est en sept fichiers et
     `MASTER_DATA_TABS` vit dans `navigation/vocabulary.ts` : les trois contrats
     de Master Data lisaient zero onglet, et celui-la ne faisait rougir personne.

POURQUOI UN REGISTRE SYNTHETIQUE. Ces tests ecrivent leur propre registre dans un
repertoire temporaire plutot que de lire celui du depot. Le vrai registre bouge
sous plusieurs sessions ; un test qui en depend mesurerait leur journee. Celui-ci
mesure le LECTEUR, avec les deux formes exactes qui l'ont mis en defaut.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_registry(shell: Path, *, contract: str, vocabulary: str = "") -> None:
    navigation = shell / "navigation"
    navigation.mkdir(parents=True, exist_ok=True)
    if vocabulary:
        (navigation / "vocabulary.ts").write_text(vocabulary, encoding="utf-8")
    (navigation / "data.ts").write_text(
        "export const data = {\n"
        '  key: "data",\n'
        '  slug: "data",\n'
        '  label: "Data",\n'
        "  subnav: [\n"
        '    section("datastreams", "Datastreams", [\n'
        f"      {contract}\n"
        "    ]),\n"
        "  ],\n"
        "} as const;\n",
        encoding="utf-8",
    )


def _tabs(shell: Path) -> tuple[str, ...]:
    from tests.support.console_addresses import read_workspaces  # noqa: PLC0415

    workspaces = read_workspaces(shell)
    return workspaces["data"]["datastreams"].objects["datastream"].tabs


def test_tabs_are_read_when_they_follow_type_immediately(tmp_path):
    """La forme d'origine, pour que la reparation n'ait rien casse."""
    _write_registry(
        tmp_path,
        contract='{ type: "datastream", tabs: ["overview", "mapping"] },',
    )
    assert _tabs(tmp_path) == ("overview", "mapping")


def test_tabs_are_read_when_another_key_sits_between(tmp_path):
    """LE defaut du 2026-09-05 : `label:` entre `type:` et `tabs:`.

    L'ordre des cles d'un objet TypeScript n'est pas un contrat ; l'objet l'est.
    """
    _write_registry(
        tmp_path,
        contract='{ type: "datastream", label: "Datastream", tabs: ["overview", "mapping"] },',
    )
    assert _tabs(tmp_path) == ("overview", "mapping")


def test_tabs_are_read_across_a_comment_block(tmp_path):
    """Le vrai contrat du Datastream porte vingt lignes de commentaire ici."""
    _write_registry(
        tmp_path,
        contract=(
            '{\n        type: "datastream",\n        label: "Datastream",\n'
            "        // `cost` est CONDITIONNEL, et cette phrase tient sur\n"
            "        // plusieurs lignes exactement comme dans le registre.\n"
            '        tabs: ["overview", "mapping"],\n'
            '        evidenceTabs: ["runs"],\n      },'
        ),
    )
    assert _tabs(tmp_path) == ("overview", "mapping")


def test_a_contract_never_borrows_the_tabs_of_the_next_one(tmp_path):
    """La borne : un contrat SANS onglets ne prend pas ceux de son voisin.

    C'est ce qui distingue une reparation d'un relachement. Sans elle, le
    lecteur rendrait des onglets a une adresse qui n'en declare aucune, et
    l'alerte DQ ouvrirait une page qui n'existe pas -- l'inverse du defaut,
    aussi faux.
    """
    _write_registry(
        tmp_path,
        contract=(
            '{ type: "datastream", label: "Datastream" },\n'
            '      { type: "import", label: "Import", tabs: ["overview"] },'
        ),
    )
    from tests.support.console_addresses import read_workspaces  # noqa: PLC0415

    objects = read_workspaces(tmp_path)["data"]["datastreams"].objects
    assert objects["datastream"].tabs == ()
    assert objects["import"].tabs == ("overview",)


def test_an_alias_declared_in_another_file_of_the_registry_resolves(tmp_path):
    """Le second defaut : depuis AD-42 le registre est en sept fichiers.

    Une liste d'onglets partagee vit la ou elle est partagee. Resolue fichier
    par fichier, elle rendait zero -- et zero ne fait rougir personne.
    """
    _write_registry(
        tmp_path,
        contract='{ type: "datastream", label: "Datastream", tabs: SHARED_TABS },',
        vocabulary='export const SHARED_TABS = ["overview", "hierarchy", "used-by"] as const;\n',
    )
    assert _tabs(tmp_path) == ("overview", "hierarchy", "used-by")


def test_the_real_registry_still_declares_the_datastream_workbench_tabs():
    """Une garde anti-scan-vide sur le VRAI registre.

    Les cinq tests ci-dessus mesurent le lecteur sur un registre qu'ils
    ecrivent. Si le lecteur rendait zero partout sur le vrai, ils resteraient
    verts -- le piege que `module-boundaries.md` appelle un scan vide. Cette
    ligne exige que l'ecran principal du produit ait toujours ses onglets.
    """
    from tests.support.console_addresses import read_workspaces  # noqa: PLC0415

    contract = read_workspaces()["data"]["datastreams"].objects["datastream"]
    for tab in ("overview", "mapping", "data", "runs", "outputs"):
        assert tab in contract.tabs, (
            f"le registre ne declare plus l'onglet `{tab}` du Datastream, ou le "
            "lecteur a cesse de le voir"
        )


@pytest.mark.parametrize("workspace", ["data", "governance"])
def test_no_workspace_reads_as_a_registry_of_tabless_objects(workspace):
    """Aucun espace ne doit rendre TOUS ses contrats vides.

    C'est la forme sous laquelle les deux defauts sont apparus : pas une erreur,
    un silence. Un objet sans onglet est legitime (`ai-path`) ; un espace entier
    sans onglet ne l'est pas.
    """
    from tests.support.console_addresses import read_workspaces  # noqa: PLC0415

    sections = read_workspaces()[workspace]
    contracts = [c for section in sections.values() for c in section.objects.values()]
    assert contracts, f"l'espace `{workspace}` ne declare aucun contrat d'objet"
    assert any(c.tabs for c in contracts), (
        f"tous les contrats de `{workspace}` lisent zero onglet -- le lecteur est "
        "aveugle, et un controle d'onglet passe sans rien verifier"
    )
