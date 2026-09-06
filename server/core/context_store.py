"""toorow -- Context layer domain service (Story 11.1).

Manages context_topics and procedures CRUD, YAML frontmatter validation,
version-append history in context_topics_versions / procedures_versions,
and AD-5 scope resolution.
"""

from __future__ import annotations

from typing import Any

import psycopg
import yaml
from ulid import ULID

from core.audit import (
    declare_action,
    insert_audit_row,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_CONTEXT_TOPIC_ARCHIVED = declare_action("context_topic.archived")
ACTION_CONTEXT_TOPIC_CREATED = declare_action("context_topic.created")
ACTION_CONTEXT_TOPIC_RESTORED = declare_action("context_topic.restored")
ACTION_CONTEXT_TOPIC_UPDATED = declare_action("context_topic.updated")
# `context_graph.edge.created` / `.deleted` are NOT declared here any more.
# Story 49-6 AC5 moved the gesture to `core.context_relationships`, which
# declares `context_relationship.created`, `.superseded` and `.restored` beside
# the code that writes them. They are new VALUES, not a rename: a retirement no
# longer destroys a row, it appends a supersession fact, and a restore did not
# exist at all. AD-42 forbids this module from declaring an action it no longer
# writes.
ACTION_PROCEDURE_ARCHIVED = declare_action("procedure.archived")
ACTION_PROCEDURE_CREATED = declare_action("procedure.created")
ACTION_PROCEDURE_RESTORED = declare_action("procedure.restored")
ACTION_PROCEDURE_UPDATED = declare_action("procedure.updated")



class DuplicateProcedureNameError(ValueError):
    """Raised when a procedure name already exists within the target project/platform scope."""

    pass


class StaleContextVersionError(ValueError):
    """Raised when a locked context row no longer matches the caller version."""

    pass


class ArchivedContextError(ValueError):
    """Raised when a normal PATCH tries to modify an archived context resource."""

    pass


class PayloadTooLargeError(ValueError):
    """Raised when a body_md or frontmatter_yaml payload exceeds its byte limit."""

    pass


class NotArchivedContextError(ValueError):
    """Raised when a restore is asked of a row that is not archived.

    A restore is not idempotent on purpose: answering "done" to a gesture that
    had nothing to undo would tell the caller an archive it never saw had been
    reversed. The message names the state, not the table.
    """

    pass


#: Ce qu'un pas FAIT, et donc l'icone que la console lui donne. Vocabulaire ferme,
#: fixe par Jean le 2026-08-03 : « 1 "Read" ca t'affiche une icone de read et la
#: description, 2 "Run" ca affiche la description, 3 "Edit" t'as un crayon et ca
#: montre ce que ca fait. Analyze, Suggest, Create Issue -> raise a github issue. »
#:
#: ⚠️ AXE DISTINCT DE `ai_path_steps.step_kind`. Celui-la dit ce qu'une execution
#: a fait AU CONTEXTE (`tool_call`, `knowledge_read`, `semantic_query`...) ; celui
#: -ci dit ce que la procedure fait AU TRAVAIL. Un meme pas est `run` ici et
#: `tool_call` la-bas. Les fondre en un seul champ ferait perdre l'une des deux
#: questions -- et c'est celle de l'observe qui sert la boucle d'amelioration.
STEP_ACTIONS = frozenset(
    {"read", "run", "edit", "analyze", "suggest", "create-issue"}
)

_MAX_STEPS = 50
_MAX_LABEL = 200
_MAX_TEXT = 400

#: Un critere d'acceptation est plus long qu'un libelle, et il doit rester
#: CITABLE MOT POUR MOT -- le tronquer changerait ce qu'il exige.
#:
#: La borne vient de la mesure, pas du gout : sur les 165 criteres << Incomplete
#: if >> ratifies du depot (2026-08-03), le plus long fait 652 caracteres --
#: `caveats-register.md`, une regle suivie de sa propre correction datee. Une
#: borne a 400 l'aurait refuse, et refuser un critere ratifie n'est pas une
#: rigueur, c'est une perte.
_MAX_CRITERION = 1000

#: Borne en OCTETS (pas en caracteres) sur les deux gros champs libres, mesuree
#: AVANT le parsing YAML et l'INSERT Postgres : aucun middleware HTTP ne borne
#: le corps des requetes (seul HostHeaderValidationMiddleware existe,
#: routing.py), donc la porte est ici, au niveau du store -- elle couvre REST
#: ET MCP d'un coup. La taille suit celle de `semantic_model_api.py`
#: (MAX_BODY_BYTES = 512 * 1024), seule borne de corps deja ratifiee du depot.
MAX_BODY_MD_BYTES = 512 * 1024

#: Le frontmatter est un mapping de metadonnees (name, description, steps...),
#: pas un document : 64 KiB est deja tres au-dela de tout frontmatter legitime,
#: et la mesure precede `yaml.safe_load`, dont le cout croit avec l'entree.
MAX_FRONTMATTER_BYTES = 64 * 1024


def _check_payload_size(value: Any, label: str, limit: int) -> None:
    if not isinstance(value, str):
        return
    if len(value.encode("utf-8")) > limit:
        raise PayloadTooLargeError(f"{label} exceeds the {limit}-byte limit.")


def _clean_text(value: Any, label: str, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    clean = value.strip()
    if len(clean) > limit:
        raise ValueError(f"{label} is too long (maximum {limit} characters).")
    return clean


def _validate_steps(value: Any) -> list[dict[str, Any]]:
    """La SEQUENCE d'une Skill -- ordonnee, et lisible par une interface.

    `tool_bindings` ne pouvait pas la porter : sa cle `tool` est reservee par
    `glossary.md` a un outil MCP, alors qu'un pas doit pouvoir nommer une
    COMMANDE (lancer le serveur, creer un credential, monter un environnement de
    test). Les deux coexistent donc : `tool_bindings` lie des outils MCP,
    `steps` decrit ce qu'on fait.

    `stop_if` est la branche d'echec. Elle existait deja dans la recette de tache
    (`daily_insights_recipe.py`, `"stopIf": "blocked"`) et nulle part dans une
    Skill -- une sequence qui ne peut pas dire « si ca echoue, arrete-toi la »
    n'est pas une procedure, c'est une liste.

    TROIS FACONS DE DIRE SUR QUOI LE PAS AGIT, et il en faut AU MOINS UNE :

        target    ce sur quoi on agit -- un fichier, un document, un ecran, une route
        command   une commande a lancer -- `npx vitest run`, `make dev`
        tool      un outil MCP, au sens strict du glossaire

    C'est la resolution de la collision de nom : `tool_bindings.tool` melangeait
    l'outil MCP et la commande sous une seule cle, ce qui obligeait a laisser les
    commandes en prose. Ici les trois sont distinctes.

    ET LA CONTRAINTE QUI REND UN PAS LISIBLE : un pas qui declare une action et un
    libelle sans rien sur quoi agir est exactement le pas vague qu'une interface
    ne peut pas rendre. Le schema le refuse.
    """
    if not isinstance(value, list):
        raise ValueError("'steps' must be a list.")
    if len(value) > _MAX_STEPS:
        raise ValueError(f"'steps' cannot contain more than {_MAX_STEPS} items.")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    previous = 0
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"steps[{index}] must be an attribute mapping.")
        unknown = set(raw) - {"step", "action", "label", "target", "command", "tool", "stop_if", "required"}
        if unknown:
            # `map(str, ...)` et pas `sorted(unknown)` : une cle YAML peut etre un
            # ENTIER (`2: oops`), et `join` levait alors un TypeError -- donc un
            # 500 a l'enregistrement au lieu d'un 422 lisible, et une levee dans
            # le lecteur qui promet de ne jamais lever.
            raise ValueError(
                f"steps[{index}] contains unsupported keys: "
                + ", ".join(sorted(map(str, unknown))) + "."
            )
        step = raw.get("step")
        if isinstance(step, bool) or not isinstance(step, int) or step < 1:
            raise ValueError(f"steps[{index}].step must be a positive integer.")
        if step in seen:
            raise ValueError(f"steps step {step} is duplicated.")
        if step < previous:
            raise ValueError("'steps' must be ordered by step.")
        action = raw.get("action")
        if action not in STEP_ACTIONS:
            raise ValueError(
                f"steps[{index}].action must be one of " + ", ".join(sorted(STEP_ACTIONS)) + "."
            )
        entry: dict[str, Any] = {
            "step": step,
            "action": action,
            "label": _clean_text(raw.get("label"), f"steps[{index}].label", limit=_MAX_LABEL),
        }
        for optional in ("target", "command", "tool", "stop_if"):
            if raw.get(optional) is not None:
                entry[optional] = _clean_text(raw[optional], f"steps[{index}].{optional}")
        if not any(key in entry for key in ("target", "command", "tool")):
            raise ValueError(
                f"steps[{index}] must declare what it acts on: "
                "one of target, command or tool."
            )
        # 2026-09-05 -- A REQUIRED STEP IS A PROMISE SOMEBODY CAN CHECK. The
        # recorder observes tool calls and nothing else, so a step declared
        # required must name its tool; a required read of a target would be a
        # requirement no path could ever prove kept or broken.
        required = raw.get("required")
        if required is not None:
            if not isinstance(required, bool):
                raise ValueError(f"steps[{index}].required must be true or false.")
            if required and "tool" not in entry:
                raise ValueError(
                    f"steps[{index}] is required but names no tool: a required step is one "
                    "the recorder can observe."
                )
            entry["required"] = required
        normalized.append(entry)
        seen.add(step)
        previous = step
    return normalized


def _validate_keywords(value: Any) -> list[str]:
    """Les mots-cles LIBRES d'une Skill -- et la raison pour laquelle ils existent.

    `mdm_tags` nomme des metriques GOUVERNEES : la console l'intitule
    « MDM tags — Canonical metrics » et le lie par cases a cocher a
    `app.target_fields`. Mais faute d'un champ libre, tout le monde s'en servait
    comme d'une folksonomie -- moi le 2026-08-03, et la procedure que le produit
    seme lui-meme (migration 196 : `platform-operating-procedure`, `datastream`,
    `context`, `testing`, `measurement`, dont AUCUN n'est un champ du catalogue).

    Tant que le seul champ de liste servait deux usages, `mdm_tags` ne pouvait
    pas etre valide : le rendre strict aurait rendu la procedure du produit non
    modifiable. Separer les deux debloque la validation ET conserve le service
    rendu -- depuis que le frontmatter est indexe, un mot-cle sert vraiment le
    rappel.
    """
    if not isinstance(value, list):
        raise ValueError("'keywords' must be a list.")
    if len(value) > _MAX_STEPS:
        raise ValueError(f"'keywords' cannot contain more than {_MAX_STEPS} items.")
    out: list[str] = []
    for index, raw in enumerate(value):
        clean = _clean_text(raw, f"keywords[{index}]", limit=_MAX_LABEL)
        if clean in out:
            raise ValueError(f"keywords contains duplicate value '{clean}'.")
        out.append(clean)
    return out


def _validate_acceptance(value: Any) -> list[str]:
    """Les criteres d'acceptation, dans la Skill et non a cote.

    La cible les nomme deja deux fois -- « Skill | [...] applicable steps, tool
    bindings and EVIDENCE REQUIREMENTS » (`analyze-and-test.md`) et le
    « Incomplete if » de 22 documents. Aucune des deux n'etait exprimable dans
    une Skill : le frontmatter refusait toute cle qu'il ne connaissait pas.
    """
    if not isinstance(value, list):
        raise ValueError("'acceptance' must be a list.")
    if len(value) > _MAX_STEPS:
        raise ValueError(f"'acceptance' cannot contain more than {_MAX_STEPS} items.")
    out: list[str] = []
    for index, raw in enumerate(value):
        clean = _clean_text(raw, f"acceptance[{index}]", limit=_MAX_CRITERION)
        if clean in out:
            raise ValueError(f"acceptance contains duplicate value '{clean}'.")
        out.append(clean)
    return out


def _validate_common_errors(value: Any) -> list[dict[str, Any]]:
    """Symptome -> causes probables. Ce qui manquait pour qu'un echec soit lisible.

    Mesure du 2026-08-03 : sur les 50 `SKILL.md` du depot, UN SEUL portait une
    section de ce genre, et en prose. Rien ne reliait un symptome a ses causes,
    donc rien n'etait cherchable, comptable, ni rendu par une interface.
    """
    if not isinstance(value, list):
        raise ValueError("'common_errors' must be a list.")
    if len(value) > _MAX_STEPS:
        raise ValueError(f"'common_errors' cannot contain more than {_MAX_STEPS} items.")
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"common_errors[{index}] must be an attribute mapping.")
        unknown = set(raw) - {"symptom", "causes"}
        if unknown:
            raise ValueError(
                f"common_errors[{index}] contains unsupported keys: "
                + ", ".join(sorted(map(str, unknown)))
                + "."
            )
        causes = raw.get("causes")
        if not isinstance(causes, list) or not causes:
            raise ValueError(f"common_errors[{index}].causes must be a non-empty list.")
        out.append({
            "symptom": _clean_text(
                raw.get("symptom"), f"common_errors[{index}].symptom", limit=_MAX_LABEL
            ),
            "causes": [
                _clean_text(cause, f"common_errors[{index}].causes[{position}]")
                for position, cause in enumerate(causes)
            ],
        })
    return out


