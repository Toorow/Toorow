"""Ce qu'une Skill DESIGNE du modele de donnees -- resolu, jamais devine.

POURQUOI. Jean, 2026-08-03 : « dans les skills et les business tu dois pouvoir
utiliser des colonnes de ton MDM avec {{produit}} {{cout}}, pour que s'il en a
besoin indirectement il puisse savoir de quoi tu parles ».

Une Skill designe le modele de donnees de DEUX facons, et elles se completent :

    mdm_tags        QUELLES metriques elle concerne -- une liste, lisible d'un
                    coup d'oeil, deja portee par le frontmatter
    {{champ}}       OU elle s'en sert -- une reference dans le texte, qui se
                    resout au point de lecture

Le catalogue gouverne porte de quoi repondre : `data_type`, `field_kind`,
`measure`, `description`, et l'etat d'approbation. Un `{{cost}}` resolu rend donc
le type, la nature de mesure, la definition et le fait qu'un humain l'a approuve.
Depuis la story 49.3 c'est `core.governed_field_catalogue` qui repond -- le
Modele Semantique d'abord, `app.target_fields` en repli -- et non plus le
dictionnaire seul, dont les portes d'ecriture sont fermees depuis le 2026-08-25.

CE MODULE RESOUT ET RAPPORTE. IL NE REFUSE RIEN, et ce n'est pas une facilite :

  * la procedure que le PRODUIT seme (migration 196) porte
    `mdm_tags: [platform-operating-procedure, datastream, context, testing,
    measurement]` -- et AUCUN de ces cinq n'est un champ du catalogue ;
  * la console, elle, intitule ce champ « MDM tags — Canonical metrics » et le
    lie par cases a cocher a `app.target_fields`.

Les deux ne peuvent pas avoir raison. Trancher en rejetant rendrait la procedure
du produit non modifiable, et trancher est une decision de conception, pas une
reparation. Donc on MESURE l'ecart et on le publie -- comme
`screens/expectations.md` publie une attente non tenue au lieu d'echouer dessus.
"""

from __future__ import annotations

import re
from typing import Any

from core.governed_field_catalogue import COLUMNS as _CATALOGUE_COLUMNS
from core.governed_field_catalogue import resolve as _resolve_catalogue

#: `{{ cost }}` autant que `{{cost}}`. Un nom de champ ne porte ni espace ni
#: point : tout le reste est du texte, et le laisser passer inventerait des
#: references.
_REFERENCE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: Ce qu'on rend d'un champ resolu. Assez pour « savoir de quoi tu parles »,
#: rien de plus : ni valeur, ni ligne, ni provenance -- ce module lit un
#: catalogue, il ne lit aucune donnee. DERIVE du catalogue gouverne : les deux
#: listes ne peuvent pas diverger sans que quelqu'un le decide.
_COLUMNS = _CATALOGUE_COLUMNS


def references(text: str) -> list[str]:
    """Les noms cites en `{{...}}`, sans doublon, dans l'ordre d'apparition."""
    seen: list[str] = []
    for match in _REFERENCE.finditer(text or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def tags_of(frontmatter_yaml: str) -> list[str]:
    """Les `mdm_tags` d'un frontmatter, sans jamais lever.

    Une lecture ne doit pas echouer parce qu'une Skill est mal formee : le
    validateur est la pour refuser a l'ECRITURE. Ici, un frontmatter illisible
    rend une liste vide, et la Skill reste lisible.
    """
    import yaml  # noqa: PLC0415

    try:
        data = yaml.safe_load(frontmatter_yaml or "") or {}
    except Exception:  # noqa: BLE001
        return []
    tags = data.get("mdm_tags") if isinstance(data, dict) else None
    if not isinstance(tags, list):
        return []
    return [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()]


def _catalog(
    conn: Any, names: list[str], project_id: str | None = None
) -> dict[str, dict[str, Any]]:
    """Le catalogue gouverne, MODELE SEMANTIQUE D'ABORD (story 49.3).

    Cette fonction lisait `app.target_fields` seule. Depuis le 2026-08-25 ses
    cinq portes d'ecriture repondent 409 `legacy_store_is_read_only` : une
    metrique declaree au workbench de Concepts etait donc rendue `unresolved`
    dans la Skill qui la cite, et le seul catalogue nomme par le refus
    (`GET /api/datamodel/fields`) ne peut plus grandir. `governed_field_catalogue`
    interroge le successeur d'abord et garde le dictionnaire en repli -- rien de
    ce qui resolvait ne cesse de resoudre.
    """
    if not names:
        return {}
    return _resolve_catalogue(conn, names=names, project_id=project_id)


def resolve(conn: Any, *, frontmatter_yaml: str = "", body_md: str = "",
            mdm_tags: list[str] | None = None,
            project_id: str | None = None) -> dict[str, Any]:
    """Ce qu'une Skill designe, et ce qu'elle designe DANS LE VIDE.

    Rend toujours les deux cotes. Ne rendre que les references resolues
    laisserait croire qu'une Skill ne cite que des champs existants -- ce qui est
    exactement le genre de silence qui fait vieillir une base sans qu'on le voie.
    """
    cited = references(f"{frontmatter_yaml}\n{body_md}")
    tags = list(mdm_tags or [])
    catalog = _catalog(conn, sorted(set(cited) | set(tags)), project_id)

    return {
        "inline": {
            "resolved": [catalog[name] for name in cited if name in catalog],
            # Une reference qui ne resout pas n'est PAS une faute de frappe
            # presumee : c'est peut-etre un champ retire. Elle est nommee telle
            # quelle, et c'est au lecteur de decider.
            "unresolved": [name for name in cited if name not in catalog],
        },
        "tags": {
            "resolved": [catalog[name] for name in tags if name in catalog],
            "unresolved": [name for name in tags if name not in catalog],
        },
    }


def render(text: str, catalog: dict[str, dict[str, Any]]) -> str:
    """Remplacer `{{champ}}` par son libelle gouverne, pour un lecteur humain.

    Une reference NON resolue est laissee TELLE QUELLE, accolades comprises. La
    remplacer par un blanc, ou par le nom nu, effacerait la seule trace qu'un
    champ a disparu -- et cette trace est le retour sur la Skill.
    """
    def swap(match: re.Match[str]) -> str:
        field = catalog.get(match.group(1))
        if field is None:
            return match.group(0)
        return field.get("display_name") or field["name"]

    return _REFERENCE.sub(swap, text or "")
