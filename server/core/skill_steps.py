"""Le referent d'un pas de Skill -- ce que `ai_path_steps.skill_step_id` designe.

POURQUOI. Mesure du 2026-08-03 : `app.ai_path_steps` declare `skill_step` comme
GENRE DE PAS de premiere classe, porte `skill_step_id` et `skill_version_id`
epingles ensemble par contrainte...

    ck_ai_path_steps_skill_pin
        CHECK ((skill_version_id IS NULL) = (skill_step_id IS NULL))

...et **aucune table de pas de Skill n'existait**, ni aucune cle etrangere. Le
referent etait reserve et vide : une trace pouvait declarer << j'ai execute le pas
X de la version Y >> sans que rien ne dise ce qu'etait le pas X.

LE REFERENT N'EST PAS UNE TABLE, ET C'EST DELIBERE. Une Skill porte deja sa
sequence dans son frontmatter, et `app.procedures_versions` en garde une copie
IMMUABLE a chaque version. Le couple (version, numero de pas) suffit donc a
designer un pas sans dupliquer quoi que ce soit -- et dupliquer aurait ete pire :
deux copies d'une sequence divergent, et c'est le defaut que ce depot passe ses
journees a trouver.

Ce module resout ce couple, et sait dire les trois refus qu'une resolution peut
rencontrer -- version inconnue, sequence absente, pas inexistant. Les confondre
ferait lire << ce pas n'existe pas >> la ou la Skill n'a simplement jamais eu de
sequence.

    (procedure_id, version_number, "3")  ->  {step, action, label, target, ...}
"""

from __future__ import annotations

from typing import Any

#: Ce qu'une resolution peut echouer a trouver. Trois etats, jamais fondus :
#: une version qu'on ne retrouve pas n'est pas une Skill sans sequence, et une
#: Skill sans sequence n'est pas un pas inexistant.
UNKNOWN_VERSION = "unknown_version"
NO_SEQUENCE = "no_sequence"
UNKNOWN_STEP = "unknown_step"


def step_reference(step: int | str) -> str:
    """La forme canonique d'un `skill_step_id` : le numero, en texte.

    `ai_path_steps.skill_step_id` est un TEXT de 1 a 200 caracteres sans cle
    etrangere. Lui donner une forme -- le numero du pas -- est ce qui le rend
    resolvable ; le laisser libre en aurait fait un commentaire.
    """
    return str(step).strip()