def _validate_anti_triggers(value: Any) -> list[str]:
    """Quand une Skill ne doit PAS se declencher -- Story 45.5.

    POURQUOI. Jean, 2026-08-03 : « **Should NOT trigger.** » Mesure du
    2026-08-05 : aucune des 50 `SKILL.md` du depot ne le disait, aucune cle ne
    l'acceptait, et le classement n'avait que des tiers POSITIFS -- une Skill a
    description large gagnait donc des questions qui ne sont pas les siennes et
    rien ne pouvait la contredire.

    LA FORME EST UN TERME, PAS UNE PHRASE, et c'est ce qui la rend applicable :
    le classement compare des sous-chaines (ILIKE), donc un anti-declencheur qui
    serait une phrase entiere ne correspondrait jamais a rien. « ne pas utiliser
    pour les questions de facturation » ne retire rien ; « facturation » si.

    La longueur est bornee comme un mot-cle pour la meme raison.
    """
    if not isinstance(value, list):
        raise ValueError("'anti_triggers' must be a list.")
    if len(value) > _MAX_STEPS:
        raise ValueError(f"'anti_triggers' cannot contain more than {_MAX_STEPS} items.")
    out: list[str] = []
    for index, raw in enumerate(value):
        clean = _clean_text(raw, f"anti_triggers[{index}]", limit=_MAX_LABEL)
        lowered = clean.lower()
        if lowered in [item.lower() for item in out]:
            raise ValueError(f"anti_triggers contains duplicate value '{clean}'.")
        out.append(clean)
    return out


def _validate_evidence(value: Any, key_name: str = "evidence") -> list[str]:
    """Les exigences de preuve ('evidence requirements') d'une Skill."""
    if not isinstance(value, list):
        raise ValueError(f"'{key_name}' must be a list.")
    if len(value) > _MAX_STEPS:
        raise ValueError(f"'{key_name}' cannot contain more than {_MAX_STEPS} items.")
    out: list[str] = []
    for index, raw in enumerate(value):
        clean = _clean_text(raw, f"{key_name}[{index}]", limit=_MAX_CRITERION)
        if clean in out:
            raise ValueError(f"{key_name} contains duplicate value '{clean}'.")
        out.append(clean)
    return out


ALLOWED_FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "description",
        "tool_bindings",
        "mdm_tags",
        "steps",
        "acceptance",
        "common_errors",
        "keywords",
        "evidence",
        "evidence_requirements",
        "anti_triggers",
    }
)


#: Les cles standardisees et LEUR validateur -- une seule table, lue par
#: l'ecriture (`validate_procedure_frontmatter`) et par la lecture
#: (`read_standard_frontmatter`). Deux tables auraient fait deux dialectes de la
#: meme cle : c'est exactement le defaut que la vague du 2026-08-05 vient de
#: fermer trois fois.
STANDARD_VALIDATORS: tuple[tuple[str, Any], ...] = (
    ("steps", _validate_steps),
    ("acceptance", _validate_acceptance),
    ("common_errors", _validate_common_errors),
    ("keywords", _validate_keywords),
    ("evidence", lambda v: _validate_evidence(v, "evidence")),
    ("evidence_requirements", lambda v: _validate_evidence(v, "evidence_requirements")),
    ("anti_triggers", _validate_anti_triggers),
)


