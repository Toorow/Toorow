"""Le referentiel de plateforme du Modele Semantique, DERIVE et jamais invente (AI-288).

CE QUI MANQUAIT, ET CE QUE CA COUTAIT. La formule d un ratio existe en TROIS
copies : `dbt/seeds/dim_metric.csv` la declare, `dbt/models/marts/semantic_*.sql`
la calcule, et `app.semantic_concept_versions.expression` la gouverne. Les deux
premieres sont au depot et tenues par `make check-metric-formula-parity`. La
troisieme vit en base et **rien ne la provisionnait** : une instance neuve ne
gouverne aucune formule, donc la garde a trois sources ne pouvait comparer que
deux copies et le disait.

POURQUOI PERSONNE NE L AVAIT ECRIT. Decider QUELS concepts sont de plateforme,
c est ecrire le vocabulaire du produit ; le deviner et le presenter comme une
reparation est ce que `CLAUDE.md` interdit, et une session precedente a refuse de
trancher seule -- correctement.

CE QUI A CHANGE : ca n a jamais eu besoin d etre devine. `dim_metric.csv` **est**
le catalogue de metriques livre avec le produit -- vingt lignes, celles sur
lesquelles dbt construit ses marts. C est exactement l argument que
`platform_canonical_vocabulary` fait pour les champs : *"it already exists,
ratified"*. Ce module projette ce catalogue, et chaque colonne vient d une ligne
que quelqu un a deja ratifiee.

LA CASCADE DE TYPE EST CELLE DE LA MIGRATION 142, pas une seconde. La migration
appliquee a deja tranche `ratio -> 'ratio'`, `monetaire -> 'money'`, sinon le type
du dictionnaire, sinon `decimal`. En reprendre une autre ferait diverger les
concepts migres de ceux provisionnes -- deux vocabulaires pour un seul objet.

CE QUE LE MODULE REFUSE PAR SON NOM. `impression_weighted_average`,
`impression_weighted` et `weighted_ratio` ne sont PAS dans
`AGGREGATION_FUNCTIONS` : le contrat d expression ne sait pas les ecrire. Elles
ne sont pas approximees en `average` -- ce serait declarer sommable-par-moyenne
une metrique qui ne l est pas. Leur ligne est NON ADDITIVE, elle porte donc
`aggregation = NULL`, ce que le CHECK de la migration 142 accepte explicitement
pour ce cas. La formule ponderee reste ou elle est deja juste : dans le mart
`semantic_avg_position`.

ET IL N EDITE JAMAIS UNE LIGNE QUI EXISTE. Un concept stocke qui contredit la
projection est une DIVERGENCE : rapportee, jamais resolue. Entre un depot et un
registre vivant, choisir un camp en silence detruit l autre. Meme posture que
`platform_canonical_vocabulary` et `platform_clocks`, et pour la meme raison.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.audit import declare_action, insert_audit_row
from core.semantic_expressions import AGGREGATION_FUNCTIONS

#: Minter un concept de PLATEFORME change ce que tout projet de l instance lit
#: comme definition d une metrique. Action distincte du mint projet, pour que
#: lire le journal ne demande pas de deduire la portee d un `project_id` absent.
ACTION_PLATFORM_CONCEPT_DECLARED = declare_action("platform_semantic_concept.declared")

_REPO = Path(__file__).resolve().parents[2]

#: Le catalogue de metriques LIVRE. Lu, jamais recopie : une seconde liste ici
#: serait libre de diverger de celle sur laquelle dbt construit.
_DIM_METRIC = _REPO / "dbt" / "seeds" / "dim_metric.csv"

#: Quelles metriques sont de l argent. Meme seed que le contrat monetaire.
_MONEY_UNITS = _REPO / "dbt" / "seeds" / "money_metric_units.csv"

#: `target_fields.data_type` -> `value_type`. La meme table que
#: `platform_canonical_vocabulary`, importee plutot que retapee.
_VALUE_TYPE_BY_DATA_TYPE: Mapping[str, str] = {
    "integer": "integer",
    "decimal": "decimal",
    "currency": "money",
    "date": "date",
    "string": "string",
    "boolean": "boolean",
    "timestamp": "timestamp",
}

#: Le mart rend `NULLIF(SUM(denominateur), 0)`, c est-a-dire NULL. La politique
#: n est donc pas un defaut choisi ici : c est la lecture du SQL deja servi.
_ZERO_DENOMINATOR = "null"


class PlatformConceptError(ValueError):
    """Une ligne du catalogue que cette projection ne sait pas exprimer, nommee."""


#: LE NOEUD QUI REND UNE VERSION IMPUBLIABLE. `ExpressionAnalysis.publishable`
#: (`semantic_expressions.py`) est `ok and not unresolved_names`, et un
#: `concept_name` EST un nom non resolu. Lu ici par le NOM DU NOEUD plutot que
#: par le mot `ratio` : ce qui rend une version impubliable n est pas d etre un
#: ratio, c est de porter un operande que rien ne pointe a une version exacte.
_UNRESOLVED_OPERAND_OP = "concept_name"


def unresolved_operand_names(expression: Any) -> list[str]:
    """Les operandes qu un arbre porte par NOM -- ceux qui interdisent la publication.

    Marche l arbre entier. Une liste vide veut dire que la version peut etre
    publiee telle quelle ; une liste non vide nomme exactement ce qu il faudrait
    epingler d abord, et a la portee PLATEFORME il n y a aucun projet contre
    lequel les epingler.
    """
    found: list[str] = []
    stack: list[Any] = [expression]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            if node.get("op") == _UNRESOLVED_OPERAND_OP:
                name = node.get("name")
                if isinstance(name, str) and name:
                    found.append(name)
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return sorted(set(found))


@dataclass(frozen=True)
class PlatformConcept:
    """Un concept de plateforme projete, pret a etre declare."""

    name: str
    value_type: str
    expression: dict[str, Any]
    additivity_class: str
    aggregation: dict[str, Any] | None = None
    non_additive_dimensions: list[str] = dataclass_field(default_factory=list)
    kind: str = "metric"

    @property
    def label(self) -> str:
        """Le nom lisible. `NOT NULL` en base, et jamais laisse vide."""
        return self.name.replace("_", " ").capitalize()

    @property
    def unresolved_operands(self) -> list[str]:
        """Les operandes que la formule porte par NOM, jamais par version exacte."""
        return unresolved_operand_names(self.expression)

    @property
    def publishable_at_platform_scope(self) -> bool:
        """Est-ce que cette forme peut etre PUBLIEE sans projet pour la resoudre ?

        C est le predicat de l amendement du 2026-08-25 de `governance.md`, lu ou
        il se decide : un concept de plateforme n a aucun projet contre lequel
        epingler ses operandes, donc une formule qui en porte un par nom serait
        une version << impubliable par construction >>.
        """
        return not self.unresolved_operands


# ---------------------------------------------------------------------------
# La projection -- pure, elle ne lit aucune base
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def money_metric_names(path: Path | None = None) -> frozenset[str]:
    """Les metriques que le contrat monetaire declare -- quelle que soit l unite."""
    return frozenset(
        (row["canonical_metric"] or "").strip()
        for row in _read_csv(path or _MONEY_UNITS)
        if (row.get("canonical_metric") or "").strip()
    )


def _value_type(
    name: str, aggregation_rule: str, *, money: frozenset[str], dictionary: Mapping[str, str]
) -> str:
    """La cascade de la migration 142, dans son ordre.

    Un ratio est un ratio avant d etre autre chose ; l argent ensuite ; puis ce
    que le dictionnaire gouverne declare ; puis `decimal`, qui est le repli que la
    migration appliquee a deja pose pour une metrique qu aucun dictionnaire ne
    porte.
    """
    if aggregation_rule == "ratio":
        return "ratio"
    if name in money:
        return "money"
    declared = dictionary.get(name)
    if declared:
        mapped = _VALUE_TYPE_BY_DATA_TYPE.get(declared)
        if mapped is None:
            raise PlatformConceptError(
                f"{name!r}: le type de donnee {declared!r} du dictionnaire n a pas de "
                "value_type canonique. L ajouter deliberement a _VALUE_TYPE_BY_DATA_TYPE "
                "plutot que de laisser la metrique atterrir en `decimal`."
            )
        return mapped
    return "decimal"


def project_catalogue(
    rows: Iterable[Mapping[str, Any]],
    *,
    money: frozenset[str] | None = None,
    dictionary: Mapping[str, str] | None = None,
) -> tuple[list[PlatformConcept], list[str]]:
    """`(concepts, refus)` du catalogue livre. Pure : aucune base n est lue.

    Le second membre est ce que la projection REFUSE de fabriquer, une ligne par
    raison. Une regle d agregation que le contrat d expression ne sait pas ecrire
    n est pas approximee : elle est nommee.
    """
    money_names = money if money is not None else money_metric_names()
    governed = dictionary or {}
    concepts: list[PlatformConcept] = []
    refused: list[str] = []

    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        rule = str(row.get("aggregation_rule") or "").strip()
        additive = str(row.get("additive") or "").strip().lower() == "true"
        value_type = _value_type(name, rule, money=money_names, dictionary=governed)

        if rule == "ratio":
            numerator = str(row.get("ratio_numerator") or "").strip()
            denominator = str(row.get("ratio_denominator") or "").strip()
            if not numerator or not denominator:
                refused.append(
                    f"{name}: declare `ratio` sans numerateur ni denominateur -- la "
                    "formule n est ecrite nulle part, donc rien ne peut la gouverner."
                )
                continue
            concepts.append(
                PlatformConcept(
                    name=name,
                    value_type=value_type,
                    #  LA MEME FORME QUE LA MIGRATION 142 (`142:825-835`) : les
                    #  operandes par NOM. C est la forme de REFERENCE que la
                    #  garde de parite compare, et c est aussi celle que
                    #  `ExpressionAnalysis.publishable` refuse -- un `concept_name`
                    #  est un nom non resolu. La projection la produit donc pour
                    #  etre COMPAREE et pour etre ADOPTEE par un projet
                    #  (`semantic_metric_presets` reecrit chaque operande en
                    #  `concept_ref` epingle a une version exacte) ; le mint de
                    #  plateforme, lui, la RETIENT -- voir `provision`.
                    expression={
                        "op": "ratio",
                        "zero_denominator": _ZERO_DENOMINATOR,
                        "numerator": {"op": "concept_name", "name": numerator},
                        "denominator": {"op": "concept_name", "name": denominator},
                    },
                    #  Un ratio ne se somme pas -- c est AD-4, et le mart le dit
                    #  deja en le calculant en vue plutot qu en le stockant.
                    additivity_class="non_additive",
                    aggregation=None,
                )
            )
            continue

        expression = {"op": "source_measure", "concept": name}
        if additive:
            if rule not in AGGREGATION_FUNCTIONS:
                refused.append(
                    f"{name}: la regle {rule!r} est declaree ADDITIVE et n est pas une "
                    f"fonction du contrat ({sorted(AGGREGATION_FUNCTIONS)}). Une metrique "
                    "additive doit dire comment elle s agrege ; l approximer serait "
                    "declarer une facon de la sommer que personne n a choisie."
                )
                continue
            concepts.append(
                PlatformConcept(
                    name=name,
                    value_type=value_type,
                    expression=expression,
                    additivity_class="additive",
                    aggregation={"function": rule},
                )
            )
            continue

        #  NON ADDITIVE ET NON RATIO : `impression_weighted_average`,
        #  `impression_weighted`, `weighted_ratio`, `max`. Les trois premieres ne
        #  sont pas dans `AGGREGATION_FUNCTIONS` et ne sont PAS approximees en
        #  `average` : ce serait declarer sommable-par-moyenne une metrique qui ne
        #  l est pas, ce que le garde AD-4 existe pour empecher. Une metrique non
        #  additive a le droit de ne porter aucune agregation -- le CHECK de la
        #  migration 142 l accepte explicitement pour ce cas -- et la formule
        #  ponderee reste la ou elle est juste : dans `semantic_avg_position`.
        concepts.append(
            PlatformConcept(
                name=name,
                value_type=value_type,
                expression=expression,
                additivity_class="non_additive",
                aggregation={"function": rule} if rule in AGGREGATION_FUNCTIONS else None,
            )
        )

    concepts.sort(key=lambda concept: concept.name)
    return concepts, refused


def project_delivered_catalogue(
    dictionary: Mapping[str, str] | None = None,
) -> tuple[list[PlatformConcept], list[str]]:
    """La projection du catalogue tel qu il est livre dans ce depot."""
    return project_catalogue(_read_csv(_DIM_METRIC), dictionary=dictionary)


# ---------------------------------------------------------------------------
# La parite des formules -- UNE regle, deux lecteurs
# ---------------------------------------------------------------------------
#
# La formule d un ratio existe en TROIS copies (le seed, le mart, le Concept
# gouverne) et `scripts/check_metric_formula_parity.py` les confronte. Son VERDICT
# n etait visible que de qui lance le script : le modele de lecture de la
# gouvernance n en composait rien et l onglet Semantic Model n en rendait rien.
#
# La regle vit donc ICI, pas dans le script : `scripts/` n est pas dans l image
# de deploiement (`infra/docker/mcp-server/Dockerfile` copie `server/` et
# `dbt/`), donc un lecteur d execution qui importerait le script planterait en
# production. Le script l importe, et il n en garde aucune copie.

#: Les regles d agregation qui declarent un numerateur et un denominateur.
RATIO_AGGREGATION_RULES = frozenset({"ratio"})

#: Les trois verdicts de parite. Un quatriemme se declare ici.
PARITY_ALIGNED = "aligned"
PARITY_PLATFORM_DIVERGENCE = "platform_divergence"
PARITY_PROJECT_OVERRIDE = "project_override"
#: La formule gouvernee EST un ratio du catalogue mais ses operandes ne se lisent
#: pas par nom ici. Ce n est pas une faute : c est l aveu que la comparaison n a
#: pas eu lieu, et le taire la ferait passer pour reussie.
PARITY_UNREADABLE = "unreadable"


def declared_ratios(path: Path | None = None) -> dict[str, tuple[str, str]]:
    """`{metrique -> (numerateur, denominateur)}` tels que `dim_metric.csv` les declare.

    Une ligne `ratio` sans operande rend `("", "")` plutot que d etre omise : son
    absence de formule est elle-meme ce que la garde doit rapporter.
    """
    ratios: dict[str, tuple[str, str]] = {}
    for row in _read_csv(path or _DIM_METRIC):
        if (row.get("aggregation_rule") or "").strip() not in RATIO_AGGREGATION_RULES:
            continue
        name = (row.get("name") or "").strip()
        if not name:
            continue
        ratios[name] = (
            (row.get("ratio_numerator") or "").strip(),
            (row.get("ratio_denominator") or "").strip(),
        )
    return ratios


def ratio_operand_names(
    expression: Any, names_by_version_id: Mapping[str, str] | None = None
) -> tuple[str | None, str | None] | None:
    """Les DEUX operandes d un arbre `ratio`, par nom. `None` si l arbre n est pas un ratio.

    Deux formes d operande coexistent legitimement et il faut lire les deux :
    `concept_name` (la forme NON RESOLUE, qui parse et ne se publie pas) et
    `concept_ref` (la forme resolue, qui pointe une version exacte). Ne lire que
    la premiere rendrait la garde aveugle a tout ratio publie -- c est-a-dire a
    tout ratio qui sert vraiment.

    Un operande d une troisieme forme (`sum`, `literal`, une arithmetique) rend
    `None` a sa place : ce n est pas une composante nommee.
    """
    if not isinstance(expression, Mapping) or expression.get("op") != "ratio":
        return None
    resolved = names_by_version_id or {}

    def _name(node: Any) -> str | None:
        if not isinstance(node, Mapping):
            return None
        if node.get("op") == _UNRESOLVED_OPERAND_OP:
            name = node.get("name")
            return name if isinstance(name, str) and name else None
        if node.get("op") == "concept_ref":
            return resolved.get(str(node.get("version_id") or "")) or None
        return None

    return _name(expression.get("numerator")), _name(expression.get("denominator"))


def formula_parity(
    *,
    name: str | None,
    project_id: str | None,
    expression: Any,
    names_by_version_id: Mapping[str, str] | None = None,
    ratios: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Le verdict de parite d UNE formule gouvernee, ou `None` s il n y en a pas.

    `None` veut dire : cette formule n est pas un ratio que le catalogue livre
    declare, donc il n y a rien a confronter. C est le cas de la quasi-totalite
    des Concepts, et une pastille << rien a signaler >> sur chacun d eux
    apprendrait a ne plus la lire.

    Les deux verdicts negatifs ne disent PAS la meme chose, et c est la
    distinction que le script tient depuis 2026-08-16 :

    * a la portee PLATEFORME, le meme objet dit deux choses -- une divergence ;
    * a la portee PROJET, le client a le DROIT de sa definition ; ce qui doit
      etre dit est que le mart `semantic_<name>` n implemente pas la sienne.
    """
    metric = (name or "").strip()
    catalogue = ratios if ratios is not None else declared_ratios()
    declared = catalogue.get(metric)
    if declared is None or not declared[0] or not declared[1]:
        return None
    operands = ratio_operand_names(expression, names_by_version_id)
    if operands is None:
        return None

    declared_text = f"{declared[0]} / {declared[1]}"
    numerator, denominator = operands
    if numerator is None or denominator is None:
        return {
            "verdict": PARITY_UNREADABLE,
            "declared": declared_text,
            "governed": None,
            "message": (
                f"This formula is a ratio named `{metric}`, and its operands cannot be "
                f"read by name here, so it was not compared with the delivered "
                f"catalogue ({declared_text}). Open the formula and pin each operand "
                "to an exact Concept version."
            ),
        }

    governed_text = f"{numerator} / {denominator}"
    if (numerator, denominator) == declared:
        return {
            "verdict": PARITY_ALIGNED,
            "declared": declared_text,
            "governed": governed_text,
            "message": (
                f"This formula computes on the components the delivered catalogue "
                f"declares ({declared_text}), which is what `semantic_{metric}` serves."
            ),
        }
    if project_id:
        return {
            "verdict": PARITY_PROJECT_OVERRIDE,
            "declared": declared_text,
            "governed": governed_text,
            "message": (
                f"This Project governs `{governed_text}` where the delivered catalogue "
                f"and the served table compute `{declared_text}`. That is your "
                f"definition to make -- but `semantic_{metric}` does not implement it, "
                "so read this metric from a Semantic View built on this Concept rather "
                "than from the delivered one."
            ),
        }
    return {
        "verdict": PARITY_PLATFORM_DIVERGENCE,
        "declared": declared_text,
        "governed": governed_text,
        "message": (
            f"The platform Concept governs `{governed_text}` and the delivered "
            f"catalogue declares `{declared_text}`: the same metric says two things. "
            "Publish a new version of this Concept on the declared components, or "
            "change the catalogue -- one of the two has to move."
        ),
    }


