#!/usr/bin/env python3
r"""Un index genere de `_bmad-output/` et `docs/product-architecture/`.

POURQUOI CE SCRIPT EXISTE. Mesure du 2026-08-03 : 846 fichiers, 47 Mo, et
AUCUN index -- ni `README.md`, ni `index.md`, nulle part sous `_bmad-output/`.
Une session qui cherche la cible d'une surface ne peut donc que `grep`, et un
grep ne classe pas : il rend le premier fichier qui contient le mot, qui est
souvent un document remplace. C'est la cause mecanique du symptome decrit par
Jean -- « il retrouve pas les document, il utilise des grep, du coup il se base
sur des document obsolete ».

Trois pieges que ce fichier existe pour desamorcer, tous mesures :

  1. `architecture-connector-2026-07-10/` porte une date PLUS ANCIENNE que
     `architecture-connector-2026-07-25/` et ce n'est PAS une vieille version :
     la premiere est la colonne vertebrale plateforme (AD-1..AD-34, mise a jour
     le 2026-07-29, citee 90 fois), la seconde est un spine de FEATURE pour
     l'ingestion fichier (citee 9 fois). Le nommage date fait croire a une
     succession qui n'existe pas.
  2. 16 cles du tracker designent un fichier de story portant un AUTRE nom
     (`15-2-tiktok-ads` -> `15-2-connecteur-tiktok-ads.md`). Chercher par la cle
     ne trouve rien, et la session se rabat sur un grep.
  3. 149 cles n'ont AUCUN fichier de story. L'absence ressemble a une recherche
     ratee ; l'index la nomme pour ce qu'elle est.

    python scripts/bmad_index.py            # regenere _bmad-output/INDEX.md
    python scripts/bmad_index.py --gate     # non-zero si l'index est perime

CE QU'IL NE FAIT PAS : deplacer ou supprimer un fichier. Une supersession est
DECLAREE ici (table `SUPERSEDED`, verifiee a la main, chaque entree datee), pas
deduite d'une date -- voir le piege 1. Ecarter un document, c'est le marquer et
nommer son successeur ; le deplacer casserait les 20 references vivantes qui
pointent dessus et recreerait exactement le probleme que cet index resout.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BMAD = ROOT / "_bmad-output"
PLANNING = BMAD / "planning-artifacts"
IMPL = BMAD / "implementation-artifacts"
ARCH = ROOT / "docs" / "product-architecture"
INDEX = BMAD / "INDEX.md"
TRACKER = IMPL / "sprint-status.yaml"
SETTLED = BMAD / "archive" / "implementation-artifacts" / "sprint-status-settled.yaml"

#: Supersessions VERIFIEES a la main. Jamais deduite d'une date : deux dossiers
#: dates peuvent etre deux portees differentes et non deux versions (cf. les
#: deux `architecture-connector-*`). Chaque entree porte la preuve de sa lecture.
SUPERSEDED: dict[str, tuple[str, str]] = {
    "planning-artifacts/ux-designs/ux-connector-2026-07-19": (
        "planning-artifacts/ux-designs/ux-connector-2026-07-23",
        "meme DESIGN.md + EXPERIENCE.md, 3 fichiers contre 92 ; verifie 2026-08-03",
    ),
    "planning-artifacts/ux-designs/ux-connector-2026-07-22": (
        "planning-artifacts/ux-designs/ux-connector-2026-07-23",
        "meme DESIGN.md + EXPERIENCE.md + mockups, 14 fichiers contre 92 ; verifie 2026-08-03",
    ),
    "planning-artifacts/implementation-readiness-report-2026-07-22-pre-correction.md": (
        "planning-artifacts/implementation-readiness-report-2026-07-22.md",
        "le nom le declare lui-meme ; verifie 2026-08-03",
    ),
}

#: Ce que le nommage laisse croire et qui est faux. Ecrit ici pour qu'un lecteur
#: le voie AVANT d'ouvrir le mauvais fichier.
NOT_A_VERSION: dict[str, str] = {
    "planning-artifacts/architecture/architecture-connector-2026-07-10": (
        "colonne vertebrale PLATEFORME (AD-1..AD-34, status final, updated 2026-07-29). "
        "La date du dossier est celle de sa creation, pas de sa derniere verite. "
        "Ce n'est PAS remplace par le dossier du 2026-07-25."
    ),
    "planning-artifacts/architecture/architecture-connector-2026-07-25": (
        "spine de FEATURE -- ingestion de sources fichier (altitude: feature, "
        "CAP-1..CAP-10). Portee differente, pas une version suivante."
    ),
}


def _yaml_tracker() -> dict:
    """Le tracker ET sa moitie reglee -- l'index a besoin de TOUTE l'histoire.

    Depuis le 2026-08-04 `sprint-status.yaml` ne porte plus que ce qui reste a
    faire ; les 424 lignes reglees vivent dans
    `archive/implementation-artifacts/sprint-status-settled.yaml`. Lire la seule
    moitie vivante ferait dire a l'index que 400 stories livrees n'ont jamais eu
    de fichier -- exactement le contresens qu'il existe pour empecher.
    """
    import yaml

    merged: dict[str, dict] = {}
    for path in (TRACKER, SETTLED):
        if not path.is_file():
            continue
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for section, value in loaded.items():
            if isinstance(value, dict):
                merged.setdefault(section, {}).update(value)
            else:
                merged.setdefault(section, value)
    return merged


def _story_num(name: str) -> tuple[int, int] | None:
    m = re.match(r"^(\d+)-(\d+)", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _title_of(path: Path) -> str:
    """Le titre d'un document : premier `# ...`, sinon un champ de frontmatter."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines()[:40]:
        if line.startswith("# "):
            return line[2:].strip()
    for key in ("epicTitle:", "name:", "title:", "scope:"):
        for line in text.splitlines()[:30]:
            if line.strip().startswith(key):
                return line.split(":", 1)[1].strip().strip("'\"")[:120]
    return ""