def validate_procedure_frontmatter(yaml_text: str) -> dict[str, Any]:
    """Validate procedure YAML frontmatter.

    Requires a YAML mapping containing both 'name' (non-empty string) and
    'description' (string). Additional keys outside ALLOWED_FRONTMATTER_KEYS are rejected.
    Raises ValueError with an English message on failure.
    """
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise ValueError("YAML frontmatter is empty or invalid.")
    # Avant `safe_load` : refuser la taille d'abord, parser ensuite.
    _check_payload_size(yaml_text, "frontmatter_yaml", MAX_FRONTMATTER_BYTES)

    try:
        data = yaml.safe_load(yaml_text)
    except Exception as exc:
        raise ValueError(f"YAML frontmatter could not be parsed: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("YAML frontmatter must be an attribute mapping.")

    unknown = set(data) - ALLOWED_FRONTMATTER_KEYS
    if unknown:
        # ⚠️ `map(str, ...)` -- UNE CLE YAML N'EST PAS FORCEMENT UNE CHAINE.
        # `2: oops` donne la cle ENTIERE 2, `sorted` la range, et `join` levait
        # un `TypeError` que `context_api.py` rend en 500 `db_error` la ou le
        # contrat promet 422 `frontmatter_invalide` (relecture du 2026-08-05 :
        # la reparation « aux deux endroits » n'en couvrait qu'un). Un code
        # d'erreur promis puis non rendu est un contrat rompu, pas un detail.
        raise ValueError(
            "YAML frontmatter contains unsupported keys: "
            + ", ".join(sorted(map(str, unknown)))
            + "."
        )

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("YAML frontmatter must contain a non-empty string 'name'.")

    description = data.get("description")
    if not isinstance(description, str):
        raise ValueError(
            "YAML frontmatter must contain a string 'description'."
        )

    tool_bindings = data.get("tool_bindings")
    if tool_bindings is not None:
        if not isinstance(tool_bindings, list):
            raise ValueError("'tool_bindings' must be a list.")
        if len(tool_bindings) > 50:
            raise ValueError("'tool_bindings' cannot contain more than 50 items.")
        normalized_bindings: list[dict[str, Any]] = []
        seen_steps: set[int] = set()
        previous_step = 0
        for index, binding in enumerate(tool_bindings):
            if not isinstance(binding, dict):
                raise ValueError(f"tool_bindings[{index}] must be an attribute mapping.")
            unknown_b = set(binding) - {"step", "tool", "viz_tag"}
            if unknown_b:
                # Meme cause, meme reparation : la cle d'un mapping YAML imbrique
                # peut etre un entier, une date ou un booleen.
                raise ValueError(
                    f"tool_bindings[{index}] contains unsupported keys: "
                    + ", ".join(sorted(map(str, unknown_b)))
                    + "."
                )
            step = binding.get("step")
            if isinstance(step, bool) or not isinstance(step, int) or step < 1:
                raise ValueError(f"tool_bindings[{index}].step must be a positive integer.")
            if step in seen_steps:
                raise ValueError(f"tool_bindings step {step} is duplicated.")
            if step < previous_step:
                raise ValueError("'tool_bindings' must be ordered by step.")
            tool = binding.get("tool")
            if not isinstance(tool, str) or not tool.strip():
                raise ValueError(f"tool_bindings[{index}].tool must be a non-empty string.")
            clean_tool = tool.strip()
            if len(clean_tool) > 160:
                raise ValueError(f"tool_bindings[{index}].tool is too long.")
            normalized: dict[str, Any] = {"step": step, "tool": clean_tool}
            if "viz_tag" in binding and binding["viz_tag"] is not None:
                viz_tag = binding["viz_tag"]
                if not isinstance(viz_tag, str) or not viz_tag.strip():
                    raise ValueError(
                        f"tool_bindings[{index}].viz_tag must be a non-empty string or null."
                    )
                clean_viz_tag = viz_tag.strip()
                if len(clean_viz_tag) > 120:
                    raise ValueError(f"tool_bindings[{index}].viz_tag is too long.")
                normalized["viz_tag"] = clean_viz_tag
            normalized_bindings.append(normalized)
            seen_steps.add(step)
            previous_step = step
        tool_bindings = normalized_bindings

    mdm_tags = data.get("mdm_tags")
    if mdm_tags is not None:
        if not isinstance(mdm_tags, list):
            raise ValueError("'mdm_tags' must be a list.")
        if len(mdm_tags) > 50:
            raise ValueError("'mdm_tags' cannot contain more than 50 items.")
        normalized_tags: list[str] = []
        seen_tags: set[str] = set()
        for index, tag in enumerate(mdm_tags):
            if not isinstance(tag, str) or not tag.strip():
                raise ValueError(f"mdm_tags[{index}] must be a non-empty string.")
            clean_tag = tag.strip()
            if len(clean_tag) > 160:
                raise ValueError(f"mdm_tags[{index}] is too long.")
            if clean_tag in seen_tags:
                raise ValueError(f"mdm_tags contains duplicate value '{clean_tag}'.")
            normalized_tags.append(clean_tag)
            seen_tags.add(clean_tag)
        mdm_tags = normalized_tags

    parsed = dict(data)
    parsed["name"] = name.strip()
    parsed["description"] = description
    if tool_bindings is not None:
        parsed["tool_bindings"] = tool_bindings
    if mdm_tags is not None:
        parsed["mdm_tags"] = mdm_tags
    for key, validate in STANDARD_VALIDATORS:
        if data.get(key) is not None:
            parsed[key] = validate(data[key])
    return parsed


def read_anti_triggers(frontmatter_yaml: str | None) -> list[str]:
    """Lire les anti-declencheurs d'une Skill SANS jamais lever -- Story 45.5.

    Le classement appelle ceci sur chaque candidat atteint. Une Skill dont le
    frontmatter serait illisible ne doit pas faire tomber la recherche de tout le
    monde : elle declare simplement zero anti-declencheur, ce qui la laisse
    exactement ou elle etait avant cette story.

    C'est ici et pas dans `context_search` parce que la forme du frontmatter
    appartient a ce module -- deux lecteurs de la meme cle divergeraient.
    """
    raw = (_safe_mapping(frontmatter_yaml) or {}).get("anti_triggers")
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def searchable_frontmatter(frontmatter_yaml: str | None) -> str:
    """Le frontmatter MOINS ses anti-declencheurs -- Story 45.5.

    POURQUOI CETTE FONCTION EXISTE. `context_search` compare `frontmatter_yaml`
    par ILIKE depuis AI-154, anti-declencheurs compris. Une Skill dont le seul
    « billing » vit sous `anti_triggers:` remontait donc sur la requete
    « bill », et l'extrait servi au lecteur etait la ligne d'exclusion : le mot
    qui exclut faisait entrer.

    ELLE PASSE PAR `_safe_mapping`, LE LECTEUR DE CE MODULE, et pas par un
    decoupage ligne a ligne. Un second dialecte de la meme cle est le defaut que
    ce fichier passe ses journees a fermer.

    Un frontmatter ILLISIBLE, ou sans anti-declencheur, est rendu INCHANGE : ne
    rien pouvoir retirer n'est pas une raison de perdre la ligne.
    """
    data = _safe_mapping(frontmatter_yaml)
    if data is None or "anti_triggers" not in data:
        return frontmatter_yaml or ""
    rest = {key: value for key, value in data.items() if key != "anti_triggers"}
    if not rest:
        return ""
    return yaml.safe_dump(rest, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _safe_mapping(frontmatter_yaml: str | None) -> dict[str, Any] | None:
    """Le frontmatter en dictionnaire, `None` s'il ne se LIT pas. Ne leve jamais.

    LES DEUX NE SONT PAS LA MEME CHOSE, et les confondre etait le defaut que ce
    module existe pour empecher : un frontmatter vide, ou reduit a un commentaire,
    se lit parfaitement et ne porte rien -- le rendre « illisible » ferait dire au
    produit qu'il n'a pas pu lire ce qu'il vient de lire.
    """
    if not frontmatter_yaml or not frontmatter_yaml.strip():
        return {}
    try:
        data = yaml.safe_load(frontmatter_yaml)
    except Exception:
        return None
    if data is None:
        # Un commentaire seul, ou des espaces : YAML rend `None` et c'est un
        # document VIDE, pas un document casse.
        return {}
    return data if isinstance(data, dict) else None


def read_standard_frontmatter(frontmatter_yaml: str | None) -> dict[str, Any]:
    """La forme standardisee d'une Skill, LUE -- Story 45.6.

    POURQUOI CETTE FONCTION EXISTE. `get_procedure` rendait `frontmatter_yaml`
    en BLOC DE TEXTE : la console lisait la sequence, l'acceptation, les erreurs
    courantes et l'anti-declencheur, et l'agent -- celui qui APPLIQUE la
    procedure -- devait parser du YAML lui-meme. La standardisation avait ete
    faite pour lui et il etait le seul a ne pas l'avoir.

    ELLE PASSE PAR LES MEMES VALIDATEURS QUE L'ECRITURE (`STANDARD_VALIDATORS`).
    Un second lecteur de la meme cle divergerait du premier, et c'est le defaut
    que cette vague ferme partout ailleurs.

    ELLE NE LEVE JAMAIS, et la distinction compte : une cle ILLISIBLE n'est pas
    une cle ABSENTE. `readable` est False quand le YAML entier ne se lit pas ;
    `unreadable_keys` nomme les cles presentes dont la forme est refusee. Rendre
    une liste vide dans ces deux cas ferait lire « cette Skill n'a pas de
    sequence » la ou la verite est « on n'a pas pu la lire » -- exactement le
    mensonge qu'AI-158 a corrige sur le magasin de contexte.
    """
    out: dict[str, Any] = {
        "readable": True,
        "unreadable_keys": [],
        **{key: [] for key, _ in STANDARD_VALIDATORS},
    }
    data = _safe_mapping(frontmatter_yaml)
    if data is None:
        out["readable"] = False
        return out
    for key, validate in STANDARD_VALIDATORS:
        raw = data.get(key)
        if raw is None:
            continue
        try:
            out[key] = validate(raw)
        except Exception:  # noqa: BLE001
            # `ValueError` etait trop etroit : un validateur pouvait lever autre
            # chose (une cle YAML entiere rendait un `TypeError`), et la levee
            # remontait jusqu'a l'outil MCP -- l'agent perdait la Skill ENTIERE,
            # corps et YAML brut compris. Un lecteur qui promet de ne jamais lever
            # doit tenir sa promesse contre le validateur qu'on ecrira demain.
            out["unreadable_keys"].append(key)
    return out


def assert_mdm_tags_resolve(
    conn: Any, mdm_tags: list[str] | None, project_id: str | None = None
) -> None:
    """`mdm_tags` nomme des metriques GOUVERNEES -- sinon il ne sert a rien.

    POURQUOI CETTE PORTE N'EXISTAIT PAS AVANT AUJOURD'HUI, et pourquoi elle peut
    exister maintenant. Le champ servait deux usages faute d'un champ libre :
    des metriques du catalogue ET des mots-cles. Le rendre strict aurait rendu la
    procedure semee par le produit non modifiable -- une validation qui casse la
    donnee du produit n'est pas une rigueur, c'est une panne. La migration 203 a
    separe les deux (`keywords`), donc la stricte devient tenable.

    ELLE REFUSE EN NOMMANT CE QU'IL FAUT FAIRE, comme les autres portes de ce
    depot : le tag inconnu, et ou vit le catalogue. Un refus qui laisse chercher
    est un refus qu'on apprend a contourner.

    `validate_procedure_frontmatter` reste PURE -- sans base. Ce controle vit
    donc ici, dans la couche qui a une connexion, et couvre toutes les voies
    d'ecriture d'un coup.

    MODELE SEMANTIQUE D'ABORD (story 49.3, etape des lecteurs). Cette porte
    interrogeait `app.target_fields` seule et renvoyait a `GET /api/datamodel
    /fields` -- un catalogue dont les cinq portes d'ecriture repondent 409
    `legacy_store_is_read_only` depuis le 2026-08-25. Une metrique publiee au
    workbench de Concepts etait donc refusee ici, et le refus nommait le seul
    endroit ou l'on ne peut plus rien ajouter. `governed_field_catalogue` lit le
    successeur d'abord et le dictionnaire en repli : l'ensemble accepte
    s'ELARGIT, aucun tag qui passait ne cesse de passer, et le catalogue
    qu'accepte cette porte est EXACTEMENT celui que `mdm_references` resout a la
    lecture -- une Skill ne peut pas porter un tag que sa propre lecture rendrait
    `unresolved`.
    """
    tags = [tag for tag in (mdm_tags or []) if tag]
    if not tags:
        return
    from core.governed_field_catalogue import governed_names  # noqa: PLC0415

    known = governed_names(conn, names=tags, project_id=project_id)
    unknown = [tag for tag in tags if tag not in known]
    if unknown:
        raise ValueError(
            "mdm_tags must name governed fields; unknown: "
            + ", ".join(sorted(unknown))
            + ". Free keywords belong in 'keywords'; declare a new field as a "
            "Concept in the Semantic Model."
        )


def _clean_owner(value: Any) -> str | None:
    """Validate a patchable ``owner`` value: string-or-null, trimmed.

    Empty/whitespace-only strings collapse to None (an explicitly cleared
    owner), matching the "explicit owner, else created_by, else 'auto'"
    resolution rule -- an empty string is not a meaningful explicit owner.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("owner must be a string or null.")
    trimmed = value.strip()
    return trimmed or None


def _format_datetime(dt: Any) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _row_to_topic(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    """AI-219: the type decides. Three names could not cover a fourth column."""
    from core.row_json import row_to_json  # noqa: PLC0415

    return row_to_json(cols, row)


# ---------------------------------------------------------------------------
# Topics Domain Logic
# ---------------------------------------------------------------------------


def create_topic(
    conn: Any,
    *,
    project_id: str | None,
    title: str,
    body_md: str = "",
    owner: str | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Create a context topic and append version 1."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Topic title cannot be empty.")
    _check_payload_size(body_md, "body_md", MAX_BODY_MD_BYTES)

    topic_id = f"top_{ULID()}"
    clean_title = title.strip()
    clean_owner = _clean_owner(owner)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_topics
                (id, project_id, title, body_md, status, owner, created_by, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, 'active', %s, %s, now(), now())
            RETURNING
                id, project_id, title, body_md, status, owner, created_by, created_at, updated_at
            """,
            (topic_id, project_id, clean_title, body_md, clean_owner, created_by),
        )
        cols = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        topic = _row_to_topic(row, cols)

        cur.execute(
            """
            INSERT INTO app.context_topics_versions
                (topic_id, project_id, title, body_md, status, owner, created_by, created_at,
                 updated_at, version_number, changed_by, changed_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, 1, %s, now())
            """,
            (
                topic["id"],
                topic["project_id"],
                topic["title"],
                topic["body_md"],
                topic["status"],
                topic["owner"],
                topic["created_by"],
                topic["created_at"],
                topic["updated_at"],
                created_by,
            ),
        )

        insert_audit_row(
            conn,
            identity=created_by,
            action=ACTION_CONTEXT_TOPIC_CREATED,
            provider_account="platform",
            connection_ref="",
            metadata={
                "topic_id": topic_id,
                "project_id": project_id,
                "title": clean_title,
                "version_number": 1,
            },
        )

    topic["version_number"] = 1
    return topic


def update_topic(
    conn: Any,
    *,
    topic_id: str,
    patch: dict[str, Any],
    changed_by: str,
    expected_version: int | None = None,
    restoring: bool = False,
) -> dict[str, Any]:
    """Update a topic and append a new version row.

    ``restoring`` is the ONE write an archived row accepts: it is set only by
    `restore_topic`, it is checked under the same ``FOR UPDATE`` lock as the
    version precondition, and it appends a version and an audit row exactly as
    an archive does.
    """
    if patch.get("body_md") is not None:
        _check_payload_size(patch["body_md"], "body_md", MAX_BODY_MD_BYTES)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, title, body_md, status, owner, created_by, created_at, updated_at
            FROM app.context_topics
            WHERE id = %s
            FOR UPDATE
            """,
            (topic_id,),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(f"Topic '{topic_id}' not found.")

        cols = [desc[0] for desc in cur.description]
        current = _row_to_topic(row, cols)

        new_title = patch.get("title", current["title"])
        if isinstance(new_title, str):
            new_title = new_title.strip()
        if not new_title:
            raise ValueError("Topic title cannot be empty.")

        new_body = patch["body_md"] if patch.get("body_md") is not None else current["body_md"]
        new_status = patch.get("status", current["status"])
        if new_status not in ("active", "archived"):
            raise ValueError("Invalid status.")
        new_owner = _clean_owner(patch["owner"]) if "owner" in patch else current["owner"]

        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 1)
            FROM app.context_topics_versions
            WHERE topic_id = %s
            """,
            (topic_id,),
        )
        max_ver_row = cur.fetchone()
        current_version = int(max_ver_row[0]) if max_ver_row else 1
        if expected_version is not None and expected_version != current_version:
            raise StaleContextVersionError(
                f"Expected version {expected_version}, current version {current_version}."
            )

        if restoring and current["status"] != "archived":
            raise NotArchivedContextError(
                "This topic is not archived, so there is nothing to restore."
            )

        if current["status"] == "archived" and not restoring:
            if patch == {"status": "archived"}:
                current["version_number"] = current_version
                return current
            raise ArchivedContextError("An archived topic cannot be modified.")

        next_ver = current_version + 1

        cur.execute(
            """
            UPDATE app.context_topics
            SET title = %s, body_md = %s, status = %s, owner = %s, updated_at = now()
            WHERE id = %s
            RETURNING
                id, project_id, title, body_md, status, owner, created_by, created_at, updated_at
            """,
            (new_title, new_body, new_status, new_owner, topic_id),
        )
        updated_row = cur.fetchone()
        updated_topic = _row_to_topic(updated_row, cols)

        cur.execute(
            """
            INSERT INTO app.context_topics_versions
                (topic_id, project_id, title, body_md, status, owner, created_by, created_at,
                 updated_at, version_number, changed_by, changed_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s, %s, now())
            """,
            (
                updated_topic["id"],
                updated_topic["project_id"],
                updated_topic["title"],
                updated_topic["body_md"],
                updated_topic["status"],
                updated_topic["owner"],
                updated_topic["created_by"],
                updated_topic["created_at"],
                updated_topic["updated_at"],
                next_ver,
                changed_by,
            ),
        )

        if new_status == "archived" and current["status"] != "archived":
            action = ACTION_CONTEXT_TOPIC_ARCHIVED
        elif restoring:
            action = ACTION_CONTEXT_TOPIC_RESTORED
        else:
            action = ACTION_CONTEXT_TOPIC_UPDATED

        insert_audit_row(
            conn,
            identity=changed_by,
            action=action,
            provider_account="platform",
            connection_ref="",
            metadata={
                "topic_id": topic_id,
                "project_id": updated_topic["project_id"],
                "title": updated_topic["title"],
                "version_number": next_ver,
            },
        )

    updated_topic["version_number"] = next_ver
    return updated_topic