# ---------------------------------------------------------------------------
# La base -- lire, comparer, minter ce qui manque
# ---------------------------------------------------------------------------


def load_dictionary_types(conn) -> dict[str, str]:
    """`{nom -> data_type}` du dictionnaire gouverne, pour la cascade de type."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, data_type FROM app.target_fields WHERE field_kind = 'metric'"
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def load_platform_concepts(conn) -> dict[str, dict[str, Any]]:
    """Les concepts de plateforme deja stockes, par nom, avec leur version en tete."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.name, c.id, c.lifecycle_status, v.id, v.value_type,
                   v.additivity_class, v.expression, v.aggregation,
                   -- INTOUCHE PAR QUICONQUE : version 1, auteur `system`, et la
                   -- seule version du concept. C est la signature du semis de la
                   -- migration 142 ; une version que quelqu un a publiee porte un
                   -- autre auteur ou un numero plus grand.
                   (v.version_number = 1 AND v.created_by = 'system'
                    AND (SELECT count(*) FROM app.semantic_concept_versions w
                          WHERE w.concept_id = c.id) = 1) AS untouched_seed
              FROM app.semantic_concepts c
              LEFT JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
             WHERE c.project_id IS NULL AND c.kind = 'metric'
            """
        )
        rows = cur.fetchall()
    return {
        str(row[0]): {
            "concept_id": row[1],
            "lifecycle_status": row[2],
            "version_id": row[3],
            "value_type": row[4],
            "additivity_class": row[5],
            "expression": row[6],
            "aggregation": row[7],
            "untouched_seed": bool(row[8]),
        }
        for row in rows
    }


def divergences(
    stored: Mapping[str, Mapping[str, Any]], projected: Iterable[PlatformConcept]
) -> list[str]:
    """Ou une ligne stockee et la projection ne disent pas la meme chose.

    Rapporte, JAMAIS resolu. Une formule gouvernee qui contredit le mart est
    exactement ce que `check_metric_formula_parity` fait rougir ; la reparer ici
    en silence ferait disparaitre la divergence sans que personne l ait tranchee.
    """
    problems: list[str] = []
    for concept in projected:
        row = stored.get(concept.name)
        if row is None or row.get("version_id") is None:
            continue
        for column, expected in (
            ("value_type", concept.value_type),
            ("additivity_class", concept.additivity_class),
            ("expression", concept.expression),
            ("aggregation", concept.aggregation),
        ):
            actual = row.get(column)
            if actual != expected:
                problems.append(
                    f"{concept.name}: {column} stocke {actual!r}, le depot projette {expected!r}"
                )
    return problems


def classify_divergences(
    stored: Mapping[str, Mapping[str, Any]], projected: Iterable[PlatformConcept]
) -> tuple[list[str], list[str]]:
    """`(semis_intouches, declarations)` -- deux divergences qui ne se traitent pas pareil.

    LA DISTINCTION EST MESUREE, PAS SUPPOSEE. Une version 1 dont l auteur est
    `system` et qui est la SEULE version de son concept n a jamais ete touchee par
    personne : c est le semis de la migration 142, tel quel. Une version que
    quelqu un a publiee porte un autre auteur ou un numero plus grand.

    POURQUOI CA COMPTE. Le semis de 142 s est fait contre une table VIDE :
    `app.metric_definitions` est peuplee par l APPLICATION
    (`metric_semantics.upsert_metric_definition`), pas par une migration, donc son
    `LEFT JOIN LATERAL` ne trouvait rien et la derivation retombait sur son
    `ELSE 'additive'`. Mesure du 2026-08-17 sur un cluster fraichement migre :
    `average_position` -- declaree `additive = false` dans `dim_metric.csv` ET dans
    `metric_definitions` -- est gouvernee `additive` avec `aggregation = sum`. Et
    le Modele Semantique GAGNE la precedence, donc le magasin qui decide declare
    sommable une metrique que AD-4 interdit de sommer.

    Ce n est pas une decision humaine qu on ecraserait : c est un defaut d ordre.
    Une declaration, elle, reste rapportee et jamais resolue.
    """
    seeded: list[str] = []
    declared: list[str] = []
    for concept in projected:
        row = stored.get(concept.name)
        if row is None or row.get("version_id") is None:
            continue
        mismatched = [
            f"{concept.name}: {column} stocke {row.get(column)!r}, le depot projette {expected!r}"
            for column, expected in (
                ("value_type", concept.value_type),
                ("additivity_class", concept.additivity_class),
                ("expression", concept.expression),
                ("aggregation", concept.aggregation),
            )
            if row.get(column) != expected
        ]
        if not mismatched:
            continue
        (seeded if row.get("untouched_seed") else declared).extend(mismatched)
    return seeded, declared


def repair_seeded_concept(conn, concept: PlatformConcept, *, actor: str) -> dict[str, Any]:
    """Publier une version CORRIGEE d un concept que la migration a mal seme.

    RIEN N EST EDITE. La version 1 est immuable et le reste -- le trigger
    `trg_semantic_concept_versions_immutable` refuse tout UPDATE, et c est bien --
    donc la correction est une version 2 et un pointeur qui bouge. L histoire dit
    ce que la migration avait ecrit ; la tete dit ce que le catalogue livre
    declare.
    """
    from ulid import ULID  # noqa: PLC0415

    #  Une version 2 est une PUBLICATION comme une autre : elle est soumise au
    #  meme predicat que le mint, sinon la reparation reintroduirait a la tete du
    #  concept la forme que le mint refuse d ecrire.
    if not concept.publishable_at_platform_scope:
        raise PlatformConceptError(withheld_from_platform_mint(concept))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, MAX(v.version_number)
              FROM app.semantic_concepts c
              JOIN app.semantic_concept_versions v ON v.concept_id = c.id
             WHERE c.project_id IS NULL AND c.name = %s
             GROUP BY c.id
            """,
            (concept.name,),
        )
        row = cur.fetchone()
        if row is None:
            raise PlatformConceptError(f"{concept.name!r} n est pas un concept de plateforme")
        concept_id, head = str(row[0]), int(row[1])

        version_id = f"scv_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name, label,
                 value_type, expression, aggregation, additivity_class,
                 non_additive_dimensions, provenance, content_hash, created_by)
            VALUES (%s, %s, NULL, %s, 'published', %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s, %s, %s::jsonb, %s, %s)
            """,
            (
                version_id,
                concept_id,
                head + 1,
                concept.kind,
                concept.name,
                concept.label,
                concept.value_type,
                _canonical_json(concept.expression),
                _canonical_json(concept.aggregation) if concept.aggregation else None,
                concept.additivity_class,
                concept.non_additive_dimensions,
                _canonical_json(
                    {
                        "derived_from": "dbt/seeds/dim_metric.csv",
                        "projection": "core.platform_semantic_concepts",
                        "supersedes": "migration 142 seed, written against an empty "
                        "app.metric_definitions",
                    }
                ),
                _content_hash(concept),
                actor,
            ),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s, updated_at = NOW() "
            "WHERE id = %s AND project_id IS NULL",
            (version_id, concept_id),
        )
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_PLATFORM_CONCEPT_DECLARED,
        provider_account="governance",
        connection_ref="",
        metadata={
            "scope": "platform",
            "concept_id": concept_id,
            "version_id": version_id,
            "name": concept.name,
            "version_number": head + 1,
            "repairs": "migration_142_seed",
            "additivity_class": concept.additivity_class,
        },
    )
    return {"concept_id": concept_id, "version_id": version_id, "name": concept.name}


def withheld_from_platform_mint(concept: PlatformConcept) -> str:
    """Pourquoi ce concept ne peut PAS etre minte a la portee plateforme, et quoi faire.

    Le refus nomme le GESTE, jamais la cause technique : un ratio du catalogue
    livre n est pas absent du produit, il est un preset qu un projet ADOPTE
    (`core.semantic_metric_presets`), et l adoption epingle chaque operande a une
    version exacte -- ce qu aucune portee plateforme ne peut faire.
    """
    #  LES OPERANDES DANS L ORDRE DE LA FORMULE, jamais l ensemble trie.
    #  `unresolved_operands` est un ensemble ordonne par nom : s en servir pour
    #  ecrire une division rendait << roas divise cost par revenue >>, soit
    #  l inverse de ce que le catalogue declare. Une phrase qui nomme la mauvaise
    #  division est pire qu une absence de phrase.
    operands = ratio_operand_names(concept.expression)
    if operands and operands[0] and operands[1]:
        formula = f"Its formula divides {operands[0]} by {operands[1]}"
    else:
        formula = "Its formula names " + ", ".join(concept.unresolved_operands)
    return (
        f"{concept.name}: adopt it in a Project. {formula}, and a published "
        "version pins each operand at an exact Concept version. The platform "
        "catalogue has no Project to pin them against, so the version would be "
        "unpublishable by construction."
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_hash(concept: PlatformConcept) -> str:
    """L empreinte de ce que la version dit. Deterministe : deux runs, un hash."""
    return hashlib.sha256(
        _canonical_json(
            {
                "name": concept.name,
                "kind": concept.kind,
                "value_type": concept.value_type,
                "expression": concept.expression,
                "aggregation": concept.aggregation,
                "additivity_class": concept.additivity_class,
                "non_additive_dimensions": concept.non_additive_dimensions,
            }
        ).encode("utf-8")
    ).hexdigest()


def declare_platform_concept(conn, concept: PlatformConcept, *, actor: str) -> dict[str, Any]:
    """Minter UN concept de plateforme et sa version 1. L appelant tient la transaction.

    Deliberement HORS de `semantic_model`: la porte des change sets exige un
    projet, une preparation et un verdict de la porte Test. Un referentiel de
    plateforme ne passe pas par la ceremonie d un projet -- il passe par un script,
    ou aucune route HTTP ne l atteint.
    """
    from ulid import ULID  # noqa: PLC0415 -- un import pour un seul chemin d ecriture

    #  LA PORTE REFUSE LA FORME IMPUBLIABLE, pas seulement son appelant. Une
    #  version `published` a `project_id = NULL` portant un operande par NOM est
    #  ce que l amendement du 2026-08-25 appelle << impubliable par construction >> :
    #  elle traverse le trigger d immuabilite et devient une definition que rien
    #  ne peut compiler, dans le vocabulaire que TOUT projet de l instance lit.
    #  Mesure du 2026-08-31 : ce chemin mintait `cpa`, `ctr` et `roas` a
    #  `project_id = NULL, status = 'published'` avec des operandes `concept_name`.
    if not concept.publishable_at_platform_scope:
        raise PlatformConceptError(withheld_from_platform_mint(concept))

    concept_id, version_id = f"sc_{ULID()}", f"scv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_concepts
                (id, project_id, kind, name, lifecycle_status, current_version_id, created_by)
            VALUES (%s, NULL, %s, %s, 'published', %s, %s)
            """,
            (concept_id, concept.kind, concept.name, version_id, actor),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name, label,
                 value_type, expression, aggregation, additivity_class,
                 non_additive_dimensions, provenance, content_hash, created_by)
            VALUES (%s, %s, NULL, 1, 'published', %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s, %s, %s::jsonb, %s, %s)
            """,
            (
                version_id,
                concept_id,
                concept.kind,
                concept.name,
                concept.label,
                concept.value_type,
                _canonical_json(concept.expression),
                _canonical_json(concept.aggregation) if concept.aggregation else None,
                concept.additivity_class,
                concept.non_additive_dimensions,
                #  D OU CA VIENT, dans la ligne elle-meme : une version publiee est
                #  immuable, donc sa provenance ne se rattrape pas apres coup.
                _canonical_json(
                    {
                        "derived_from": "dbt/seeds/dim_metric.csv",
                        "projection": "core.platform_semantic_concepts",
                    }
                ),
                _content_hash(concept),
                actor,
            ),
        )
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_PLATFORM_CONCEPT_DECLARED,
        provider_account="governance",
        connection_ref="",
        metadata={
            "scope": "platform",
            "concept_id": concept_id,
            "version_id": version_id,
            "name": concept.name,
            "value_type": concept.value_type,
            "additivity_class": concept.additivity_class,
            "derived_from": "dbt/seeds/dim_metric.csv",
        },
    )
    return {"concept_id": concept_id, "version_id": version_id, "name": concept.name}


def provision(
    conn,
    *,
    actor: str,
    dry_run: bool = False,
    repair_seeded: bool = False,
    mint: bool = True,
) -> dict[str, Any]:
    """Minter ce qui manque, laisser ce qui existe, rapporter ce qui diverge.

    `dry_run` repond a la meme question sans ecrire -- c est la forme que prend
    la porte.

    `mint` ne mint QUE ce que la portee plateforme peut publier. Ce qu elle ne
    peut pas -- une formule dont les operandes n existent que par nom -- part
    dans `withheld` avec le geste qui la rend disponible : l adopter dans un
    projet (`core.semantic_metric_presets`), ou chaque operande s epingle a une
    version exacte.

    `mint=False` REPARE SANS AGRANDIR, et les deux actes sont separes parce qu ils
    ne se decident pas ensemble : corriger un semis remet une definition existante
    sur ce que le catalogue livre declare deja, alors que minter un concept ajoute
    au vocabulaire que TOUT projet de l instance lit. Une session qui doit la
    premiere reparation ne doit pas etre forcee de prendre la seconde decision.
    """
    projected, refused = project_delivered_catalogue(load_dictionary_types(conn))
    stored = load_platform_concepts(conn)
    absent = [concept for concept in projected if concept.name not in stored]
    #  LE MINT NE PRODUIT QUE LA FORME PUBLIABLE. Un concept dont la formule
    #  porte un operande par NOM n a, a la portee plateforme, aucun projet contre
    #  lequel l epingler : le minter ecrirait une version `published` que rien ne
    #  peut compiler, ce que l amendement du 2026-08-25 de `governance.md` liste
    #  en `Incomplete if`. Il n est ni minte ni oublie : il est RETENU, avec le
    #  geste qui le rend disponible -- l adopter dans un projet.
    missing = [concept for concept in absent if concept.publishable_at_platform_scope]
    withheld = [
        withheld_from_platform_mint(concept)
        for concept in absent
        if not concept.publishable_at_platform_scope
    ]

    seeded, declared = classify_divergences(stored, projected)
    repairable = [
        concept
        for concept in projected
        if (stored.get(concept.name) or {}).get("untouched_seed")
        and any(concept.name + ":" in line for line in seeded)
        and concept.publishable_at_platform_scope
    ]
    report: dict[str, Any] = {
        "projected": [concept.name for concept in projected],
        "refused": refused,
        #  Ce que la portee plateforme ne peut pas publier, nomme avec son geste.
        #  Rapporte a chaque run, `dry_run` compris : un catalogue qui se tait sur
        #  ce qu il ne mint pas se lit comme un catalogue complet.
        "withheld": withheld,
        "present": sorted(stored),
        "minted": [],
        "repaired": [],
        #  Les deux moities, gardees SEPAREES : un semis que la migration a ecrit
        #  contre une table vide n est pas une decision qu on ecraserait, et une
        #  declaration humaine ne se corrige jamais toute seule.
        "seeded_divergences": seeded,
        "divergences": declared,
    }
    if dry_run:
        report["would_mint"] = [concept.name for concept in missing]
        #  JAMAIS conditionne a `repair_seeded` : la porte doit rougir sur un semis
        #  contradictoire meme quand l appelant n a pas demande a le reparer.
        report["would_repair"] = [concept.name for concept in repairable]
        return report

    if mint:
        for concept in missing:
            declare_platform_concept(conn, concept, actor=actor)
            report["minted"].append(concept.name)
    if repair_seeded:
        for concept in repairable:
            repair_seeded_concept(conn, concept, actor=actor)
            report["repaired"].append(concept.name)
    return report