def _ref_count(needle: str) -> int:
    """Combien de fichiers du depot citent ce chemin -- hors lui-meme."""
    out = subprocess.run(
        ["git", "grep", "-l", "--", needle], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    return sum(1 for line in out.splitlines()
               if needle not in line.split(":")[0] and "INDEX.md" not in line)


def build() -> str:
    ds = _yaml_tracker()["development_status"]
    story_keys = {k: v for k, v in ds.items() if _story_num(k)}
    epic_status = {k: v for k, v in ds.items() if re.match(r"^epic-\d+$", k)}

    files = {p.stem: p for p in IMPL.glob("*.md") if _story_num(p.name)}
    by_num: dict[tuple[int, int], list[str]] = defaultdict(list)
    for stem in files:
        by_num[_story_num(stem)].append(stem)

    epic_docs = {}
    for p in sorted(PLANNING.glob("epic-*.md")):
        m = re.match(r"^epic-(\d+)-", p.name)
        if m:
            epic_docs[int(m.group(1))] = p

    per_epic: dict[int, Counter] = defaultdict(Counter)
    renamed: list[tuple[str, str]] = []
    orphan_keys: list[str] = []
    for key, status in story_keys.items():
        n = _story_num(key)
        per_epic[n[0]][status] += 1
        if key in files:
            continue
        if n in by_num:
            renamed.append((key, by_num[n][0]))
        else:
            orphan_keys.append(key)

    ledger = json.loads((ARCH / "completeness-ledger.json").read_text(encoding="utf-8"))

    out: list[str] = []
    w = out.append
    w("<!-- GENERE par scripts/bmad_index.py -- ne pas editer a la main. -->")
    w("<!-- Regenerer : python scripts/bmad_index.py   |   verifier : --gate -->")
    w("")
    w("# Index de `_bmad-output/` et de la cible ratifiee")
    w("")
    w("**Commencer ici, ne pas `grep`.** Un grep rend le premier fichier qui contient")
    w("le mot, souvent un document remplace ; cette page dit lequel fait foi.")
    w("")
    w("Ordre d'autorite, quand deux documents se contredisent :")
    w("")
    w("1. `docs/product-architecture/` -- la cible ratifiee, **un document par surface** ;")
    w("2. `_bmad-output/planning-artifacts/` -- epics, stories, UX, architecture ;")
    w("3. `_bmad-output/implementation-artifacts/` -- ce qui a ete livre, story par story.")
    w("")
    w("Un ecart entre 1 et 3 est une reconciliation a faire, pas une question a poser")
    w("(CLAUDE.md section 1).")
    w("")

    w("## 1. La cible ratifiee")
    w("")
    w("| Document | Titre | Criteres ouverts |")
    w("| --- | --- | --- |")
    # La correspondance document -> cle de ledger appartient a finished_work_audit :
    # la redériver ici ferait deux verites pour un seul fait (et `datastream` est
    # justement une cle dont le document ne porte pas le nom).
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from finished_work_audit import SURFACES, incomplete_if  # type: ignore
        doc_to_key = {s.doc.name: s.key for s in SURFACES}
    except Exception:  # pragma: no cover - l'index doit survivre a l'audit
        doc_to_key, incomplete_if = {}, None

    for p in sorted(ARCH.glob("*.md")):
        entry = ledger.get(doc_to_key.get(p.name, p.stem))
        if isinstance(entry, dict) and incomplete_if is not None:
            # Une entree est {index: {date, evidence, verdict}}. `verdict: "false"`
            # veut dire « le critere d'incompletude est FAUX », donc ferme.
            #
            # LE DENOMINATEUR EST LE DOCUMENT, PAS LE LEDGER, et c'est tout l'objet
            # de ce bloc. Compter les cles du ledger rendait invisible le critere
            # que PERSONNE n'a juge, alors que le ledger dit lui-meme « un critere
            # non enregistre compte pour VRAI ». Mesure du 2026-08-04 : six surfaces
            # etaient affichees `0 ouverts` ici -- governance, data, overview,
            # analyze-and-test, file-source-ingestion, datastream -- pendant que
            # `finished_work_audit.py` en comptait douze d'ouverts sur les memes
            # documents. L'INDEX est la page que CLAUDE.md section 1 fait lire EN
            # PREMIER : il disait « fini » de six surfaces qui ne l'etaient pas.
            recorded = {
                k: v for k, v in entry.items()
                if not k.startswith("_") and isinstance(v, dict)
            }
            total = max(len(incomplete_if(p)), len(recorded))
            closed = sum(1 for c in recorded.values() if str(c.get("verdict")).lower() == "false")
            opened = total - closed
            crit = f"**{opened}** ouverts / {total}" if opened else f"0 / {total}"
        elif "incomplete if" in p.read_text(encoding="utf-8", errors="replace").lower():
            crit = "⚠️ hors ledger — comptent TOUS ouverts"
        else:
            crit = "—"
        w(f"| `{p.name}` | {_title_of(p)[:70]} | {crit} |")
    w("")
    w("Le denominateur est le nombre de puces « Incomplete if » du **document**, pas")
    w("le nombre d'entrees du ledger : un critere que personne n'a juge compte")
    w("OUVERT. C'est la regle du ledger lui-meme, et la seule qui s'accorde avec")
    w("`python scripts/finished_work_audit.py`. Un document qui porte un")
    w("« Incomplete if » sans aucune entree au ledger a donc **tous** ses criteres")
    w("comptes ouverts.")
    w("")
    w("Capacites (`capabilities/`) suivies au ledger : "
      + ", ".join(f"`{k}`" for k in sorted(ledger) if not k.startswith("_")
                  and not (ARCH / f"{k}.md").exists()) + ".")
    w("")

    w("## 2. Les epics")
    w("")
    w("`etat` vient du tracker. `fichiers` compte les stories qui ont un fichier au nom")
    w("EXACT de leur cle -- les autres sont listees en section 4.")
    w("")
    w("| Epic | Document de planification | Etat | Stories (tracker) | fichiers |")
    w("| --- | --- | --- | --- | --- |")
    for n in sorted(set(list(per_epic) + list(epic_docs))):
        doc = epic_docs.get(n)
        counts = per_epic.get(n, Counter())
        detail = ", ".join(f"{s}:{c}" for s, c in counts.most_common()) or "—"
        exact = sum(1 for k in story_keys if _story_num(k)[0] == n and k in files)
        w(f"| {n} | {f'`{doc.name}`' if doc else '**aucun**'} "
          f"| {epic_status.get(f'epic-{n}', '—')} | {detail} | {exact} |")
    w("")

    w("## 3. Documents transverses")
    w("")
    w("| Document | Nature | References | Remarque |")
    w("| --- | --- | --- | --- |")
    families = [
        ("architecture/architecture-connector-*", "architecture"),
        ("ux-designs/ux-*", "UX"),
        ("spike-*.md", "spike"),
        ("implementation-readiness-report-*.md", "revue de preparation"),
        ("sprint-change-proposal-*.md", "changement de sprint"),
    ]
    for pattern, nature in families:
        for p in sorted(PLANNING.glob(pattern)):
            rel = f"planning-artifacts/{p.relative_to(PLANNING).as_posix()}"
            note = ""
            if rel in SUPERSEDED:
                succ, why = SUPERSEDED[rel]
                note = f"**REMPLACE** par `{succ.split('/')[-1]}` — {why}"
            elif rel in NOT_A_VERSION:
                note = f"⚠️ {NOT_A_VERSION[rel]}"
            w(f"| `{rel}` | {nature} | {_ref_count(rel.split('/')[-1])} | {note} |")
    w("")

    w("## 4. Ce qu'une recherche par cle ne trouve PAS")
    w("")
    w(f"**{len(renamed)} stories** dont le fichier porte un autre nom que leur cle.")
    w("Chercher la cle ne rend rien ; le fichier existe.")
    w("")
    w("| Cle au tracker | Fichier reel |")
    w("| --- | --- |")
    for key, stem in sorted(renamed):
        w(f"| `{key}` | `{stem}.md` |")
    w("")
    w(f"**{len(orphan_keys)} stories du tracker n'ont aucun fichier.** Ce n'est pas une")
    w("recherche ratee : le fichier n'a jamais ete ecrit (route `quick-dev`, ou story")
    w("fermee sans redaction). Les chercher est une perte de temps.")
    w("")
    w("<details><summary>La liste</summary>")
    w("")
    for key in sorted(orphan_keys, key=lambda k: _story_num(k)):
        w(f"- `{key}` — {story_keys[key]}")
    w("")
    w("</details>")
    w("")

    archive = BMAD / "archive"
    if archive.exists():
        w("## 5. Ce qui a ete ecarte")
        w("")
        w("`_bmad-output/archive/` -- vrai AU PASSE, plus la volonte. Rien n'y est")
        w("supprime : tout y est arrive par `git mv`. **Regle d'entree : seulement ce que")
        w("personne ne cite.** Voir `archive/README.md`.")
        w("")
        w("| Lot | Fichiers |")
        w("| --- | --- |")
        for d in sorted(p for p in archive.iterdir() if p.is_dir()):
            w(f"| `archive/{d.name}/` | {sum(1 for _ in d.rglob('*') if _.is_file())} |")
        w("")

    w("## 6. Volumetrie")
    w("")
    w("| Repertoire | Fichiers |")
    w("| --- | --- |")
    for d in sorted(BMAD.iterdir()):
        if d.is_dir():
            w(f"| `_bmad-output/{d.name}/` | {sum(1 for _ in d.rglob('*') if _.is_file())} |")
    w(f"| `docs/product-architecture/` | {sum(1 for _ in ARCH.rglob('*') if _.is_file())} |")
    w("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", action="store_true",
                    help="non-zero si INDEX.md ne correspond plus au depot")
    args = ap.parse_args()

    fresh = build()
    if args.gate:
        current = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
        if current != fresh:
            print("INDEX.md est perime -- `python scripts/bmad_index.py` le regenere.",
                  file=sys.stderr)
            return 1
        print("INDEX.md est a jour")
        return 0

    INDEX.write_text(fresh, encoding="utf-8")
    print(f"{INDEX.relative_to(ROOT)} regenere ({len(fresh):,} octets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
