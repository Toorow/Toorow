"""Le rapport de conformite que le DEPOT produit, hors ligne, sans aucun compte.

POURQUOI CE FICHIER EXISTE
==========================
Aujourd'hui la seule chose qui peut lever ``public_catalog.verification: blocked``
est ``scripts/ratify_connector.py`` -- une sonde LIVE, human-gated, qui exige un
compte reel et un consentement. Personne ne dispose des 38 comptes, donc AUCUN
connecteur n'a jamais ete ratifie et les 38 restent ``blocked``. Un dossier de
preuve qui ne peut pas etre produit n'est pas un dossier de preuve.

Ce script produit l'autre moitie : celle que le depot peut prouver tout seul.
Il rejoue le balayage de conformance sur les 38 modules et ecrit
``reports/conformance-<date>.json`` -- une entree par connecteur, un verdict
pass/fail/skip par couche, avec le message qui l'explique.

CE QU'IL PROUVE
---------------
* le manifeste valide son schema (couche ``manifest``) ;
* l'enveloppe rendue a la forme attendue (``envelope``) ;
* les report-packs se valident contre leur schema (``reports``) ;
* les lignes brutes du golden rejouees dans ``transform()`` donnent exactement
  les faits canoniques attendus (``golden_pull``) ;
* les valeurs de dimension declarees existent dans le vocabulaire seed
  (``vocabulary``) ;
* le golden de contexte rejoue ``pull()`` avec des I/O bouchonnees et verifie le
  tamponnage platform/source (``context_events``) ;
* le connecteur est APPELABLE par le worker tel que le worker appelle
  (``pull_contract``).

CE QU'IL NE PROUVE PAS -- ET C'EST LE POINT, PAS L'EXCUSE
---------------------------------------------------------
1. **L'API distante ne repond jamais ici.** Aucun octet ne quitte la machine.
   Qu'un champ declare dans ``api_catalog.json`` existe encore chez le
   fournisseur, qu'un rapport accepte la combinaison metrique x dimension
   declaree, qu'un quota se comporte comme annonce : rien de tout cela n'est
   teste. C'est exactement le perimetre de ``ratify_connector.py``, et il reste
   entier.
2. **Les fixtures golden sont ecrites par nous.** Elles prouvent que
   ``transform()`` est stable et conforme au contrat canonique ; elles ne
   prouvent pas que la forme brute qu'elles imitent est celle que le fournisseur
   envoie reellement aujourd'hui.
3. **La taxonomie d'erreur est verifiee comme DECLARATION, pas comme
   comportement.** Personne n'a fait retourner un 429 par le fournisseur.
4. **Un verdict ``skip`` n'est pas un verdict vert.** C'est une couche qui n'a
   rien mesure -- soit parce qu'elle est structurellement sans objet pour ce
   connecteur (``golden_pull`` sur un connecteur purement contextuel), soit
   parce qu'elle n'a pas trouve de quoi mesurer. Le resume compte les deux
   separement : ``fully_green`` (aucun ``fail``) et ``green_no_skips``.
5. **Certaines couches portent des tests non parametres par module** (le
   vocabulaire verifie aussi les seeds du depot). Ils tournent a l'identique
   pour chaque connecteur : un echec la-dedans rougit les 38 lignes. C'est
   volontaire et honnete -- mais ce n'est pas 38 defauts, c'est un seul.

COORDINATION
------------
Le balayage a deux regimes, et le script DETECTE lequel s'applique au lieu de
le supposer :

* une couche deja **parametree par module** (``test_pull_contract.py``
  aujourd'hui, et bientot les six autres si la parametrisation de
  ``conftest.py`` atterrit) est lancee UNE FOIS et ses resultats sont attribues
  par l'identifiant de parametre ;
* une couche qui exige encore ``--module-path`` est lancee 38 fois, une par
  module.

La detection se fait par ``pytest --collect-only`` SANS ``--module-path`` : si
au moins la moitie des noms de modules apparaissent en parametre, la couche est
consideree comme balayante.

USAGE
-----
    python scripts/conformance_report.py
    python scripts/conformance_report.py --gate-mode regression
    python scripts/conformance_report.py --baseline reports/conformance-2026-07-31.json

CODE DE SORTIE
--------------
* ``0`` : la porte est verte selon ``--gate-mode``.
* ``1`` : au moins une cellule est rouge (``--gate-mode any-failure``, defaut),
  ou au moins une cellule a REGRESSE par rapport a la baseline
  (``--gate-mode regression``).
* ``2`` : le balayage lui-meme n'a pas pu tourner (pytest introuvable, erreur de
  collecte...). Un rapport partiel est tout de meme ecrit.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_MODULES_DIR = _ROOT / "server" / "modules"
_CONFORMANCE_DIR = _ROOT / "server" / "tests" / "conformance"

# Les sept couches, dans l'ordre ou elles sont rapportees. La cle est le nom de
# couche du rapport ; la valeur est le fichier de test qui la porte.
LAYERS: list[tuple[str, str]] = [
    ("manifest", "test_manifest.py"),
    ("envelope", "test_envelope.py"),
    ("reports", "test_reports.py"),
    ("golden_pull", "test_golden_pull.py"),
    ("vocabulary", "test_vocabulary.py"),
    ("context_events", "test_context_events.py"),
    ("pull_contract", "test_pull_contract.py"),
]

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_SKIP = "skip"
VERDICT_MISSING = "missing"  # la couche n'a produit aucun test pour ce module

MESSAGE_MAX = 600

CANNOT_PROVE = [
    "L'API distante ne repond jamais dans ce balayage : aucun appel reseau n'est "
    "emis. Qu'un champ declare existe encore chez le fournisseur, qu'un rapport "
    "accepte la combinaison metrique x dimension declaree, qu'un quota se "
    "comporte comme annonce -- rien de cela n'est teste ici. C'est le perimetre "
    "de scripts/ratify_connector.py, qui exige un compte reel.",
    "Les fixtures golden sont ecrites par nous : elles prouvent la stabilite de "
    "transform()/pull(), pas que la forme brute imitee est celle que le "
    "fournisseur envoie aujourd'hui.",
    "La taxonomie d'erreur est verifiee comme DECLARATION (error_map present et "
    "coherent), jamais comme comportement : aucun 429 ni 401 reel n'a ete "
    "provoque.",
    "Un verdict 'skip' ne prouve rien. Il signale une couche sans objet pour ce "
    "connecteur, ou une couche qui n'a pas trouve de quoi mesurer. Le resume le "
    "compte a part (green_no_skips).",
    "Certains tests d'une couche ne sont pas parametres par module (le "
    "vocabulaire verifie aussi les seeds du depot). Ils tournent a l'identique "
    "pour les 38 connecteurs : un echec la-dedans rougit les 38 lignes sans "
    "etre 38 defauts.",
]


# ---------------------------------------------------------------------------
# Decouverte des modules
# ---------------------------------------------------------------------------


def discover_modules(modules_dir: Path) -> list[str]:
    """Retourne les noms de dossiers de module portant un connector.py."""
    names = []
    for child in sorted(modules_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if (child / "connector.py").exists() and (child / "manifest.json").exists():
            names.append(child.name)
    return names


# ---------------------------------------------------------------------------
# Invocation pytest
# ---------------------------------------------------------------------------


def _pytest_cmd(extra: list[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-o",
        "junit_family=xunit2",
        "--no-header",
        "-q",
        *extra,
    ]


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


_PARAM_RE = re.compile(r"\[([^\]]+)\]\s*$")


def _param_of(name: str) -> str | None:
    m = _PARAM_RE.search(name)
    return m.group(1) if m else None


def detect_sweeping_layers(
    layers: list[tuple[str, str]], modules: list[str], root: Path
) -> tuple[dict[str, bool], list[str]]:
    """Determine, par collecte seche, quelles couches balaient deja les modules.

    Retourne ({layer: is_sweeping}, notes). Une couche est declaree balayante si
    au moins la moitie des noms de modules apparait comme identifiant de
    parametre dans ses node ids collectes SANS --module-path.
    """
    sweeping: dict[str, bool] = {}
    notes: list[str] = []
    module_set = set(modules)
    for layer, filename in layers:
        path = _CONFORMANCE_DIR / filename
        if not path.exists():
            sweeping[layer] = False
            notes.append(f"{layer}: {filename} absent du depot")
            continue
        proc = _run(
            _pytest_cmd(["--collect-only", str(path.relative_to(root))]), root
        )
        params: set[str] = set()
        for line in proc.stdout.splitlines():
            if "::" not in line:
                continue
            p = _param_of(line.strip())
            if p and p in module_set:
                params.add(p)
        is_sweep = len(params) >= max(1, len(modules) // 2)
        sweeping[layer] = is_sweep
        notes.append(
            f"{layer}: {'parametree par module' if is_sweep else 'exige --module-path'} "
            f"({len(params)}/{len(modules)} modules vus a la collecte seche)"
        )
    return sweeping, notes


# ---------------------------------------------------------------------------
# Lecture du junit xml
# ---------------------------------------------------------------------------


def _sanitize(text: str) -> str:
    """Retire le chemin absolu de la machine du message.

    Le rapport est un artefact COMMITTE : un `C:\\Users\\<moi>\\...` dedans est une
    reference exterieure au sens de la regle « le depot doit rester partageable ».
    On le remplace par le chemin relatif a la racine du depot.
    """
    if not text:
        return ""
    for variant in (str(_ROOT), _ROOT.as_posix()):
        text = text.replace(variant + "\\", "").replace(variant + "/", "")
        text = text.replace(variant, "<repo>")
    return text


def _truncate(text: str) -> str:
    text = " ".join(_sanitize(text or "").split())
    return text if len(text) <= MESSAGE_MAX else text[: MESSAGE_MAX - 3] + "..."


def parse_junit(xml_path: Path) -> list[dict[str, Any]]:
    """Retourne [{file, name, param, outcome, message}] depuis un junit xml."""
    if not xml_path.exists():
        return []
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return []
    out: list[dict[str, Any]] = []
    for case in tree.iter("testcase"):
        classname = case.get("classname") or ""
        # tests.conformance.test_manifest -> test_manifest
        file_stem = classname.split(".")[-1] if classname else ""
        name = case.get("name") or ""
        outcome = "pass"
        message = ""
        for child in case:
            tag = child.tag
            if tag in ("failure", "error", "skipped"):
                outcome = "fail" if tag != "skipped" else "skip"
                # L'attribut `message` porte la raison ; `text` porte la trace
                # complete (et, pour un skip, le chemin:ligne + la meme phrase).
                # On prend l'attribut et on ne retombe sur le texte que s'il est
                # vide, sinon chaque message est double.
                message = _truncate(child.get("message") or child.text or "")
                break
        out.append(
            {
                "file": file_stem,
                "name": name,
                "param": _param_of(name),
                "outcome": outcome,
                "message": message,
            }
        )
    return out


def _file_to_layer(layers: list[tuple[str, str]]) -> dict[str, str]:
    return {Path(f).stem: layer for layer, f in layers}


# ---------------------------------------------------------------------------
# Verdict d'une cellule (module x couche)
# ---------------------------------------------------------------------------


def cell_verdict(cases: list[dict[str, Any]]) -> dict[str, Any]:
    if not cases:
        return {
            "verdict": VERDICT_MISSING,
            "message": "aucun test collecte pour cette couche sur ce connecteur",
            "tests": {"passed": 0, "failed": 0, "skipped": 0},
        }
    failed = [c for c in cases if c["outcome"] == "fail"]
    skipped = [c for c in cases if c["outcome"] == "skip"]
    passed = [c for c in cases if c["outcome"] == "pass"]
    counts = {"passed": len(passed), "failed": len(failed), "skipped": len(skipped)}
    if failed:
        message = " | ".join(f"{c['name']}: {c['message']}" for c in failed[:3])
        return {"verdict": VERDICT_FAIL, "message": _truncate(message), "tests": counts}
    if passed:
        if skipped:
            msg = (
                f"{len(passed)} test(s) verts, {len(skipped)} saute(s): "
                + _truncate(skipped[0]["message"])
            )
        else:
            msg = f"{len(passed)} test(s) verts"
        return {"verdict": VERDICT_PASS, "message": msg, "tests": counts}
    return {
        "verdict": VERDICT_SKIP,
        "message": _truncate(skipped[0]["message"]) if skipped else "tout saute",
        "tests": counts,
    }


# ---------------------------------------------------------------------------
# Balayage
# ---------------------------------------------------------------------------


def sweep(
    modules: list[str],
    layers: list[tuple[str, str]],
    root: Path,
    workdir: Path,
    *,
    force_per_module: bool = False,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]], list[str], bool]:
    """Execute le balayage. Retourne (cells, suite_level, notes, harness_ok).

    ``cells`` est {module: {layer: verdict_dict}}.
    ``suite_level`` porte les tests d'une couche balayante qui ne sont pas
    parametres par module (contraintes globales du depot).
    """
    file_to_layer = _file_to_layer(layers)
    sweeping, notes = detect_sweeping_layers(layers, modules, root)
    if force_per_module:
        sweeping = {layer: False for layer, _ in layers}
        notes.append(
            "--force-per-module: detection ignoree, chaque couche est relancee "
            "une fois par module via --module-path (regime de repli)."
        )
    harness_ok = True

    cells: dict[str, dict[str, dict[str, Any]]] = {
        m: {layer: None for layer, _ in layers} for m in modules  # type: ignore[misc]
    }
    suite_level: list[dict[str, Any]] = []
    module_set = set(modules)
    # Le filtre d'attribution doit connaitre TOUS les modules du depot, pas
    # seulement ceux demandes par --modules : sinon un `--modules gsc` laisse
    # passer l'echec parametre de `dv360` dans la cellule de `gsc`.
    known_modules = set(discover_modules(_MODULES_DIR)) | module_set

    # --- 1. Les couches deja parametrees : une seule invocation ---------------
    sweep_files = [
        str((_CONFORMANCE_DIR / f).relative_to(root))
        for layer, f in layers
        if sweeping.get(layer) and (_CONFORMANCE_DIR / f).exists()
    ]
    if sweep_files:
        xml = workdir / "sweep.xml"
        proc = _run(_pytest_cmd([*sweep_files, f"--junitxml={xml}"]), root)
        cases = parse_junit(xml)
        if not cases:
            harness_ok = False
            notes.append(
                "ECHEC HARNAIS: l'invocation balayante n'a produit aucun cas de "
                f"test (code {proc.returncode}). stderr: {_truncate(proc.stderr)}"
            )
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for c in cases:
            layer = file_to_layer.get(c["file"])
            if layer is None:
                continue
            if c["param"] in known_modules:
                if c["param"] in module_set:
                    buckets.setdefault((c["param"], layer), []).append(c)
                # sinon: module hors selection --modules, on l'ignore.
            else:
                # Test non parametre par module : contrainte GLOBALE du depot
                # (les seeds de vocabulaire, par exemple). Ne pas la recopier sur
                # les 38 lignes -- un echec la n'est pas 38 defauts.
                suite_level.append({"layer": layer, **c})
        for (mod, layer), group in buckets.items():
            cells[mod][layer] = cell_verdict(group)

    # --- 2. Les couches qui exigent --module-path : une invocation par module -
    path_files = [
        (layer, str((_CONFORMANCE_DIR / f).relative_to(root)))
        for layer, f in layers
        if not sweeping.get(layer) and (_CONFORMANCE_DIR / f).exists()
    ]
    if path_files:
        files_only = [p for _, p in path_files]
        for i, mod in enumerate(modules, 1):
            xml = workdir / f"mod_{mod}.xml"
            module_path = (_MODULES_DIR / mod).as_posix()
            proc = _run(
                _pytest_cmd(
                    [*files_only, "--module-path", module_path, f"--junitxml={xml}"]
                ),
                root,
            )
            cases = parse_junit(xml)
            if not cases:
                harness_ok = False
                notes.append(
                    f"ECHEC HARNAIS ({mod}): aucun cas de test produit "
                    f"(code {proc.returncode}). stderr: {_truncate(proc.stderr)}"
                )
            grouped: dict[str, list[dict[str, Any]]] = {}
            for c in cases:
                layer = file_to_layer.get(c["file"])
                if layer is None:
                    continue
                # Un fichier peut se PARAMETRER LUI-MEME sur les 38 modules et
                # ignorer --module-path (c'est le cas de test_pull_contract.py).
                # Sans ce filtre, la cellule de `gsc` heriterait de l'echec de
                # `cm360` -- 38 lignes rouges pour un seul defaut. Mesure verifiee
                # le 2026-07-31 avec --force-per-module.
                if c["param"] in known_modules and c["param"] != mod:
                    continue
                grouped.setdefault(layer, []).append(c)
            for layer, _f in path_files:
                cells[mod][layer] = cell_verdict(grouped.get(layer, []))
            print(
                f"  [{i:>2}/{len(modules)}] {mod:<24} "
                + " ".join(
                    f"{layer}={cells[mod][layer]['verdict']}" for layer, _ in path_files
                ),
                flush=True,
            )

    # Cellules jamais renseignees (couche absente du depot).
    for mod in modules:
        for layer, _f in layers:
            if cells[mod][layer] is None:
                cells[mod][layer] = {
                    "verdict": VERDICT_MISSING,
                    "message": "couche non executee (fichier de test absent)",
                    "tests": {"passed": 0, "failed": 0, "skipped": 0},
                }

    return cells, suite_level, notes, harness_ok


# ---------------------------------------------------------------------------
# Assemblage du rapport
# ---------------------------------------------------------------------------


def build_report(
    *,
    cells: dict[str, dict[str, dict[str, Any]]],
    suite_level: list[dict[str, Any]],
    notes: list[str],
    layers: list[tuple[str, str]],
    generated_at: str,
    harness_ok: bool,
) -> dict[str, Any]:
    layer_names = [layer for layer, _ in layers]
    connectors: dict[str, Any] = {}
    fully_green = []
    green_no_skips = []
    for mod in sorted(cells):
        per_layer = cells[mod]
        verdicts = [per_layer[layer]["verdict"] for layer in layer_names]
        has_fail = any(v in (VERDICT_FAIL, VERDICT_MISSING) for v in verdicts)
        has_skip = any(v == VERDICT_SKIP for v in verdicts)
        if not has_fail:
            fully_green.append(mod)
            if not has_skip:
                green_no_skips.append(mod)
        connectors[mod] = {
            "fully_green": not has_fail,
            "failing_layers": [
                layer
                for layer in layer_names
                if per_layer[layer]["verdict"] in (VERDICT_FAIL, VERDICT_MISSING)
            ],
            "skipped_layers": [
                layer for layer in layer_names if per_layer[layer]["verdict"] == VERDICT_SKIP
            ],
            "layers": {layer: per_layer[layer] for layer in layer_names},
        }

    per_layer_totals = {
        layer: {
            "pass": sum(1 for m in cells if cells[m][layer]["verdict"] == VERDICT_PASS),
            "fail": sum(1 for m in cells if cells[m][layer]["verdict"] == VERDICT_FAIL),
            "skip": sum(1 for m in cells if cells[m][layer]["verdict"] == VERDICT_SKIP),
            "missing": sum(
                1 for m in cells if cells[m][layer]["verdict"] == VERDICT_MISSING
            ),
        }
        for layer in layer_names
    }

    suite_failures = [c for c in suite_level if c["outcome"] == "fail"]

    return {
        "schema_version": "1",
        "generated_at": generated_at,
        "harness_ok": harness_ok,
        "layers": layer_names,
        "summary": {
            "connectors_total": len(cells),
            "fully_green": len(fully_green),
            "fully_green_modules": fully_green,
            "green_no_skips": len(green_no_skips),
            "green_no_skips_modules": green_no_skips,
            "per_layer": per_layer_totals,
            "suite_level_failures": len(suite_failures),
        },
        "sweep_notes": notes,
        "suite_level_tests": suite_level,
        "cannot_prove": CANNOT_PROVE,
        "connectors": connectors,
    }


# ---------------------------------------------------------------------------
# Baseline / regression
# ---------------------------------------------------------------------------


def find_previous_report(reports_dir: Path, exclude: Path | None) -> Path | None:
    if not reports_dir.is_dir():
        return None
    candidates = sorted(
        p
        for p in reports_dir.glob("conformance-*.json")
        if exclude is None or p.resolve() != exclude.resolve()
    )
    return candidates[-1] if candidates else None


def compute_regressions(report: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Liste les cellules qui etaient vertes dans la baseline et ne le sont plus."""
    regressions: list[str] = []
    base_conn = baseline.get("connectors", {})
    for mod, entry in report["connectors"].items():
        base_layers = base_conn.get(mod, {}).get("layers", {})
        for layer, cell in entry["layers"].items():
            was = base_layers.get(layer, {}).get("verdict")
            now = cell["verdict"]
            if was == VERDICT_PASS and now != VERDICT_PASS:
                regressions.append(f"{mod}/{layer}: {was} -> {now}")
            elif was == VERDICT_SKIP and now in (VERDICT_FAIL, VERDICT_MISSING):
                regressions.append(f"{mod}/{layer}: {was} -> {now}")
    return sorted(regressions)


