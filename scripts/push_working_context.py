#!/usr/bin/env python3
r"""Projeter le contexte de travail d'un agent dans le Context Hub de toorow.

POURQUOI. Idee de Jean, 2026-08-03 : trois metiers -- Testeur, Developpeur,
Manager -- relies a des procedures, plus une carte par ecran. Le cas d'usage est
double : ca peut m'aider a travailler, ET ca eprouve le graphe autrement que par
de l'analytique. Un agent est un utilisateur exigeant : il echoue bruyamment
quand son contexte ment.

LE SENS DE LA PROJECTION EST UNIQUE : depot -> produit. Le depot reste la source,
toorow en tient une PROJECTION. Relance apres `make context`, la fraicheur ne
depend plus du produit. Corollaire a dire a qui ouvrira l'ecran : ce qui est
projete NE S'EDITE PAS dans l'interface, la remontee suivante ecraserait.

    python scripts/push_working_context.py                       # simulation
    python scripts/push_working_context.py --apply --base http://localhost:8021 \
        --project proj_qa_local --token "$TOK"

LES CLASSES DU PRODUIT, ET LA FORME QUI LES RELIE. Jean, 2026-08-03 : « il faut
que tu arrives a te baser sur les business classes procedure et skill », puis
« et les procedure et skills tu les relis comment a tout ca ? ». Les deux
questions ont eu la meme reponse : mal, et il a fallu deux mesures pour le voir.

Le vocabulaire est dans `glossary.md`, la FORME dans `context-hub.md` :

    « A mindmap rooted in the project's Business Domains. It expands THROUGH
      CLASSIFICATION LAYERS to [...] knowledge and Skills. »
    « Skills Registry -- Versioned procedures grouped by governed business
      CLASSIFICATION, not six hardcoded departments. »

D'ou quatre etages, et non deux :

  1. **Business Domain** -- racine gouvernee, portee ORGANISATION. Six sont
     SEMEES par la migration 130 dans chaque organisation. On s'accroche a l'une
     d'elles ; en creer a cote produit deux vocabulaires sans relation.
  2. **Business Classification** -- la couche qui GROUPE les Skills.
     `POST /api/context/business-classifications` <- {domain_id,
     classification_type, name, slug, description, reason}
  3. **Skill** (le mot *Procedure* n'est PAS un nom produit, c'est un jeton livre)
     `POST /api/context/procedures` <- {frontmatter_yaml, body_md}, le frontmatter
     portant `name` et `description` (`context_store.py:47`).
     **Knowledge** -- `POST /api/context/topics` <- {title, **body_md**}.
  4. **Les liens**, et il y en a DEUX SYSTEMES DISTINCTS :
       * `POST /api/context/business-links` rattache a la taxonomie gouvernee
         {taxonomy_type, taxonomy_id, target_type, target_id, relation_type} ;
       * `POST /api/context/graph/edges` relie deux noeuds du Hub entre eux
         {from_id, from_type, to_id, to_type, edge_type} -- et ici, ⚠️ le
         `project_id` va dans le CORPS, pas en query string.

DEUX MESURES, DEUX ETATS FAUX :

  * 3 domaines, 36 cartes, **0 lien** -- le champ `business_domain` que je mettais
    dans le corps etait simplement ignore. J'avais annonce que les cartes se
    rattachaient aux domaines.
  * puis 45 liens mais **0 arete**, et **0 classification dans toute la base** --
    chaque objet pendait de sa racine, aucun n'etait attache a un autre, et la
    couche que le document ratifie nomme n'existait nulle part. Une etoile a trois
    centres, pas un graphe.

IDEMPOTENCE, CLIENT ET SERVEUR. Le serveur n'a pas d'upsert. Il a en revanche
trois unicites qui rendent une reprise sure : le slug d'un domaine, le nom d'une
procedure par projet, et `uq_mdm_business_link` sur le sextuplet du lien -- tous
rendent 409. Les Knowledge, eux, n'ont d'unicite QUE sur les titres de portee
plateforme : deux passages creeraient deux cartes. Ce script lit donc l'etat
avant d'ecrire et ne cree que ce qui manque. C'est la lecon des 108 cartes
orphelines : 36 x 3 passages, sans qu'aucune porte ne s'en apercoive.

POURQUOI `--dry-run` PAR DEFAUT. Un script qui cree des dizaines d'objets dans
une base reelle sans avoir tourne une fois, c'est la faute des 29 datastreams de
test -- et ceux-la ne peuvent plus etre supprimes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REASON = "Projection du contexte de travail agent depuis le depot (make context)"

#: Les relations reprises du vocabulaire deja utilise par la console
#: (`ui/admin/src/__tests__/ContextHubLayout.test.tsx`), pas inventees ici.
#:
#: ⚠️ ECRITES DEJA NORMALISEES. Le serveur passe `relation_type` dans
#: `normalize_slug` (`business_taxonomy.py:87`), qui remplace tout caractere non
#: alphanumerique par un tiret : `applies_to` est STOCKE `applies-to`. Envoyer la
#: forme a underscore marche -- mais la relecture ne retrouve plus le lien qu'on
#: vient d'ecrire, chaque passage le re-POSTe, et le 409 qui suit se laisse
#: compter comme une creation. Mesure : 9 liens annonces « crees » sur un
#: passage qui n'avait rien a creer.
REL_SKILL = "applies-to"
REL_KNOWLEDGE = "explains"

from working_context_corpus import TRADES, _tags, edges, knowledge, skills  # noqa: E402

#: Le type de classification. `normalize_slug` l'accepte librement ; celui-ci dit
#: ce que les metiers SONT : des roles de travail, pas des produits.
CLASSIFICATION_TYPE = "role"

#: L'arete interne au Hub : une Knowledge est reliee a la Skill de son metier dont
#: elle est la matiere. Sans elle, chaque objet pend de sa racine et aucun n'est
#: attache a un autre -- et le tier `neighbor` de `search_context`, qui traverse
#: `app.context_graph`, ne remonte jamais rien.
EDGE_TYPE = "applies-to"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def http(base: str, method: str, path: str, token: str | None, payload: dict | None = None):
    """Rend `(status, objet_json_ou_texte)`. Ne leve pas sur une reponse HTTP."""
    headers = {"Accept": "application/json", "User-Agent": "toorow-context-push/2.0"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base.rstrip("/") + path, method=method, data=data, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw


def scoped(path: str, project_id: str) -> str:
    """`project_id` va en QUERY STRING -- `business_taxonomy_api.py:56`.

    Le mettre dans le corps rend 422 `project_id is required`, un message exact
    que j'avais lu comme « il manque le projet » au lieu de « il n'est pas la ou
    je le lis ».
    """
    return f"{path}?project_id={urllib.parse.quote(project_id)}"


# ---------------------------------------------------------------------------
# Ce qui est projete
# ---------------------------------------------------------------------------


def skill_documents() -> list[tuple[str, str, str]]:
    """(slug du metier, frontmatter YAML, corps) pour chaque Skill.

    AUCUNE ETAPE N'EST ECRITE ICI. `working_context_corpus.skills()` les LIT dans
    les documents ratifies -- la seule table « Setup steps » du depot, et les
    « Incomplete if » de 22 documents. Une Skill redigee a la main serait une
    decision de conception presentee comme une reparation.
    """
    documents = []
    for skill in skills():
        # ⚠️ `mdm_tags` N'EST PAS UN CHAMP DE MOTS-CLES. La console l'intitule
        # « MDM tags — Canonical metrics » et le lie par cases a cocher au
        # catalogue `app.target_fields`. J'y ai d'abord ecrit `developer`,
        # `role-sequence`, `screen-fixer` : sur les 13 champs approuves du
        # catalogue local, ZERO resolvait. Il n'est donc ecrit que s'il nomme
        # vraiment des metriques gouvernees -- sinon pas du tout.
        tags = _tags(skill)
        frontmatter = (
            f"name: {json.dumps(skill['name'], ensure_ascii=False)}\n"
            f"description: {json.dumps(skill['description'], ensure_ascii=False)}\n"
        )
        if tags:
            frontmatter += "mdm_tags:\n" + "".join(f"  - {tag}\n" for tag in tags)
        # Les trois cles validees depuis le 2026-08-03. Une Skill qui n'en porte
        # pas n'est pas fautive -- elles sont facultatives -- mais elle laisse
        # l'interface sans rien a rendre qu'un bloc de texte.
        for step in skill.get("steps", []):
            frontmatter += (
                "steps:\n" if step is skill["steps"][0] else ""
            ) + "".join(
                f"  {'- ' if key == 'step' else '  '}{key}: "
                f"{json.dumps(value, ensure_ascii=False) if isinstance(value, str) else value}\n"
                for key, value in step.items()
            )
        if skill.get("acceptance"):
            frontmatter += "acceptance:\n" + "".join(
                f"  - {json.dumps(item, ensure_ascii=False)}\n"
                for item in skill["acceptance"]
            )
        # Symptome PUIS causes -- la seule forme qu'une interface peut rendre, et
        # qu'un symptome sans cause ne satisfait pas : le validateur le refuse,
        # parce qu'un symptome seul est un constat et non une aide.
        errors = [entry for entry in skill.get("common_errors", []) if entry.get("causes")]
        if errors:
            frontmatter += "common_errors:\n" + "".join(
                f"  - symptom: {json.dumps(entry['symptom'], ensure_ascii=False)}\n"
                "    causes:\n"
                + "".join(f"      - {json.dumps(cause, ensure_ascii=False)}\n"
                          for cause in entry["causes"])
                for entry in errors
            )
        body = "\n".join([
            *skill["lines"],
            "",
            f"**À retenir.** {skill['note']}",
            "",
            f"Source : `{skill['source']}` — lue, pas rédigée. "
            "Régénérée par `make context`.",
        ])
        documents.append((skill["trade"], frontmatter, body))
    return documents


def knowledge_cards() -> list[dict]:
    """La matiere : connecteurs, capacites, architecture, references, ecrans."""
    return knowledge()


# ---------------------------------------------------------------------------
# Etat courant, puis ecriture de ce qui manque seulement
# ---------------------------------------------------------------------------


def read_state(base: str, project_id: str, token: str | None) -> dict:
    state: dict = {"domains": {}, "roles": {}, "skills": {}, "cards": {},
                   "links": set(), "edges": set()}

    status, payload = http(base, "GET", scoped("/api/context/business-taxonomy", project_id), token)
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"lecture de la taxonomie impossible ({status}) : {str(payload)[:200]}")
    state["domains"] = {d["slug"]: d["id"] for d in payload.get("domains", [])}
    state["roles"] = {c["slug"]: c["id"] for c in payload.get("classifications", [])}

    # ⚠️ LES ARCHIVES GARDENT LEUR SLUG. Un metier archive puis reprojete rend
    # 409 « A sibling business classification with this slug already exists » --
    # un refus exact, mais qui bloque une reprise si on ne lit que l'actif. On
    # relit donc avec `status=all` pour pouvoir REACTIVER au lieu de recreer.
    status, payload = http(
        base, "GET", scoped("/api/context/business-taxonomy", project_id) + "&status=all", token)
    if status == 200 and isinstance(payload, dict):
        state["archived_roles"] = {
            c["slug"]: c["id"]
            for c in payload.get("classifications", [])
            if c.get("status") != "active"
        }

    # On retient le CORPS et la VERSION, pas seulement l'identifiant : une
    # projection qui se contente de creer ce qui manque laisse mentir tout ce qui
    # existe deja. Mesure : les 36 cartes creees par une version anterieure du
    # script portaient un corps VIDE (le champ s'appelle `body_md`, pas `body`) ;
    # les repasser sans comparer les laissait vides indefiniment.
    status, payload = http(base, "GET", scoped("/api/context/procedures", project_id), token)
    if status == 200 and isinstance(payload, dict):
        state["skills"] = {
            p["name"]: (p["id"], p.get("body_md") or "", p.get("version_number") or 1,
                        p.get("frontmatter_yaml") or "")
            for p in payload.get("procedures", [])
        }

    status, payload = http(base, "GET", scoped("/api/context/topics", project_id), token)
    if status == 200 and isinstance(payload, dict):
        state["cards"] = {
            t["title"]: (t["id"], t.get("body_md") or "", t.get("version_number") or 1)
            for t in payload.get("topics", [])
        }

    status, payload = http(base, "GET", scoped("/api/context/business-links", project_id), token)
    if status == 200 and isinstance(payload, dict):
        state["links"] = {
            (link["taxonomy_id"], link["target_type"], link["target_id"], link["relation_type"])
            for link in payload.get("links", [])
        }

    # Les aretes INTERNES au Hub (`app.context_graph`), Knowledge <-> Skill. Elles
    # ne sont pas dans `business-links` : ce sont deux systemes de liens
    # differents, et n'en connaitre qu'un laisse le graphe en etoile.
    status, payload = http(base, "GET", scoped("/api/context/graph/edges", project_id), token)
    if status == 200 and isinstance(payload, dict):
        state["edges"] = {
            (e["from_id"], e["to_id"], e["edge_type"]) for e in payload.get("edges", [])
        }
    return state


def project(base: str, project_id: str, token: str | None) -> int:
    state = read_state(base, project_id, token)
    # [crees, deja la, mis a jour] -- la troisieme colonne existe parce qu'une
    # projection sans elle ne peut pas corriger ce qu'elle a deja ecrit.
    tally = {"metiers": [0, 0, 0], "skills": [0, 0, 0], "knowledge": [0, 0, 0],
             "liens": [0, 0, 0], "aretes": [0, 0, 0]}
    failures: list[str] = []

    def note(bucket: str, created: bool, ok: bool, message: str = "") -> None:
        tally[bucket][0 if created else 1] += 1
        if not ok:
            failures.append(message)

    def refresh(bucket: str, path: str, current: tuple, wanted_body: str, patch: dict) -> None:
        """PATCH si le corps OU le frontmatter a derive.

        ⚠️ Ne comparer que le corps laissait passer une derive de frontmatter :
        ajouter `mdm_tags` a 31 Skills n'aurait rien declenche, et la projection
        aurait affirme « 0 mis a jour » en etant fausse.
        """
        object_id, body, version = current[0], current[1], current[2]
        wanted_front = patch.get("frontmatter_yaml")
        if body == wanted_body and (
            wanted_front is None or len(current) < 4 or wanted_front == current[3]
        ):
            return
        status, payload = http(
            base, "PATCH", scoped(f"{path}/{object_id}", project_id), token,
            {**patch, "expected_version": version},
        )
        if status in (200, 201):
            tally[bucket][2] += 1
        else:
            failures.append(f"mise a jour {object_id} -> {status} {str(payload)[:160]}")

    # 1. LA COUCHE DE CLASSIFICATION, sous des racines QUI EXISTENT DEJA.
    #    `context-hub.md` : « A mindmap rooted in the project's Business Domains.
    #    It expands THROUGH CLASSIFICATION LAYERS to [...] knowledge and Skills »,
    #    et « Skills Registry -- Versioned procedures grouped by governed business
    #    CLASSIFICATION ». Chaque metier declare SA racine : un Data Analyst vit
    #    sous `marketing`, un auditeur sous `engineering`. Les creer comme racines
    #    mettait deux vocabulaires sans relation cote a cote.
    for slug, trade in TRADES.items():
        if slug in state["roles"]:
            note("metiers", False, True)
            continue
        archived = state.get("archived_roles", {}).get(slug)
        if archived:
            status, payload = http(
                base, "PATCH",
                scoped(f"/api/context/business-classifications/{archived}", project_id), token,
                {"status": "active", "name": trade["name"],
                 "description": trade["description"], "reason": REASON},
            )
            if status in (200, 201):
                state["roles"][slug] = archived
                tally["metiers"][2] += 1
                continue
            note("metiers", True, False,
                 f"reactivation de {slug} -> {status} {str(payload)[:160]}")
            continue
        root_id = state["domains"].get(trade["root"])
        if root_id is None:
            note("metiers", True, False,
                 f"metier {slug} : la racine « {trade['root']} » n'existe pas dans "
                 "cette organisation (elle est semee par la migration 130)")
            continue
        status, payload = http(
            base,
            "POST",
            scoped("/api/context/business-classifications", project_id),
            token,
            {
                "domain_id": root_id,
                "classification_type": CLASSIFICATION_TYPE,
                "name": trade["name"],
                "slug": slug,
                "description": trade["description"],
                "reason": REASON,
            },
        )
        if status in (200, 201) and isinstance(payload, dict):
            state["roles"][slug] = payload["id"]
            note("metiers", True, True)
        else:
            note("metiers", True, False, f"metier {slug} -> {status} {str(payload)[:160]}")

    missing = [slug for slug in TRADES if slug not in state["roles"]]
    if missing:
        print(
            "REFUS : les metiers " + ", ".join(missing) + " n'existent pas. Rien "
            "d'autre ne sera cree : ce serait orphelin, et personne ne le "
            "retrouverait jamais par le graphe.",
            file=sys.stderr,
        )
        for line in failures:
            print("  " + line, file=sys.stderr)
        return 1

    # 2. Skills, puis leur lien vers le metier qui les groupe.
    for trade_slug, frontmatter, body in skill_documents():
        name = json.loads(frontmatter.splitlines()[0].split(": ", 1)[1])
        known = state["skills"].get(name)
        if known is None:
            status, payload = http(
                base, "POST", scoped("/api/context/procedures", project_id), token,
                {"frontmatter_yaml": frontmatter, "body_md": body},
            )
            if status in (200, 201) and isinstance(payload, dict):
                state["skills"][name] = (payload["id"], body, 1, frontmatter)
                note("skills", True, True)
            else:
                note("skills", True, False, f"skill « {name} » -> {status} {str(payload)[:160]}")
                continue
        else:
            note("skills", False, True)
            refresh("skills", "/api/context/procedures", known, body,
                    {"frontmatter_yaml": frontmatter, "body_md": body})
        link(base, project_id, token, state, tally, failures,
             state["roles"][trade_slug], "procedure", state["skills"][name][0], REL_SKILL)

    # 3. Knowledge, puis leur lien vers le metier dont elle est la matiere.
    for card in knowledge_cards():
        known = state["cards"].get(card["title"])
        if known is None:
            status, payload = http(
                base, "POST", scoped("/api/context/topics", project_id), token,
                {"title": card["title"], "body_md": card["body_md"]},
            )
            if status in (200, 201) and isinstance(payload, dict):
                state["cards"][card["title"]] = (payload["id"], card["body_md"], 1)
                note("knowledge", True, True)
            else:
                note("knowledge", True, False,
                     f"carte « {card['title']} » -> {status} {str(payload)[:160]}")
                continue
        else:
            note("knowledge", False, True)
            refresh("knowledge", "/api/context/topics", known, card["body_md"],
                    {"title": card["title"], "body_md": card["body_md"]})
        link(base, project_id, token, state, tally, failures,
             state["roles"][card["trade"]], "topic",
             state["cards"][card["title"]][0], REL_KNOWLEDGE)

    # 4. LES ARETES INTERNES AU HUB, et SEULEMENT celles qui se derivent.
    #    Une carte rattachee a un metier est trouvable ; elle ne dit toujours pas
    #    QUOI FAIRE avec. C'est `app.context_graph` qui porte cette relation, et
    #    c'est ce que le tier `neighbor` de `search_context` traverse.
    pairs, orphans = edges()
    for card_title, skill_name in pairs:
        card = state["cards"].get(card_title)
        skill = state["skills"].get(skill_name)
        if card is None or skill is None:
            failures.append(f"arete impossible : « {card_title} » ou « {skill_name} » absent")
            continue
        edge(base, project_id, token, state, tally, failures,
             skill[0], "procedure", card[0], "topic", EDGE_TYPE)
    if orphans:
        print(f"\n{len(orphans)} Knowledge sans Skill dérivable — mesure, pas oubli :")
        families: dict[str, int] = {}
        for title in orphans:
            family = title.split(" — ")[0]
            families[family] = families.get(family, 0) + 1
        for family, count in sorted(families.items(), key=lambda kv: -kv[1]):
            print(f"    {family:<24}{count:>4}")
        print("  Leur inventer une Skill remettrait de la prose la ou la "
              "documentation n'en porte pas.")

    print()
    for bucket, (created, kept, updated) in tally.items():
        print(f"  {bucket:<10} {created:>3} créés   {kept:>3} déjà là   {updated:>3} mis à jour")
    if failures:
        print(f"\n{len(failures)} échec(s) :", file=sys.stderr)
        for line in failures[:10]:
            print("  " + line, file=sys.stderr)
        return 1
    print("\nprojection complète — relancer ce script est sans effet (idempotent).")
    return 0


def edge(base, project_id, token, state, tally, failures,
         from_id, from_type, to_id, to_type, edge_type) -> None:
    """Une arete `app.context_graph` -- Knowledge <-> Skill, a l'interieur du Hub.

    Ce n'est PAS le meme systeme que `business-links` : celui-la rattache a la
    taxonomie gouvernee, celui-ci relie deux noeuds du Hub entre eux. N'en
    connaitre qu'un laisse un graphe sans aucune arete horizontale.
    """
    key = (from_id, to_id, edge_type)
    if key in state["edges"]:
        tally["aretes"][1] += 1
        return
    status, payload = http(
        base, "POST", "/api/context/graph/edges", token,
        {
            "project_id": project_id,   # ⚠️ ICI le projet va dans le CORPS
            "from_id": from_id, "from_type": from_type,
            "to_id": to_id, "to_type": to_type, "edge_type": edge_type,
        },
    )
    if status in (200, 201):
        tally["aretes"][0] += 1
        state["edges"].add(key)
    elif status == 409:
        tally["aretes"][1] += 1
        state["edges"].add(key)
    else:
        failures.append(f"arete {from_id} -> {to_id} : {status} {str(payload)[:160]}")


def link(base, project_id, token, state, tally, failures,
         taxonomy_id, target_type, target_id, relation_type) -> None:
    key = (taxonomy_id, target_type, target_id, relation_type)
    if key in state["links"]:
        tally["liens"][1] += 1
        return
    status, payload = http(
        base,
        "POST",
        scoped("/api/context/business-links", project_id),
        token,
        {
            # La SOURCE du lien est une classification, pas une racine. Ecrit en
            # dur a `business_domain`, le serveur cherchait l'identifiant dans la
            # mauvaise table et rendait 404 « Resource not found » -- un refus qui
            # ne dit pas qu'on s'est trompe de couche.
            "taxonomy_type": "business_classification",
            "taxonomy_id": taxonomy_id,
            "target_type": target_type,
            "target_id": target_id,
            "relation_type": relation_type,
            "reason": REASON,
        },
    )
    if status in (200, 201):
        tally["liens"][0] += 1
        state["links"].add(key)
    elif status == 409:
        # Le lien existait sans que la relecture le retrouve : c'est un ECART
        # entre ce qu'on envoie et ce que le serveur stocke, pas une creation.
        # Le compter comme creation est exactement ce qui a masque la
        # normalisation de `relation_type` pendant deux passages.
        tally["liens"][1] += 1
        state["links"].add(key)
        failures.append(
            f"lien {target_type} {target_id} rendu 409 alors que la relecture ne le "
            f"voyait pas -- la cle ecrite et la cle relue divergent ({relation_type})"
        )
    else:
        failures.append(f"lien {target_type} {target_id} -> {status} {str(payload)[:160]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="envoyer réellement (défaut : n'imprime que le plan)")
    ap.add_argument("--base", default="http://localhost:8000",
                    help="instance visée ; JAMAIS la production sans y avoir pensé deux fois")
    ap.add_argument("--token", default=os.environ.get("TOOROW_QA_ID_TOKEN"),
                    help="jeton porteur ; défaut : $TOOROW_QA_ID_TOKEN")
    ap.add_argument("--project", default=None,
                    help="project_id ; exigé en QUERY STRING par /api/context/* (422 sans lui)")
    args = ap.parse_args()

    documents = skill_documents()
    cards = knowledge_cards()
    pairs, orphans = edges()
    for slug, trade in TRADES.items():
        count_s = sum(1 for d in documents if d[0] == slug)
        count_k = sum(1 for c in cards if c["trade"] == slug)
        print(f"  {slug:<18} sous `{trade['root']:<11}`  {count_s:>3} Skills  "
              f"{count_k:>3} Knowledge")
    print(f"  -> {len(documents) + len(cards)} liens gouvernés, {len(pairs)} arêtes, "
          f"{len(orphans)} cartes sans Skill dérivable")
    print(f"cible : {args.base}   mode : {'APPLIQUE' if args.apply else 'simulation'}")

    if not args.apply:
        print("\nsimulation — les cinq appels :")
        print("  POST /api/context/business-classifications  sous une racine SEMÉE")
        print("  POST /api/context/procedures                frontmatter name/description")
        print("  POST /api/context/topics                    title + body_md")
        print("  POST /api/context/business-links            classification -> Skill")
        print("  POST /api/context/graph/edges               Skill -> Knowledge")
        print("\nRelancer avec --apply --project <id> --token <jeton> pour envoyer.")
        return 0

    if not args.project:
        print("REFUS : --project est requis (les routes /api/context/* rendent 422 sans lui).",
              file=sys.stderr)
        return 1
    if not args.token:
        print("REFUS : aucun jeton (--token ou $TOOROW_QA_ID_TOKEN). Toute route rendrait 401.",
              file=sys.stderr)
        return 1
    return project(args.base, args.project, args.token)


if __name__ == "__main__":
    raise SystemExit(main())