def archive_topic(
    conn: Any, *, topic_id: str, changed_by: str, expected_version: int | None = None
) -> dict[str, Any]:
    """Archive a topic with the same locked version precondition as PATCH."""
    return update_topic(
        conn,
        topic_id=topic_id,
        patch={"status": "archived"},
        changed_by=changed_by,
        expected_version=expected_version,
    )


def restore_topic(
    conn: Any, *, topic_id: str, changed_by: str, expected_version: int | None = None
) -> dict[str, Any]:
    """Bring an archived topic back, with archive's own version and audit trail.

    Archiving is a version, not a deletion, so undoing it is a version too: the
    row returns to `active`, a new version row is appended, and the audit reads
    `context_topic.restored`. A topic that is not archived is refused rather
    than answered "done".
    """
    return update_topic(
        conn,
        topic_id=topic_id,
        patch={"status": "active"},
        changed_by=changed_by,
        expected_version=expected_version,
        restoring=True,
    )


def get_topic(
    conn: Any, *, topic_id: str, caller_project_id: str | None = None
) -> dict[str, Any] | None:
    """Fetch topic by ID with version number, enforcing scope."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                t.id, t.project_id, t.title, t.body_md, t.status, t.owner,
                t.created_by, t.created_at, t.updated_at,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.context_topics t
            LEFT JOIN app.context_topics_versions v ON t.id = v.topic_id
            WHERE t.id = %s
            GROUP BY
                t.id, t.project_id, t.title, t.body_md, t.status, t.owner,
                t.created_by, t.created_at, t.updated_at
            """,
            (topic_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        cols = [desc[0] for desc in cur.description]
        topic = _row_to_topic(row, cols)

        # AD-5 Scope check: platform topics (project_id IS NULL) readable by all;
        # project-scoped topics readable only if caller_project_id matches.
        if (
            caller_project_id is not None
            and topic["project_id"] is not None
            and topic["project_id"] != caller_project_id
        ):
            return None

        return topic


def list_topic_versions(
    conn: Any, *, topic_id: str, caller_project_id: str | None = None
) -> list[dict[str, Any]]:
    """Return immutable topic snapshots visible in the caller project scope."""
    with conn.cursor() as cur:
        if caller_project_id is None:
            cur.execute(
                """
                SELECT topic_id, project_id, title, body_md, status, owner,
                       created_by, created_at, updated_at, version_number,
                       changed_by, changed_at
                FROM app.context_topics_versions
                WHERE topic_id = %s
                ORDER BY version_number DESC
                """,
                (topic_id,),
            )
        else:
            cur.execute(
                """
                SELECT topic_id, project_id, title, body_md, status, owner,
                       created_by, created_at, updated_at, version_number,
                       changed_by, changed_at
                FROM app.context_topics_versions
                WHERE topic_id = %s
                  AND (project_id IS NULL OR project_id = %s)
                ORDER BY version_number DESC
                """,
                (topic_id, caller_project_id),
            )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_topic(row, cols) for row in cur.fetchall()]


