"""Un Canonical Field ne s'appelle jamais « concept » dans la copie produit.

LA REGLE EST RATIFIEE, ET ELLE ETAIT VIOLEE. `docs/product-architecture/glossary.md`
§ *The rule this settles*, arbitrage Jean du 2026-08-14 : *"Unqualified, Concept
means Semantic Concept. A Canonical Field is never called a concept in product
copy, an empty state, an error message or an MCP docstring"*. Le meme document
liste la faute dans ses propres `Incomplete if` : *"A screen calls a Canonical
Field a concept"*. Mesure du 2026-08-16 : six fichiers de la console le faisaient.

CE QUE LES DEUX OBJETS SONT, parce que la garde n'a de sens que si on les
distingue. Un **Canonical Field** est une ligne de `app.mdm_canonical_fields` :
un nom qu'une liaison verifie, sans formule et sans historique de versions. Un
**Semantic Concept** est une definition versionnee portant une expression, publiee
par un change set. Une personne qui declare le premier depuis l'onglet Mapping ne
le retrouve pas dans la lentille `Concepts` -- c'est exactement le cout que
l'arbitrage a nomme.

CE QUE CETTE GARDE NE PEUT PAS FAIRE, ET ELLE LE DIT. « Cette phrase parle-t-elle
d'un Canonical Field ou d'un Semantic Concept ? » n'est pas decidable
statiquement : le glossaire autorise explicitement le mot NU pour le second
(*"Unqualified, Concept means Semantic Concept"*), et le panneau de declaration
parle legitimement des deux dans le meme fichier. Une premiere version de cette
garde interdisait le mot dans ces fichiers : elle a rougi sur onze emplois
CORRECTS (« No published concept to reference yet », « a concept reference pins a
concept and its exact version »). Une garde qui crie au loup se desactive, et le
depot en porte deja une dont la fragilite est un constat ouvert.

CE QU'ELLE COUVRE DONC : l'intersection DECIDABLE. Les quatre gestes qui ne
s'appliquent QU'A un Canonical Field -- **lier** une colonne, **revendiquer** une
cible, **joindre** des colonnes en une, **decouper** une colonne en plusieurs --
parce qu'un Semantic Concept, le panneau le dit lui-meme, *"binds no column"*.
Une phrase qui melange le mot nu et l'un de ces gestes parle forcement du premier
objet. Les fautes hors de cette intersection (« Concept 1 » sans verbe) ne sont
pas attrapees : c'est une limite ECRITE, pas un trou qu'on ignore.

La liste des fichiers est DERIVEE et jamais ecrite a la main : tout fichier de la
console qui touche le registre. Un ecran neuf entre dans la garde sans que
personne y pense. `data-testid`, `concept_kind` et `concept_ref` gardent leur
orthographe -- le glossaire dit lui-meme qu'un nom de colonne n'est pas un mot du
produit.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONSOLE = ROOT / "ui" / "admin" / "src"

#: Ce qui fait d'un fichier une surface du registre des champs canoniques.
_TOUCHES_REGISTRY = re.compile(r"canonical-fields|mdm_canonical_fields|mdm_target")

#: Le mot NU. `Semantic Concept` est le nom qualifie et reste permis ;
#: `concept_kind` / `concept_ref` / `conceptKey` sont des tokens stockes.
_BARE_CONCEPT = re.compile(r"(?<!semantic )\bconcepts?\b(?!_|[A-Z])", re.IGNORECASE)

#: Les gestes qui ne s'appliquent QU'A un Canonical Field. Un Semantic Concept ne
#: lie aucune colonne, n'est revendique par aucune regle, et ne se joint ni ne se
#: decoupe -- c'est `DeclareConceptPanel` lui-meme qui l'ecrit : *"binds no
#: column: a calculated value is not collected from one"*.
_CANONICAL_FIELD_ONLY_GESTURE = re.compile(
    r"\b(claimed|claims?|binds?|binding|bound|joined|joins? into|splits? into|"
    r"mapped to|maps to)\b",
    re.IGNORECASE,
)

#: Une chaine de copie produit : entre guillemets doubles ou dans un gabarit,
#: portant au moins une espace. Une chaine d'un seul mot est un token.
_COPY = re.compile(r'"([^"\\\n]* [^"\\\n]*)"|`([^`\\\n]* [^`\\\n]*)`')


def _surfaces() -> list[Path]:
    """Les fichiers de la console qui touchent le registre. Derives, pas listes."""
    return sorted(
        path
        for path in CONSOLE.rglob("*.tsx")
        if "__tests__" not in path.parts
        and _TOUCHES_REGISTRY.search(path.read_text(encoding="utf-8"))
    )


def offending_copy(text: str) -> list[str]:
    """Les phrases qui disent « concept » nu POUR un geste de champ canonique."""
    found: list[str] = []
    for match in _COPY.finditer(text):
        value = next(group for group in match.groups() if group is not None)
        if _BARE_CONCEPT.search(value) and _CANONICAL_FIELD_ONLY_GESTURE.search(value):
            found.append(value.strip())
    return found


def test_the_registry_surfaces_are_actually_found():
    """Une garde qui ne surveille rien est verte pour rien.

    Si la derivation cesse de trouver les ecrans, c'est CE test qui rougit --
    pas le suivant, qui passerait sur une liste vide sans rien prouver.
    """
    surfaces = _surfaces()
    assert len(surfaces) >= 4, f"seuls {len(surfaces)} ecrans du registre trouves"
    names = {path.name for path in surfaces}
    assert "CanonicalTargetCell.tsx" in names
    assert "DeclareConceptPanel.tsx" in names


def test_the_guard_recognizes_the_sentence_it_exists_for():
    """La phrase reelle du 2026-08-16, celle qui a motive la garde.

    Sans ce test, une expression reguliere cassee rendrait le test suivant vert
    pour rien -- le defaut exact que `guard-covering-one-package-hides-the-rest`
    a coute une fois deja.
    """
    assert offending_copy('title="That concept is already claimed"') == [
        "That concept is already claimed"
    ]
    # Et elle laisse passer l'emploi CORRECT du mot nu pour l'autre objet.
    assert offending_copy('title="No published concept to reference yet"') == []
    assert offending_copy('"a concept reference pins a concept and its exact version."') == []


def test_no_registry_surface_calls_a_canonical_field_a_concept():
    """La regle du glossaire, executable sur sa part decidable."""
    problems: list[str] = []
    for path in _surfaces():
        for fragment in offending_copy(path.read_text(encoding="utf-8")):
            problems.append(f"{path.relative_to(ROOT).as_posix()}: {fragment!r}")

    assert not problems, (
        "Un Canonical Field est appele « concept » dans la copie produit "
        "(glossary.md § The rule this settles). Lier, revendiquer, joindre et "
        "decouper ne s'appliquent qu'a un champ canonique -- dire `canonical "
        "field` :\n  " + "\n  ".join(problems)
    )