def resolve(
    conn: Any, *, procedure_id: str, version_number: int, skill_step_id: str
) -> dict[str, Any]:
    """Le pas designe, ou la raison EXACTE pour laquelle il ne l'est pas."""
    from core.context_store import validate_procedure_frontmatter  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT frontmatter_yaml, name
            FROM app.procedures_versions
            WHERE procedure_id = %s AND version_number = %s
            """,
            (procedure_id, version_number),
        )
        row = cur.fetchone()

    if row is None:
        return {"found": False, "reason": UNKNOWN_VERSION}

    frontmatter, name = row
    try:
        parsed = validate_procedure_frontmatter(frontmatter or "")
    except ValueError:
        # Une version ANCIENNE peut ne plus satisfaire le validateur d'aujourd'hui.
        # C'est une sequence qu'on ne sait pas lire, pas un pas absent.
        return {"found": False, "reason": NO_SEQUENCE, "skill_name": name}

    steps = parsed.get("steps") or []
    if not steps:
        return {"found": False, "reason": NO_SEQUENCE, "skill_name": name}

    wanted = step_reference(skill_step_id)
    for step in steps:
        if step_reference(step["step"]) == wanted:
            return {"found": True, "skill_name": name, "step": step}
    return {
        "found": False,
        "reason": UNKNOWN_STEP,
        "skill_name": name,
        "declared_steps": [step_reference(step["step"]) for step in steps],
    }


#: L'etat d'une resolution, TEL QU'UNE SURFACE LE LIT. Les quatre premiers sont
#: des faits sur le corpus ; le cinquieme est un fait sur la LECTURE, et les
#: fondre ferait dire << ce pas n'existe pas >> la ou la verite est << on n'a pas
#: pu regarder >>. C'est la regle d'AI-158, appliquee au referent.
PIN_RESOLVED = "resolved"
PIN_UNAVAILABLE = "unavailable"


def resolve_pin(
    conn: Any, *, skill_version_id: str | None, skill_step_id: str | None
) -> dict[str, Any] | None:
    """CE QUE LE PAS DISAIT, pour une surface qui lit un chemin -- Story 45.7.

    POURQUOI ELLE EXISTE. Mesure de la relecture adversariale du 2026-08-05 :
    `resolve()` avait ZERO lecteur sur une surface de chemin. `ai_paths` rendait
    `skill_version_id` brut et `trace_observation.context_skills_lens` le couple
    brut -- ni intitule, ni action, a aucune version. L'AC1 (<< le pas resout
    vers l'intitule et l'action que cette version declarait >>) et l'AC2 (<< la
    Skill avance, il resout toujours contre la version servie >>) n'avaient donc
    aucun code : le referent etait ecrit, teste, et servi a personne.

    LE COUPLE, OU RIEN. `ck_ai_path_steps_skill_pin` refuse d'enregistrer une
    moitie de couple ; cette fonction refuse d'en lire une. Elle rend `None`
    quand il n'y a pas d'epinglage -- une absence, pas un echec.

    ELLE NE LEVE JAMAIS. Lire ce que le pas disait est une DECORATION du chemin ;
    un referent illisible ne doit pas emporter la lecture de la trace. Le sort
    d'une lecture qui a echoue est `unavailable` -- jamais une sequence vide, qui
    se lirait << cette Skill n'a pas de pas >>.
    """
    if not skill_version_id or not skill_step_id:
        return None
    pinned = {"skill_version_id": skill_version_id, "skill_step_id": skill_step_id}
    parsed = parse_version_reference(skill_version_id)
    if parsed is None:
        return {**pinned, "state": UNKNOWN_VERSION}
    procedure_id, version_number = parsed
    try:
        found = resolve(
            conn,
            procedure_id=procedure_id,
            version_number=version_number,
            skill_step_id=skill_step_id,
        )
    except Exception:  # noqa: BLE001 -- reading never breaks the read
        return {**pinned, "state": PIN_UNAVAILABLE}
    if not found["found"]:
        return {**pinned, "state": found["reason"],
                "skill_name": found.get("skill_name")}
    step = found["step"]
    return {
        **pinned,
        "state": PIN_RESOLVED,
        "skill_name": found.get("skill_name"),
        # CE QUE LA VERSION SERVIE DECLARAIT, et rien de plus : le referent
        # decrit un pas, il ne re-sert pas la Skill.
        "action": step.get("action"),
        "label": step.get("label"),
        "target": step.get("target"),
        "command": step.get("command"),
        "tool": step.get("tool"),
        "stop_if": step.get("stop_if"),
    }


def describes_a_step(
    conn: Any, *, procedure_id: str, version_number: int, skill_step_id: str
) -> bool:
    """Vrai si le couple designe un pas reel.

    Sert a REFUSER une trace qui epingle un pas inexistant : sans ce controle,
    `skill_step_id` accepte n'importe quel texte, et un chemin observe peut
    pretendre avoir suivi une sequence qu'il n'a pas suivie -- ce qui rendrait la
    comparaison attendu/observe muette au moment ou elle compte.
    """
    return bool(
        resolve(
            conn,
            procedure_id=procedure_id,
            version_number=version_number,
            skill_step_id=skill_step_id,
        )["found"]
    )


# ---------------------------------------------------------------------------
# Ce qu'une trace a PRIS, et ce qu'on peut honnetement en observer -- Story 45.7
# ---------------------------------------------------------------------------
#
# LE PROBLEME, POSE FRANCHEMENT. Le serveur ne voit pas un agent « executer un
# pas ». Il voit une Skill servie, puis des appels d'outils. Ce module ne comble
# cet ecart QUE la ou il n'y a rien a inventer : un pas qui declare `tool: X`,
# suivi dans la MEME trace d'un appel a X, est un pas execute -- c'est
# exactement ce que `tool_bindings` et le champ `tool` d'un pas voulaient dire.
#
# TROIS REFUS DELIBERES, parce qu'observer de travers vaut moins que ne rien
# observer -- une trace qui fabrique de l'adherence est pire qu'une trace vide :
#
#   * PAS DE TRACE, PAS DE PAS. La fenetre temporelle qui suffit a `adherence`
#     (une MESURE, non bloquante) ne suffit pas ici : le chemin observe est la
#     preuve que le Test lit, et attribuer l'appel d'un autre operateur a ma
#     Skill y serait un faux, pas une approximation.
#   * UN PAS SANS `tool` N'EST PAS OBSERVABLE. `read`, `edit` sur une cible, une
#     commande a lancer : le serveur ne les voit pas passer, donc il n'en dit
#     rien.
#   * DEUX PAS, MEME OUTIL, AUCUN PAS. Si deux pas declarent le meme outil, on
#     ne peut pas dire lequel a ete franchi. Choisir le premier serait un tirage
#     au sort presente comme une observation.
#
# L'etat vit EN PROCESSUS, comme `core.adherence` et pour la meme raison (mono
# replique a ce stade). Une replique qui n'a pas servi la Skill n'emet pas le
# pas : on perd une observation, on n'en invente jamais une.

import threading  # noqa: E402
import time  # noqa: E402

_SERVED_TTL_SECONDS = 900
_SERVED_MAX = 512
#: Cle = (trace_id, project_id). Le projet EST dans la cle depuis le 2026-08-10 :
#: une trace qui traverse deux projets garde deux memoires, elle n'en ecrase pas
#: une.
_served: dict[tuple[str, str | None], dict[str, Any]] = {}
_served_lock = threading.Lock()


def version_reference(procedure_id: str, version_number: int) -> str:
    """La forme canonique d'un `skill_version_id`.

    `app.procedures_versions` a pour cle primaire (procedure_id, version_number)
    et AUCUN identifiant de substitution : la reference doit donc porter les deux.
    Elle est fabriquee ici et nulle part ailleurs, comme `step_reference`.
    """
    return f"{procedure_id}@{int(version_number)}"


def parse_version_reference(reference: str | None) -> tuple[str, int] | None:
    """Le couple designe par un `skill_version_id`, ou None s'il n'en designe pas."""
    if not reference or "@" not in reference:
        return None
    procedure_id, _, raw = reference.rpartition("@")
    if not procedure_id or not raw.isdigit():
        return None
    return procedure_id, int(raw)


def _prune(now: float) -> None:
    """Sous verrou. Borne la memoire par le temps ET par le nombre."""
    stale = [key for key, value in _served.items() if now - value["at"] > _SERVED_TTL_SECONDS]
    for key in stale:
        _served.pop(key, None)
    while len(_served) > _SERVED_MAX:
        _served.pop(min(_served, key=lambda k: _served[k]["at"]), None)


def remember_served(
    trace_id: str | None,
    *,
    procedure_id: str,
    version_number: int | None,
    steps: list[dict[str, Any]] | None,
    project_id: str | None = None,
) -> bool:
    """Retenir qu'une trace a pris cette Skill, A CETTE VERSION.

    La version retenue est celle SERVIE. Une Skill qui avance ensuite ne doit pas
    re-etiqueter une execution deja faite -- c'est la regle que la migration 150
    applique deja au `policy_snapshot`, vue d'un autre cote.
    """
    if not trace_id or not procedure_id or not version_number:
        return False
    by_tool: dict[str, list[str]] = {}
    for step in steps or []:
        tool = (step.get("tool") or "").strip()
        if not tool:
            continue
        by_tool.setdefault(tool, []).append(step_reference(step["step"]))
    if not by_tool:
        return False
    now = time.time()
    with _served_lock:
        # ⚠️ ON ACCUMULE, ON N'ECRASE PAS. Relecture adversariale du 2026-08-05 :
        # deux `get_procedure` dans une meme trace -- le cas NORMAL des qu'une
        # Skill en cite une autre -- faisaient disparaitre la premiere sequence,
        # et un outil declare par les deux etait epingle a la derniere servie.
        # C'etait le tirage au sort que ce module refuse DANS une Skill, fait
        # ENTRE deux. Un outil ambigu entre plusieurs Skills n'en designe donc
        # aucune, exactement comme entre deux pas.
        #
        # ⚠️ LA CLE PORTE LE PROJET, ET C'EST LA SECONDE PERTE SILENCIEUSE.
        # Relecture du 2026-08-05, defaut #4 : l'entree etait REMPLACEE des que
        # le projet changeait, donc servir A dans p1 puis B dans p2 dans la meme
        # trace effacait la memoire de p1 -- `observed_step(T,'t1','p1')` rendait
        # `None`. Une trace peut legitimement traverser deux projets ; la memoire
        # de l'un n'est pas une raison de perdre celle de l'autre. Deux memoires,
        # deux cles.
        key = (trace_id, project_id)
        entry = _served.get(key)
        if entry is None:
            entry = {"project_id": project_id, "by_tool": {}, "at": now}
            _served[key] = entry
        entry["at"] = now
        version = version_reference(procedure_id, version_number)
        # THE WHOLE SEQUENCE SERVED, not only its tool index: the policy a path
        # pins and the closure rule read it (2026-09-05).
        sequences = entry.setdefault("sequences", {})
        if version not in sequences:
            sequences[version] = [
                {
                    "step": step_reference(step["step"]),
                    "tool": (step.get("tool") or "").strip() or None,
                    "required": bool(step.get("required")),
                }
                for step in (steps or [])
                if isinstance(step, dict) and step.get("step") is not None
            ]
        for tool, references in by_tool.items():
            known = entry["by_tool"].setdefault(tool, [])
            for reference in references:
                # ⚠️ DEDUPE, ET C'EST LA PREMIERE PERTE SILENCIEUSE. Relecture
                # du 2026-08-05, defaut #3 : une MEME Skill servie deux fois
                # dans une trace -- un agent qui relit sa propre Skill --
                # empilait le meme couple deux fois, `len(candidates) != 1`
                # devenait vrai, et l'observation disparaissait sans un mot.
                # Deux fois le meme pas n'est pas une ambiguite : c'est le meme
                # pas.
                pair = (version, reference)
                if pair not in known:
                    known.append(pair)
        # APRES l'ecriture, et pas avant : elaguer d'abord laisse la carte
        # atteindre MAX+1 -- une borne qu'on depasse d'une case n'est pas une
        # borne, c'est une intention.
        _prune(now)
    return True


def served_sequences(
    trace_id: str | None, *, project_id: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """version reference -> the steps the served Skill declared (`step`, `tool`, `required`).

    Empty when the trace took no Skill in this Project, or when the memory
    expired: a caller reads « nothing served » and pins nothing, never a guess.
    """
    if not trace_id:
        return {}
    with _served_lock:
        served = _served.get((trace_id, project_id))
        if served is None or time.time() - served["at"] > _SERVED_TTL_SECONDS:
            return {}
        return {
            version: [dict(step) for step in steps]
            for version, steps in (served.get("sequences") or {}).items()
        }


def observed_step(
    trace_id: str | None, tool_name: str | None, *, project_id: str | None = None
) -> tuple[str, str] | None:
    """`(skill_version_id, skill_step_id)` si cet appel EST un pas declare.

    LE PROJET DOIT CORRESPONDRE. Sans lui, une trace qui prend une Skill du
    projet A puis appelle un outil du projet B ecrivait, dans le chemin de B,
    une reference a la Skill de A -- un identifiant d'un autre locataire dans le
    magasin de preuves. Le sort d'une observation douteuse est de ne pas exister.
    """
    if not trace_id or not tool_name:
        return None
    with _served_lock:
        served = _served.get((trace_id, project_id))
        if served is None or time.time() - served["at"] > _SERVED_TTL_SECONDS:
            return None
        candidates = served["by_tool"].get(tool_name.strip())
        if not candidates or len(candidates) != 1:
            return None
        return candidates[0]


def forget_served() -> None:
    """Vider la memoire -- pour les tests, qui ne doivent pas se contaminer."""
    with _served_lock:
        _served.clear()