def list_topics(
    conn: Any, *, project_id: str | None, status: str = "active"
) -> list[dict[str, Any]]:
    """List topics visible to project_id (platform + project) with version numbers.

    ``status`` is the strict lifecycle filter the REST list exposes: "active"
    (default), "archived" or "all" (no filter). One contract, one place --
    the previous boolean made ``status=archived`` mean "everything".
    """
    where_clauses = ["(t.project_id IS NULL OR t.project_id = %s)"]
    params: list[Any] = [project_id]

    if status == "active":
        where_clauses.append("t.status = 'active'")
    elif status == "archived":
        where_clauses.append("t.status = 'archived'")
    elif status != "all":
        raise ValueError(f"unknown topic status filter: {status}")

    where_sql = " WHERE " + " AND ".join(where_clauses)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                t.id, t.project_id, t.title, t.body_md, t.status, t.owner,
                t.created_by, t.created_at, t.updated_at,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.context_topics t
            LEFT JOIN app.context_topics_versions v ON t.id = v.topic_id
            {where_sql}
            GROUP BY
                t.id, t.project_id, t.title, t.body_md, t.status, t.owner,
                t.created_by, t.created_at, t.updated_at
            ORDER BY t.created_at DESC
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_topic(row, cols) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Procedures Domain Logic
# ---------------------------------------------------------------------------