# ---------------------------------------------------------------------------
# Rendu console
# ---------------------------------------------------------------------------


def print_table(report: dict[str, Any]) -> None:
    layer_names = report["layers"]
    short = {
        "manifest": "man",
        "envelope": "env",
        "reports": "rep",
        "golden_pull": "gold",
        "vocabulary": "voc",
        "context_events": "ctx",
        "pull_contract": "pull",
    }
    symbol = {
        VERDICT_PASS: "OK ",
        VERDICT_FAIL: "FAIL",
        VERDICT_SKIP: "skip",
        VERDICT_MISSING: "----",
    }
    header = f"{'connector':<24}" + "".join(
        f"{short.get(layer, layer[:4]):<6}" for layer in layer_names
    )
    print()
    print(header)
    print("-" * len(header))
    for mod, entry in report["connectors"].items():
        row = f"{mod:<24}"
        for layer in layer_names:
            row += f"{symbol[entry['layers'][layer]['verdict']]:<6}"
        print(row)
    print("-" * len(header))
    s = report["summary"]
    print(
        f"{s['fully_green']}/{s['connectors_total']} connecteurs sans aucune couche "
        f"rouge ; {s['green_no_skips']}/{s['connectors_total']} verts SANS aucun skip."
    )
    print()
    print("Ecarts par connecteur (couches rouges) :")
    any_gap = False
    for mod, entry in report["connectors"].items():
        if entry["failing_layers"]:
            any_gap = True
            for layer in entry["failing_layers"]:
                print(f"  {mod:<24} {layer:<15} {entry['layers'][layer]['message'][:160]}")
    if not any_gap:
        print("  (aucun)")
    if report["summary"]["suite_level_failures"]:
        print()
        print("Echecs NON imputables a un connecteur (contraintes globales du depot) :")
        for c in report["suite_level_tests"]:
            if c["outcome"] == "fail":
                print(f"  {c['layer']}/{c['name']}: {c['message'][:160]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Chemin du rapport (defaut: reports/conformance-<date>.json).",
    )
    p.add_argument(
        "--modules",
        default=None,
        help="Liste de modules separes par des virgules (defaut: les 38 decouverts).",
    )
    p.add_argument(
        "--gate-mode",
        choices=("any-failure", "regression"),
        default="any-failure",
        help=(
            "any-failure (defaut): sortie non nulle des qu'une cellule est rouge. "
            "regression: sortie non nulle seulement si une cellule verte de la "
            "baseline ne l'est plus."
        ),
    )
    p.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Rapport de reference (defaut: le precedent reports/conformance-*.json).",
    )
    p.add_argument(
        "--force-per-module",
        action="store_true",
        help=(
            "Ignore la detection et relance chaque couche une fois par module via "
            "--module-path. Sert a PROUVER que le regime de repli fonctionne, "
            "avant comme apres la parametrisation de conftest.py."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not _MODULES_DIR.is_dir():
        print(f"ERREUR: {_MODULES_DIR} introuvable", file=sys.stderr)
        return 2

    modules = (
        [m.strip() for m in args.modules.split(",") if m.strip()]
        if args.modules
        else discover_modules(_MODULES_DIR)
    )
    if not modules:
        print("ERREUR: aucun module decouvert", file=sys.stderr)
        return 2

    generated_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    out_path = args.out or (_ROOT / "reports" / f"conformance-{date.today().isoformat()}.json")

    # Un second passage le MEME jour ecrase le rapport du jour : on le lit AVANT
    # d'ecrire pour qu'il puisse servir de baseline implicite. Sans cela, deux
    # passages du meme jour ne comparent rien et la porte 'regression' est muette
    # exactement quand elle sert -- pendant qu'on repare.
    same_day_previous: dict[str, Any] | None = None
    if out_path.exists():
        try:
            same_day_previous = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            same_day_previous = None

    print(f"toorow -- rapport de conformite hors ligne ({len(modules)} connecteurs)")
    print(f"racine : {_ROOT}")

    workdir = Path(tempfile.mkdtemp(prefix="toorow-conformance-"))
    try:
        cells, suite_level, notes, harness_ok = sweep(
            modules, LAYERS, _ROOT, workdir, force_per_module=args.force_per_module
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    for n in notes:
        print(f"  . {n}")

    report = build_report(
        cells=cells,
        suite_level=suite_level,
        notes=notes,
        layers=LAYERS,
        generated_at=generated_at,
        harness_ok=harness_ok,
    )

    baseline_path = args.baseline or find_previous_report(out_path.parent, out_path)
    baseline: dict[str, Any] | None = None
    baseline_label: str | None = None
    if baseline_path and baseline_path.exists():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline_label = baseline_path.as_posix().replace(_ROOT.as_posix() + "/", "")
        except (OSError, json.JSONDecodeError):
            baseline = None
    if baseline is None and same_day_previous is not None:
        baseline = same_day_previous
        baseline_label = (
            out_path.as_posix().replace(_ROOT.as_posix() + "/", "")
            + f" (passage precedent du meme jour, {same_day_previous.get('generated_at')})"
        )
    report["baseline"] = baseline_label
    report["regressions"] = compute_regressions(report, baseline) if baseline else []

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    print_table(report)
    print()
    print("Ce que ce rapport NE PEUT PAS prouver :")
    for line in CANNOT_PROVE:
        print(f"  - {line}")
    print()
    print(f"Rapport ecrit : {out_path}")

    if not harness_ok:
        print("ERREUR: le harnais lui-meme a echoue (voir sweep_notes).", file=sys.stderr)
        return 2

    if args.gate_mode == "regression":
        regressions = report.get("regressions") or []
        if report.get("baseline") is None:
            print("Aucune baseline : la porte 'regression' passe par defaut.")
            return 0
        if regressions:
            print(f"REGRESSIONS ({len(regressions)}) :", file=sys.stderr)
            for r in regressions:
                print(f"  - {r}", file=sys.stderr)
            return 1
        return 0

    red = report["summary"]["connectors_total"] - report["summary"]["fully_green"]
    if red or report["summary"]["suite_level_failures"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
