#!/usr/bin/env python3
"""What each recorded verdict OFFERS as evidence — a command, or a sentence.

    python scripts/ledger_verdict_census.py             # per surface + totals
    python scripts/ledger_verdict_census.py --prose-only
    python scripts/ledger_verdict_census.py --traversals
    python scripts/ledger_verdict_census.py --json
    python scripts/ledger_verdict_census.py --gate      # ratchet: no NEW prose verdict
    python scripts/ledger_verdict_census.py --baseline  # print today's prose set

WHY THIS EXISTS. `docs/product-architecture/completeness-ledger.json` closes a
ratified criterion, and its own `_README` states the rule: *"Evidence is the
command, route or screen that produced the verdict — not an assertion."*
Nothing checked it. `finished_work_audit.py` reads the same file and asks only
whether `verdict == "false"` and `evidence` is a non-empty string, so a sentence
about the code closes a criterion exactly as well as a command that ran.

Measured on 2026-08-31, and this script is the command that measures it:
**165 of 761 recorded verdicts carry no command at all**, and eight cite an
address of the deployed product. Eleven surfaces read DONE.

Re-measured on 2026-09-01, same command: **108 of 761**, nine traversals. LOT 1
of the rewriting took the two worst surfaces -- governance (29 prose) and
file-source-ingestion (28) -- to zero, by deriving the command each clause needs
today and RUNNING it. Five of the 28 could not be closed that way and were
REOPENED with the missing thing named, which is the only honest end for a
verdict whose proof no longer replays. That is the state
`element-control-loop.md` §6 forbids in one line — *a status is a computed
state, never a claim* — and the whole of AI-334.

**Finished on 2026-09-01: 0 of 760.** Lot 2 took seven more surfaces to zero and
lot 3 the last fifteen. Every recorded verdict now offers a command or an
address of the deployed product, the ratchet's baseline is EMPTY, and the rule
this file exists for is enforced for the first time rather than merely stated.
Eight of lot 3's rewrites are verdicts that stay OPEN: an open criterion owes a
command too — the one that MEASURES the gap, so that the day it closes is a
computed day and not a remembered one. One criterion REOPENED under the
rewriting (`caveats-register[0]`), which is what the census is for.

WHAT IT DOES NOT DO. It does not rewrite a verdict, and it must not: the words
of a recorded verdict are the record of what someone measured, and replacing
them from outside is how a ledger becomes fiction. It classifies, it counts, and
it holds the line while the rewriting happens elsewhere.

HOW EACH VERDICT IS READ. Three buckets, exclusive, in this order.

**traversal** — the evidence names an ADDRESS OF THE DEPLOYED PRODUCT *and* a
result read back from it. Both halves are required, and the address must be an
address, never a word:

    address  https://….run.app / ….web.app, `gcloud run services …`,
             a QA-journey step id (`G9-T07`), a recorded run artefact under
             `_bmad-output/qa/runs/`, or a named Cloud Run revision
    result   an HTTP status, PASS/FAIL of the step, the revision that answered,
             a minted identifier, or the run artefact itself

The word `production` is deliberately NOT an address. Ten recorded verdicts use
it and six of them say the OPPOSITE of a traversal — *"the loop is dead in
production"*, *"the `.py` path is NOT armed in production"*, *"nothing here is a
claim about production"*. Matching the word would have filed those six as
traversals of a path nobody walked, which is worse than counting none.

**command** — the evidence carries something a reader can run: `pytest`,
`python -m …`, `python scripts/…`, `npx vitest`, `npx tsc`, `dbt build`,
`psql`, `bq query`, `gcloud …`, `curl -…`, `grep -…`, or a `cd server && …`
prologue. This is the ledger's own stated bar, no higher.

**prose** — neither. The sentence may be true, careful and detailed; several of
these are the best-written entries in the file. It cannot be re-run, so it
cannot be re-measured, and §4 of `element-control-loop.md` is about exactly the
elements that regress after someone believed them.

THE RATCHET. `PROSE_VERDICTS_AT_2026_08_31` is the set measured the day this was
written. It turns ONE WAY. A verdict that gains a command must be deleted from
it, and a NEW prose verdict is refused at the moment it is written — the only
moment adding a line to it is free.

WHAT THIS CENSUS CANNOT SEE, named rather than left to be discovered:

* a command that is quoted but was never run, or was run against a fixture
  production does not use. `ledger_evidence_reachability.py` attacks one half of
  that and `quiet_guard_census.py` the other; neither is this one;
* a traversal genuinely performed and written up without its address. It counts
  as `command`, and the fix is to write the address, not to loosen the rule;
* the ratchet is keyed by `surface[index]`, the ledger's own addressing. A
  criterion inserted mid-list re-points every key under it — which
  `scripts/check_ledger_anchors.py` refuses first, and loudly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "docs" / "product-architecture" / "completeness-ledger.json"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

TRAVERSAL = "traversal"
COMMAND = "command"
PROSE = "prose"

#: An address of the RUNNING product. Every one of these names a thing that
#: answers, not a thing that is described.
_DEPLOYED_ADDRESS = re.compile(
    r"""(?xi)
      https?://[^\s`'"]*\.(?:run\.app|web\.app)\b   # Cloud Run service, hosted console
    | \bgcloud\s+run\s+services\b                   # the deployed revision, read back
    | \bG\d{1,2}-T\d{2}\b                           # a QA-journey step, run on the deployment
    | _bmad-output/qa/runs/                         # the artefact one of those runs wrote
    | \brevision\s+mcp-server-\d+                   # the revision that answered
    """
)

#: Something read BACK from that address. An address with no result is a
#: description of where the product lives.
_RESULT_READ_BACK = re.compile(
    r"""(?x)
      ->\s*\d{3}\b
    | \b(?:200|201|202|204|301|302|400|401|403|404|409|422|429|500|502|503)\b
    | \bPASS\b | \bpass\b | \bFAIL\b | \bKO\b
    | \brevision\s+\S+
    | \.json\b
    | \b[a-z]{2,5}_[0-9A-Z]{20,}\b                  # an identifier minted over there
    """
)

#: A command a reader can run. The ledger's own bar, no higher: `_README` asks
#: for "the command, route or screen that produced the verdict".
_COMMAND = re.compile(
    r"""(?xi)
      \b(?:uv\s+run\s+)?pytest\b
    | \bpython\s+(?:-m\b|-c\b|scripts/|\S*\.py\b)
    | \bnpx\s+vitest\b | \bnpm\s+run\b | \bnpx\s+tsc\b | \btsc\s+--noEmit\b
    | \bpnpm\s+(?:--filter\s+\S+\s+)?(?:test|exec|run|vitest)\b
    | \bdbt\s+(?:build|run|test|seed|compile|deps)\b
    | \bpsql\b | \bbq\s+query\b | \bgcloud\s+\w+ | \bcurl\s+-\w
    | \bgrep\s+-\w | \brg\s+-\w
    | \bcd\s+(?:server|ui/admin|dbt)\b
    | \bnode\s+\S+\.mjs\b
    | \bmake\s+[a-z-]{3,}\b
    """
)


@dataclass
class Verdict:
    """One recorded entry of the completeness ledger."""

    surface: str
    key: str
    date: str
    form: str
    addresses_a_criterion: bool
    excerpt: str

    @property
    def identity(self) -> str:
        return f"{self.surface}[{self.key}]"


def classify(evidence: str) -> str:
    """Which of the three forms *evidence* is written in."""
    if _DEPLOYED_ADDRESS.search(evidence) and _RESULT_READ_BACK.search(evidence):
        return TRAVERSAL
    if _COMMAND.search(evidence):
        return COMMAND
    return PROSE


def _criteria_count(surface_key: str) -> int | None:
    """How many ratified criteria that surface has, or None when it has no entry.

    Read through `finished_work_audit`, which owns the parser. A second walk of
    the documents here is the defect `test_criteria_parser.py::
    test_no_module_anywhere_re_implements_the_walk` refuses by name.
    """
    audit = _audit_module()
    for surface in audit.SURFACES:
        if surface.key == surface_key:
            return len(audit.incomplete_if(surface.doc))
    return None


_AUDIT = None


def _audit_module():
    global _AUDIT
    if _AUDIT is None:
        try:
            import finished_work_audit as audit  # noqa: PLC0415
        except ModuleNotFoundError:  # pragma: no cover - packaging fallback
            from scripts import (  # type: ignore[no-redef]  # noqa: I001, PLC0415
                finished_work_audit as audit,
            )
        _AUDIT = audit
    return _AUDIT


def census(ledger_path: Path = LEDGER) -> list[Verdict]:
    """Every recorded verdict, classified by the form of its evidence."""
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    out: list[Verdict] = []
    for surface, entries in data.items():
        if surface.startswith("_") or not isinstance(entries, dict):
            continue
        total = _criteria_count(surface)
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            evidence = entry.get("evidence", "") or ""
            addressed = key.isdigit() and (total is None or int(key) < total)
            out.append(
                Verdict(
                    surface=surface,
                    key=key,
                    date=entry.get("date", "?"),
                    form=classify(evidence),
                    addresses_a_criterion=addressed,
                    excerpt=re.sub(r"\s+", " ", evidence).strip()[:120],
                )
            )
    return out


# --------------------------------------------------------------------------- #
# The surfaces that read DONE
# --------------------------------------------------------------------------- #


def done_surfaces() -> list[str]:
    """The surfaces `finished_work_audit` prints as DONE.

    Computed by that script, never by a second copy of its rule: DONE is not
    "no open criterion", it is no open criterion AND no structural finding AND
    no unmounted component, and the three can diverge.
    """
    audit = _audit_module()
    ledger = json.loads(audit.LEDGER.read_text(encoding="utf-8")) if audit.LEDGER.exists() else {}
    orphans = audit.orphan_components()
    done = []
    for surface in audit.SURFACES:
        if audit.evaluate(surface, ledger, orphans).verdict == "DONE":
            done.append(surface.key)
    return done


# --------------------------------------------------------------------------- #
# The ratchet
# --------------------------------------------------------------------------- #

#: The PROSE verdicts measured on 2026-08-31, the day this instrument was
#: written. The set turns ONE WAY: a verdict that gains a command must be
#: deleted from it, and a NEW prose verdict is refused where it is written.
#:
#: These are not 165 false verdicts. They are 165 closed criteria whose evidence
#: cannot be re-run, so nothing can tell whether they are still closed. AI-334
#: records the rewriting as the work of the sessions that follow; this list is
#: what stops the number climbing while that happens.
#:
#:     165  2026-08-31  mesure d'origine (AI-334)
#:     108  2026-09-01  LOT 1 -- governance (29) et file-source-ingestion (28)
#:                      reecrites avec la commande qui les prouve, rejouee ce jour ;
#:                      5 des 28 ROUVERTES faute de preuve rejouable.
#:      41  2026-09-01  LOT 2 -- proactive-assertions (11), currency-fx (10),
#:                      project-settings (10), overview (9), analyze-and-test (9),
#:                      data (9) et visualization-and-rendering (9) a ZERO prose ;
#:                      chaque commande rejouee ce jour, 0 reouverture.
#:                      SEPT de ces rangs portaient DEJA une commande et se lisaient
#:                      prose : `_COMMAND` ne connait pas `pnpm`, que le depot utilise.
#:       0  2026-09-01  LOT 3, le dernier -- les 41 restantes sur quinze surfaces
#:                      (caveats-register 3, competitors 1, context-hub 6, country 1,
#:                      data-path 1, datastream 3, execution-substrate 4,
#:                      first-figure-path 2, glossary 3, mcp-tool-surface 4,
#:                      module-boundaries 1, organization-settings 4, page-structure 1,
#:                      unresolved-values 3, user-bridge 4). UNE ROUVERTE :
#:                      caveats-register[0] -- `proactive-assertions.md` est passe a
#:                      `status: draft` le 2026-09-01 (0a2c354e, arbitrage Jean qui
#:                      NOMME ce critere), donc trois surfaces a claim causal n'ont
#:                      plus de document ratifie. Les huit verdicts deja OUVERTS du
#:                      lot portent desormais la commande qui MESURE le manque, pas
#:                      une phrase qui le decrit.
#:
#: LA BASELINE EST VIDE, et c'est la seule forme dans laquelle ce cliquet a un
#: sens durable : il ne tolere plus aucun verdict-prose, ni ancien ni neuf.
#: Ajouter une ligne ici est desormais un aveu -- ecrire la commande coute moins.
PROSE_VERDICTS_AT_2026_08_31: frozenset[str] = frozenset()


def _gate(verdicts: list[Verdict]) -> int:
    prose = {v.identity for v in verdicts if v.form == PROSE}
    baseline = PROSE_VERDICTS_AT_2026_08_31
    new = sorted(prose - baseline)
    gone = sorted(baseline - prose)

    counts = Counter(v.form for v in verdicts)
    print(f"recorded verdicts: {len(verdicts)}")
    print(f"  citing a traversal of the deployed product: {counts[TRAVERSAL]}")
    print(f"  citing a command                          : {counts[COMMAND]}")
    print(f"  prose, no command                         : {len(prose)}"
          f"   (baselined on 2026-08-31: {len(baseline)})")

    if new:
        print(f"\nREFUSED: {len(new)} NEW verdict(s) closed on prose alone.")
        print("The ledger's own rule: evidence is the command, route or screen that")
        print("produced the verdict. Write the command that can be re-run.")
        for identity in new:
            print(f"  {identity}")
    if gone:
        print(f"\nREFUSED: {len(gone)} baselined prose verdict(s) are no longer prose.")
        print("The ratchet turns one way: delete these from")
        print("PROSE_VERDICTS_AT_2026_08_31 in scripts/ledger_verdict_census.py.")
        for identity in gone:
            print(f"  {identity}")
    if new or gone:
        return 1
    print("\nOK: no new prose verdict, no stale line in the baseline.")
    return 0


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def _report(verdicts: list[Verdict], with_done: bool) -> None:
    per: dict[str, Counter] = {}
    for verdict in verdicts:
        per.setdefault(verdict.surface, Counter())[verdict.form] += 1

    done = set(done_surfaces()) if with_done else set()
    width = max(len(s) for s in per) if per else 1

    print()
    print("  Recorded verdicts, by the form their evidence is written in.")
    print()
    print(f"  {'surface'.ljust(width)}  {'':6s} trav  cmd  prose")
    for surface in sorted(per):
        counts = per[surface]
        mark = "DONE  " if surface in done else "      "
        print(
            f"  {surface.ljust(width)}  {mark}"
            f"{counts[TRAVERSAL]:4d} {counts[COMMAND]:4d} {counts[PROSE]:6d}"
        )

    totals = Counter(v.form for v in verdicts)
    print()
    print(f"  {len(verdicts)} recorded verdicts:")
    print(f"    {totals[TRAVERSAL]:4d} cite an address of the DEPLOYED product and a result")
    print(f"    {totals[COMMAND]:4d} cite a command")
    print(f"    {totals[PROSE]:4d} are prose with no command  <-- the ratchet holds this one")

    if with_done:
        in_done = [v for v in verdicts if v.surface in done]
        traversed = [v for v in in_done if v.form == TRAVERSAL]
        closing = [v for v in traversed if v.addresses_a_criterion]
        print()
        print(
            f"  {len(done)} surfaces read DONE, on {len(in_done)} verdicts, of which "
            f"{len(traversed)} cite a traversal"
        )
        print(
            f"  of the deployed product — and {len(closing)} of those close a criterion."
        )
        print("  A surface reads DONE on its criteria and its structure; nothing in that")
        print("  verdict is state 7 of element-control-loop.md.")

    unaddressed = [v for v in verdicts if not v.addresses_a_criterion]
    if unaddressed:
        print(f"\n  Recorded against a key that addresses no criterion ({len(unaddressed)}):")
        for verdict in unaddressed:
            print(f"    {verdict.identity}  {verdict.date}")
        print("  `finished_work_audit.py` reads `str(index)` only, so these close nothing.")
    print()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable")
    parser.add_argument("--prose-only", action="store_true", help="only the prose verdicts")
    parser.add_argument("--traversals", action="store_true", help="only the traversal verdicts")
    parser.add_argument("--gate", action="store_true", help="refuse a NEW prose verdict")
    parser.add_argument("--baseline", action="store_true", help="print today's prose set")
    parser.add_argument(
        "--no-done", action="store_true", help="skip the DONE cross-tab (skips the orphan sweep)"
    )
    args = parser.parse_args(argv)

    verdicts = census()
    if args.gate:
        return _gate(verdicts)
    if args.baseline:
        for identity in sorted(v.identity for v in verdicts if v.form == PROSE):
            print(f'        "{identity}",')
        return 0
    if args.json:
        print(json.dumps([asdict(v) for v in verdicts], indent=2, ensure_ascii=False))
        return 0
    if args.prose_only or args.traversals:
        wanted = PROSE if args.prose_only else TRAVERSAL
        for verdict in verdicts:
            if verdict.form == wanted:
                print(f"  {verdict.identity:38s} {verdict.date}  {verdict.excerpt}")
        return 0
    _report(verdicts, with_done=not args.no_done)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