def create_procedure(
    conn: Any,
    *,
    project_id: str | None,
    frontmatter_yaml: str,
    body_md: str = "",
    owner: str | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Create a procedure, validating frontmatter and appending version 1."""
    fm = validate_procedure_frontmatter(frontmatter_yaml)
    _check_payload_size(body_md, "body_md", MAX_BODY_MD_BYTES)
    assert_mdm_tags_resolve(conn, fm.get("mdm_tags"), project_id)
    name = fm["name"]
    description = fm["description"]

    proc_id = f"proc_{ULID()}"
    clean_owner = _clean_owner(owner)

    try:
        with conn.cursor() as cur:
            # NOTE: We do NOT pre-check for name duplicates here.
            # The partial unique indexes (uq_procedures_name_platform / uq_procedures_name_project)
            # exclude archived rows (WHERE status != 'archived'), so a manual SELECT-based check
            # would wrongly 409 on reuse of an archived name. We rely solely on the DB
            # UniqueViolation catch below, which respects the partial index exactly.

            cur.execute(
                """
                INSERT INTO app.procedures (
                    id, project_id, name, description, frontmatter_yaml,
                    body_md, status, owner, created_by, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s, now(), now())
                RETURNING
                    id, project_id, name, description, frontmatter_yaml,
                    body_md, status, owner, created_by, created_at, updated_at
                """,
                (
                    proc_id,
                    project_id,
                    name,
                    description,
                    frontmatter_yaml,
                    body_md,
                    clean_owner,
                    created_by,
                ),
            )
            cols = [desc[0] for desc in cur.description]
            row = cur.fetchone()
            proc = _row_to_topic(row, cols)

            cur.execute(
                """
                INSERT INTO app.procedures_versions
                    (procedure_id, project_id, name, description, frontmatter_yaml, body_md,
                     status, owner, created_by, created_at, updated_at, version_number,
                     changed_by, changed_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz,
                     1, %s, now())
                """,
                (
                    proc["id"],
                    proc["project_id"],
                    proc["name"],
                    proc["description"],
                    proc["frontmatter_yaml"],
                    proc["body_md"],
                    proc["status"],
                    proc["owner"],
                    proc["created_by"],
                    proc["created_at"],
                    proc["updated_at"],
                    created_by,
                ),
            )

            insert_audit_row(
                conn,
                identity=created_by,
                action=ACTION_PROCEDURE_CREATED,
                provider_account="platform",
                connection_ref="",
                metadata={
                    "procedure_id": proc_id,
                    "project_id": project_id,
                    "name": name,
                    "version_number": 1,
                },
            )
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateProcedureNameError(
            "A procedure with this name already exists in this scope."
        ) from exc

    proc["version_number"] = 1
    return proc


def update_procedure(
    conn: Any,
    *,
    procedure_id: str,
    patch: dict[str, Any],
    changed_by: str,
    expected_version: int | None = None,
    restoring: bool = False,
) -> dict[str, Any]:
    """Update a procedure and append a new version row.

    ``restoring`` is the ONE write an archived row accepts -- see
    `update_topic`. The name check above still applies: the partial unique
    index only covers non-archived rows, so a name taken while this Skill was
    archived refuses the restore with `DuplicateProcedureNameError`.
    """
    if patch.get("body_md") is not None:
        _check_payload_size(patch["body_md"], "body_md", MAX_BODY_MD_BYTES)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, project_id, name, description, frontmatter_yaml,
                    body_md, status, owner, created_by, created_at, updated_at
                FROM app.procedures
                WHERE id = %s
                FOR UPDATE
                """,
                (procedure_id,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Procedure '{procedure_id}' not found.")

            cols = [desc[0] for desc in cur.description]
            current = _row_to_topic(row, cols)

            new_fm_yaml = patch.get("frontmatter_yaml", current["frontmatter_yaml"])
            fm = validate_procedure_frontmatter(new_fm_yaml)
            # La meme porte qu'a la creation : une Skill ne doit pas pouvoir
            # ACQUERIR par modification un tag que la creation aurait refuse.
            # Le Projet est celui de la Skill, pas celui de l'appelant : c'est
            # son Modele Semantique qui dit ce que ses tags peuvent nommer.
            assert_mdm_tags_resolve(conn, fm.get("mdm_tags"), current["project_id"])
            new_name = fm["name"]
            new_desc = fm["description"]

            if new_name != current["name"] or restoring:
                # Check duplicate name, excluding archived rows to match
                # partial unique index semantics. A RESTORE re-enters that
                # index under an unchanged name, so it is checked here too:
                # while this Skill was archived, its name may have been taken.
                p_id = current["project_id"]
                if p_id is None:
                    cur.execute(
                        """
                        SELECT 1 FROM app.procedures
                        WHERE name = %s AND project_id IS NULL AND id != %s
                          AND status != 'archived'
                        """,
                        (new_name, procedure_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT 1 FROM app.procedures
                        WHERE name = %s AND project_id = %s AND id != %s
                          AND status != 'archived'
                        """,
                        (new_name, p_id, procedure_id),
                    )
                if cur.fetchone():
                    raise DuplicateProcedureNameError(
                        "A procedure with this name already exists in this scope."
                    )

            new_body = patch["body_md"] if patch.get("body_md") is not None else current["body_md"]
            new_status = patch.get("status", current["status"])
            if new_status not in ("active", "archived"):
                raise ValueError("Invalid status.")
            new_owner = _clean_owner(patch["owner"]) if "owner" in patch else current["owner"]

            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 1)
                FROM app.procedures_versions
                WHERE procedure_id = %s
                """,
                (procedure_id,),
            )
            max_ver_row = cur.fetchone()
            current_version = int(max_ver_row[0]) if max_ver_row else 1
            if expected_version is not None and expected_version != current_version:
                raise StaleContextVersionError(
                    f"Expected version {expected_version}, current version {current_version}."
                )

            if restoring and current["status"] != "archived":
                raise NotArchivedContextError(
                    "This procedure is not archived, so there is nothing to restore."
                )

            if current["status"] == "archived" and not restoring:
                if patch == {"status": "archived"}:
                    current["version_number"] = current_version
                    return current
                raise ArchivedContextError(
                    "An archived procedure cannot be modified."
                )

            next_ver = current_version + 1

            cur.execute(
                """
                UPDATE app.procedures
                SET name = %s, description = %s, frontmatter_yaml = %s,
                    body_md = %s, status = %s, owner = %s, updated_at = now()
                WHERE id = %s
                RETURNING
                    id, project_id, name, description, frontmatter_yaml,
                    body_md, status, owner, created_by, created_at, updated_at
                """,
                (new_name, new_desc, new_fm_yaml, new_body, new_status, new_owner, procedure_id),
            )
            updated_row = cur.fetchone()
            updated_proc = _row_to_topic(updated_row, cols)

            cur.execute(
                """
                INSERT INTO app.procedures_versions
                    (procedure_id, project_id, name, description, frontmatter_yaml, body_md,
                     status, owner, created_by, created_at, updated_at, version_number, changed_by,
                     changed_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz,
                     %s, %s, now())
                """,
                (
                    updated_proc["id"],
                    updated_proc["project_id"],
                    updated_proc["name"],
                    updated_proc["description"],
                    updated_proc["frontmatter_yaml"],
                    updated_proc["body_md"],
                    updated_proc["status"],
                    updated_proc["owner"],
                    updated_proc["created_by"],
                    updated_proc["created_at"],
                    updated_proc["updated_at"],
                    next_ver,
                    changed_by,
                ),
            )

            if new_status == "archived" and current["status"] != "archived":
                action = ACTION_PROCEDURE_ARCHIVED
            elif restoring:
                action = ACTION_PROCEDURE_RESTORED
            else:
                action = ACTION_PROCEDURE_UPDATED

            insert_audit_row(
                conn,
                identity=changed_by,
                action=action,
                provider_account="platform",
                connection_ref="",
                metadata={
                    "procedure_id": procedure_id,
                    "project_id": updated_proc["project_id"],
                    "name": updated_proc["name"],
                    "version_number": next_ver,
                },
            )
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateProcedureNameError(
            "A procedure with this name already exists in this scope."
        ) from exc

    updated_proc["version_number"] = next_ver
    return updated_proc


def archive_procedure(
    conn: Any,
    *,
    procedure_id: str,
    changed_by: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Archive a procedure with the same locked version precondition as PATCH."""
    return update_procedure(
        conn,
        procedure_id=procedure_id,
        patch={"status": "archived"},
        changed_by=changed_by,
        expected_version=expected_version,
    )


def restore_procedure(
    conn: Any,
    *,
    procedure_id: str,
    changed_by: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Bring an archived procedure back, symmetric to `restore_topic`."""
    return update_procedure(
        conn,
        procedure_id=procedure_id,
        patch={"status": "active"},
        changed_by=changed_by,
        expected_version=expected_version,
        restoring=True,
    )


def get_procedure(
    conn: Any, *, procedure_id: str, caller_project_id: str | None = None
) -> dict[str, Any] | None:
    """Fetch procedure by ID with version number, enforcing scope."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.id, p.project_id, p.name, p.description, p.frontmatter_yaml,
                p.body_md, p.status, p.owner, p.created_by, p.created_at, p.updated_at,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.procedures p
            LEFT JOIN app.procedures_versions v ON p.id = v.procedure_id
            WHERE p.id = %s
            GROUP BY
                p.id, p.project_id, p.name, p.description, p.frontmatter_yaml,
                p.body_md, p.status, p.owner, p.created_by, p.created_at, p.updated_at
            """,
            (procedure_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        cols = [desc[0] for desc in cur.description]
        proc = _row_to_topic(row, cols)

        if (
            caller_project_id is not None
            and proc["project_id"] is not None
            and proc["project_id"] != caller_project_id
        ):
            return None

        return proc


def list_procedure_versions(
    conn: Any, *, procedure_id: str, caller_project_id: str | None = None
) -> list[dict[str, Any]]:
    """Return immutable procedure snapshots visible in the caller project scope."""
    with conn.cursor() as cur:
        if caller_project_id is None:
            cur.execute(
                """
                SELECT procedure_id, project_id, name, description, frontmatter_yaml,
                       body_md, status, owner, created_by, created_at, updated_at,
                       version_number, changed_by, changed_at
                FROM app.procedures_versions
                WHERE procedure_id = %s
                ORDER BY version_number DESC
                """,
                (procedure_id,),
            )
        else:
            cur.execute(
                """
                SELECT procedure_id, project_id, name, description, frontmatter_yaml,
                       body_md, status, owner, created_by, created_at, updated_at,
                       version_number, changed_by, changed_at
                FROM app.procedures_versions
                WHERE procedure_id = %s
                  AND (project_id IS NULL OR project_id = %s)
                ORDER BY version_number DESC
                """,
                (procedure_id, caller_project_id),
            )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_topic(row, cols) for row in cur.fetchall()]


def list_procedures(
    conn: Any, *, project_id: str | None, status: str = "active", limit: int | None = None
) -> list[dict[str, Any]]:
    """List procedures visible to project_id (platform + project) with version numbers.

    ``status`` : same strict lifecycle contract as ``list_topics`` --
    "active" (default), "archived" or "all".
    """
    where_clauses = ["(p.project_id IS NULL OR p.project_id = %s)"]
    params: list[Any] = [project_id]

    if status == "active":
        where_clauses.append("p.status = 'active'")
    elif status == "archived":
        where_clauses.append("p.status = 'archived'")
    elif status != "all":
        raise ValueError(f"unknown procedure status filter: {status}")

    where_sql = " WHERE " + " AND ".join(where_clauses)
    limit_sql = ""
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("procedure limit must be between 1 and 1000")
        limit_sql = " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                p.id, p.project_id, p.name, p.description, p.frontmatter_yaml,
                p.body_md, p.status, p.owner, p.created_by, p.created_at, p.updated_at,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.procedures p
            LEFT JOIN app.procedures_versions v ON p.id = v.procedure_id
            {where_sql}
            GROUP BY
                p.id, p.project_id, p.name, p.description, p.frontmatter_yaml,
                p.body_md, p.status, p.owner, p.created_by, p.created_at, p.updated_at
            ORDER BY p.created_at DESC
            {limit_sql}
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_topic(row, cols) for row in cur.fetchall()]


def list_procedure_picker(
    conn: Any, *, project_id: str | None, status: str = "active", limit: int = 201
) -> list[dict[str, Any]]:
    """Read only the bounded fields required by an analysis Skill picker."""

    where_clauses = ["(p.project_id IS NULL OR p.project_id = %s)"]
    params: list[Any] = [project_id]
    if status == "active":
        where_clauses.append("p.status = 'active'")
    elif status == "archived":
        where_clauses.append("p.status = 'archived'")
    elif status != "all":
        raise ValueError(f"unknown procedure status filter: {status}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise ValueError("procedure picker limit must be between 1 and 1000")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.id, left(p.name, 160) AS name,
                   COALESCE(MAX(v.version_number), 1) AS version_number
              FROM app.procedures p
              LEFT JOIN app.procedures_versions v ON p.id = v.procedure_id
             WHERE {" AND ".join(where_clauses)}
             GROUP BY p.id, p.name, p.created_at
             ORDER BY p.created_at DESC, p.id
             LIMIT %s
            """,
            (*params, limit),
        )
        return [
            {"id": str(row[0]), "name": str(row[1]), "version_number": int(row[2])}
            for row in cur.fetchall()
        ]


# ---------------------------------------------------------------------------
# Context Graph Edge Domain Logic (Story 11.4)
# ---------------------------------------------------------------------------

#: Valid node types per the app.context_graph from_type/to_type CHECK
#: constraints: migration 031 shipped topic/procedure/schema_doc, migration 117
#: (Story 44.10) widened both to include 'target_field' — a data-dictionary
#: field, keyed by its NAME (app.target_fields has no id column) — and migration
#: 272 (Story 37.9) to include 'master_data_node'.
#:
#: `master_data_node` is a GOVERNED IDENTITY (app.master_data_nodes, migration
#: 140), keyed by its own `mdnode_...` id. Country's ratified coverage matrix asks
#: that geographic nodes be linkable to knowledge and Skills, and until 272 a
#: Market or Region could not be an endpoint of any edge — so `context_search`'s
#: one-hop walk, the corpus an agent actually reads, could never reach one.
#:
#: ONE type rather than 'market' / 'region' / 'competitor': Country is the first
#: capability to need this, not the only one, and every registry mounts its
#: identities on the same generic owner. The node's KIND stays readable where it
#: is governed (`master_data_nodes.node_kind`), so a caller that needs to tell a
#: Market from a Region reads the registry rather than the edge.
#:
#: A COUNTRY ISO CODE IS NOT ONE. It is a `child_value` of a hierarchy, never a
#: node — the same reason it declares no used-by dependency. Accepting it here
#: would put a value in a column pair whose contract is governed identities, and
#: a country's meaning is reachable through its market anyway.
GRAPH_NODE_TYPES = frozenset(
    {"topic", "procedure", "schema_doc", "target_field", "master_data_node"}
)

#: Canonical edge vocabulary: the console's EDGE_TYPE_SUGGESTIONS
#: (ui/admin/src/KnowledgeGraphPage.tsx), "applies-to" (the relation the
#: business-taxonomy projection and the working-context push script write) and
#: "describes" (the platform seed's topic -> schema_doc relation, context_seed).
#: Free-form strings made the graph a pile of vocabularies (live finding F2).
GRAPH_EDGE_TYPES = frozenset(
    {"defines", "depends_on", "explains", "relates_to", "applies-to", "describes"}
)


def _row_to_edge(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for col, val in zip(cols, row):
        if col == "created_at" and val is not None:
            record[col] = _format_datetime(val)
        else:
            record[col] = val
    return record


def list_graph_edges(
    conn: Any, *, project_id: str | None
) -> list[dict[str, Any]]:
    """List graph edges visible to project_id (platform + project).

    AD-5 scoping: returns edges where edge project_id IS NULL (platform)
    OR edge project_id = requested project_id.
    """
    where_clauses = ["(e.project_id IS NULL OR e.project_id = %s)"]
    params: list[Any] = [project_id]
    where_sql = " WHERE " + " AND ".join(where_clauses)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                e.id, e.from_id, e.from_type, e.to_id, e.to_type,
                e.edge_type, e.project_id, e.created_by, e.created_at
            FROM app.context_graph e
            {where_sql}
            ORDER BY e.created_at DESC
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_edge(row, cols) for row in cur.fetchall()]


def _row_to_schema_doc(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for col, val in zip(cols, row):
        if col in ("generated_at", "created_at") and val is not None:
            record[col] = _format_datetime(val)
        else:
            record[col] = val
    return record


def list_schema_docs(conn: Any, *, project_id: str) -> list[dict[str, Any]]:
    """List schema_context docs for a project (Story 44.3 / AD-17).

    app.schema_context.project_id is NOT NULL (schema docs are always project-scoped,
    unlike topics/procedures which can be platform-scoped). ``relation`` is the
    doc's title candidate (the table/relation name).

    IMPORTANT — version_number semantics differ from topics/procedures: schema_context_versions
    stores the PRE-update snapshot (upsert_schema_context_doc appends the OLD body BEFORE
    overwriting app.schema_context — see schema_context_gen.py), so the live row's version
    is always one AHEAD of the newest history row, not equal to it. The topics/procedures
    COALESCE(MAX(v.version_number), 1) pattern does NOT apply here: it would under-count by
    one. Instead we use a scalar subquery: COALESCE(MAX(v.version_number), 0) + 1. This also
    avoids a GROUP BY over the (potentially huge) body_md column.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                s.id, s.project_id, s.relation, s.doc_kind, s.body_md,
                s.generated_at, s.created_at,
                (
                    SELECT COALESCE(MAX(v.version_number), 0) + 1
                    FROM app.schema_context_versions v
                    WHERE v.schema_context_id = s.id
                ) AS version_number
            FROM app.schema_context s
            WHERE s.project_id = %s
            ORDER BY s.created_at DESC
            """,
            (project_id,),
        )
        cols = [desc[0] for desc in cur.description]
        return [_row_to_schema_doc(row, cols) for row in cur.fetchall()]


def get_graph_edge(conn: Any, *, edge_id: str) -> dict[str, Any] | None:
    """Fetch a single graph edge by ID (unfiltered; caller enforces scope)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                e.id, e.from_id, e.from_type, e.to_id, e.to_type,
                e.edge_type, e.project_id, e.created_by, e.created_at
            FROM app.context_graph e
            WHERE e.id = %s
            """,
            (edge_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in cur.description]
        return _row_to_edge(row, cols)


def _node_exists_in_scope(
    conn: Any,
    *,
    node_id: str,
    node_type: str,
    project_id: str | None,
) -> bool:
    """Check that a referenced node (from/to) exists and is visible in scope.

    For topics and procedures: platform rows (project_id IS NULL) are always
    visible; project rows must match the requested project_id.
    For schema_doc: any existing schema_context row counts (AD-17 — schema
    docs are read-only but linkable).
    For target_field (Story 44.10): ``node_id`` is the field NAME, not an id —
    app.target_fields is keyed by name and has no id column. The table is
    PLATFORM-GLOBAL (no org_id/project_id column at all, see
    core.datamodel.get_target_field), so scope never restricts visibility, the
    same way a platform topic is visible from every project. A soft-deleted
    field (status='deleted', migration 107) is NOT linkable: history outlives
    visibility, but a deleted field must not become a fresh edge endpoint.
    For master_data_node (Story 37.9): a governed identity keyed by its own id,
    with archived nodes excluded for target_field's reason, and the same
    platform-or-project scope clause as a topic — migration 143 made project_id
    nullable so an org-scoped registry is visible from every Project of its org.
    """
    with conn.cursor() as cur:
        if node_type == "topic":
            cur.execute(
                """
                SELECT 1 FROM app.context_topics
                WHERE id = %s
                  AND status = 'active'
                  AND (project_id IS NULL OR project_id = %s)
                """,
                (node_id, project_id),
            )
        elif node_type == "procedure":
            cur.execute(
                """
                SELECT 1 FROM app.procedures
                WHERE id = %s
                  AND status = 'active'
                  AND (project_id IS NULL OR project_id = %s)
                """,
                (node_id, project_id),
            )
        elif node_type == "schema_doc":
            # schema_context rows have a mandatory project_id (not nullable).
            # Edges TO a schema_doc are allowed even from platform scope;
            # the schema_doc must exist for the given project.
            cur.execute(
                """
                SELECT 1 FROM app.schema_context
                WHERE id = %s
                """,
                (node_id,),
            )
        elif node_type == "master_data_node":
            # Story 37.9. A governed identity, keyed by its own id. `archived_at
            # IS NULL` for target_field's reason: nothing is deleted upstream (an
            # old Result pins a version and must stay reproducible), so archiving
            # is the only thing that retires an identity — and a retired identity
            # must not become a FRESH endpoint. Existing edges are untouched:
            # history outlives visibility.
            #
            # The scope clause is the topic/procedure clause, not a stricter one.
            # Migration 143 made `project_id` nullable so an ORG-scoped registry
            # (Competitors) can be reused by several Projects, and those nodes are
            # visible from every Project of the org exactly as a platform topic is.
            cur.execute(
                """
                SELECT 1 FROM app.master_data_nodes
                WHERE id = %s
                  AND archived_at IS NULL
                  AND (project_id IS NULL OR project_id = %s)
                """,
                (node_id, project_id),
            )
        elif node_type == "target_field":
            # Name-keyed, platform-global, soft-deleted fields excluded.
            cur.execute(
                """
                SELECT 1 FROM app.target_fields
                WHERE name = %s
                  AND status != 'deleted'
                """,
                (node_id,),
            )
        else:
            return False
        return cur.fetchone() is not None


def node_exists_in_scope(
    conn: Any,
    *,
    node_id: str,
    node_type: str,
    project_id: str | None,
) -> bool:
    """The scope predicate above, PUBLIC -- one owner for one question.

    `core.context_relationships` validates the endpoints of every relation it
    mints and must apply exactly this predicate: two readers of "is this node
    visible from this project" drift into two answers, and the drift shows up as
    a relation pointing at an object the owning screen refuses to open. So the
    authority calls this rather than writing its own query.
    """
    return _node_exists_in_scope(
        conn, node_id=node_id, node_type=node_type, project_id=project_id
    )


# ---------------------------------------------------------------------------
# THE TWO WRITERS BECAME DELEGATIONS (story 49-6 AC5, migration 317)
#
# They kept their signatures, their ValueError contract and every one of their
# callers -- `context_seed`, the REST route, the console -- and stopped writing
# `app.context_graph` themselves. `core.context_relationships` is the authority
# now: it appends an immutable version row and writes the legacy row as a READ
# PROJECTION in the same transaction, which is the shape `governance.md`
# Decision 2 ratified on 2026-08-25 for the superseded taxonomy store.
#
# TWO DOORS, ONE WRITER -- `context-hub.md`, 2026-08-25: *what is forbidden is a
# second WRITER, never a second door*. Removing these two functions would have
# been the unmounted route that amendment refuses, and would have forced an edit
# into `context_seed.py`, a module another session owns.
#
# THE IMPORT IS LOCAL, and that is not a habit: `context_relationships` reads
# `GRAPH_EDGE_TYPES` and `node_exists_in_scope` from this module, so a
# module-level import in both directions is a cycle.
# ---------------------------------------------------------------------------


def create_graph_edge(
    conn: Any,
    *,
    project_id: str | None,
    from_id: str,
    from_type: str,
    to_id: str,
    to_type: str,
    edge_type: str,
    created_by: str,
) -> dict[str, Any]:
    """Declare one relation through the authority; return its legacy projection.

    The return shape is unchanged -- the nine columns of `app.context_graph` --
    because the REST door serialises it and `context_seed` reads `id` from it.
    A relation the legacy store cannot express (an endpoint that is a Semantic
    View, a metric or a Datastream) cannot arrive through this door at all: its
    `from_type` / `to_type` are outside `GRAPH_NODE_TYPES` and are refused
    below, exactly as they were before.

    `ValueError` stays the refusal type: `ContextRelationshipRefused` IS a
    `ValueError`, so every caller that mapped this to a 422 keeps doing so, and
    the sentence a person reads is now the authority's.
    """
    from core.context_relationships import create_relationship  # noqa: PLC0415

    if from_type not in GRAPH_NODE_TYPES:
        raise ValueError(
            f"Invalid source node type: '{from_type}'. "
            f"Allowed values: {sorted(GRAPH_NODE_TYPES)}."
        )
    if to_type not in GRAPH_NODE_TYPES:
        raise ValueError(
            f"Invalid target node type: '{to_type}'. "
            f"Allowed values: {sorted(GRAPH_NODE_TYPES)}."
        )
    if not isinstance(edge_type, str) or not edge_type.strip():
        raise ValueError("edge_type cannot be empty.")
    edge_type = edge_type.strip()
    if edge_type not in GRAPH_EDGE_TYPES:
        raise ValueError(
            f"Invalid edge_type: '{edge_type}'. "
            f"Allowed values: {sorted(GRAPH_EDGE_TYPES)}."
        )

    relation = create_relationship(
        conn,
        project_id=project_id,
        source_type=from_type,
        source_id=from_id,
        target_type=to_type,
        target_id=to_id,
        relationship_kind=edge_type,
        actor=created_by,
        provenance="context_graph_door",
    )
    return relation["projection"] or {}


def delete_graph_edge(
    conn: Any, *, edge_id: str, deleted_by: str
) -> bool:
    """Retire the relation this edge projects. Returns False when none is found.

    A HARD DELETE IS GONE, and that is the point of the story: the relation is
    SUPERSEDED -- the head keeps its identity, an immutable version row records
    who retired it and when, and only the read projection loses its row, so the
    four legacy readers see exactly what they saw before. `context-hub.md`,
    2026-08-17: *Retirement is a supersede, never a delete*.
    """
    from core.context_relationships import (  # noqa: PLC0415
        find_by_projection_edge,
        supersede_relationship,
    )

    relation = find_by_projection_edge(conn, edge_id=edge_id)
    if relation is None:
        return False
    supersede_relationship(
        conn,
        project_id=relation["project_id"],
        relationship_id=relation["id"],
        actor=deleted_by,
        reason="retired through the Knowledge Graph",
    )
    return True
