r"""Refuse a completeness verdict that no longer points at the criterion it judged.

WHY THIS EXISTS. `completeness-ledger.json` keys a verdict by the ZERO-BASED
INDEX of a bullet in a document's `Incomplete if` list -- its own `_README.shape`
says so. An index into a mutable list is a pointer with no anchor: INSERT a bullet
and every key below it silently re-points at its neighbour, and the verdicts keep
looking recorded.

It is not hypothetical, and it is not rare enough to leave alone. Measured
2026-08-08 on `analyze-and-test`: a bullet was inserted on 2026-08-04, while the
key `10` had been written on 2026-07-29. The evidence recorded under `10` --
measured, commanded, real -- was attesting to criterion 11. Two criteria looked
judged and were not; one verdict vouched for a criterion nobody had measured.

And the same day, repairing a NEIGHBOURING item, I re-committed it: rewriting a
bullet in place under a key that already carried a `false` verdict. That is why
this guard exists rather than a rule in a header. A rule that is broken by the
person who wrote it, an hour later, is not a rule.

WHAT IT ASKS OF A NEW VERDICT: one field, `criterion`, carrying the opening of
the bullet it judges. Then a moved bullet is a red test instead of a silent
re-pointing.

    python scripts/check_ledger_anchors.py          # the state, exit 0
    python scripts/check_ledger_anchors.py --gate   # refuse drift and growth

THREE CHECKS, AND WHY THE THIRD IS A RATCHET RATHER THAN A REFUSAL:

  1. an anchored verdict whose bullet has MOVED or CHANGED -- refused outright;
  2. a key POINTING PAST the end of its list -- refused outright, since a
     criterion that does not exist cannot have been judged;
  3. a verdict with NO anchor -- counted. 122 of them predate this guard, and
     stamping today's text into them would assert that today's mapping is
     correct, which is exactly what `analyze-and-test` proved could be false.
     Anchoring one is a READING, so it is done by whoever re-measures the
     criterion, and the count may only come down.

Six surfaces of the ledger have no `<surface>.md` at all (`competitors`,
`country`, `currency-fx`, `datastream`, `reporting-timezone`, `tax-fees`): their
criteria live inside other pages, so nothing here can re-derive what their keys
point at. They are reported as UNRESOLVABLE rather than counted as green -- an
angle this instrument does not see must not read as an angle it checked.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from finished_work_audit import SURFACES, incomplete_if  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
DOCS = REPO / "docs" / "product-architecture"
LEDGER = DOCS / "completeness-ledger.json"

#: The surface registry is held ONCE, in `finished_work_audit.SURFACES`.
_DOC_BY_SURFACE = {surface.key: surface.doc for surface in SURFACES}

#: How much of the bullet a `criterion` anchor must carry. Long enough that two
#: neighbouring bullets cannot share it, short enough that reflowing a paragraph
#: does not break it.
ANCHOR_CHARS = 60

#: Verdicts recorded before this guard existed, carrying no anchor. SHRINK-ONLY.
#:
#:     122  2026-08-09  mesure d'origine
#:     121  2026-08-09  `analyze-and-test[11]` ancre -- le SEUL dont la
#:                      correspondance ait ete verifiee a la main, en relisant les
#:                      douze puces le jour ou sa cle a ete deplacee de 10 vers 11.
#:                      Les 121 autres attendent leur relecture, pas un script.
#:
#: LE CHIFFRE NE BOUGE PAS LE 2026-08-21, ET C'EST LE POINT. Six commits des 16
#: et 17 aout ont enregistre 16 verdicts SANS ancre, contre la seule chose que ce
#: fichier demande a un verdict neuf : le compte est monte a 135. Douze d'entre
#: eux ont ete relus -- `--unanchored-drift` prouve que leur puce n'a pas bouge
#: entre le commit qui les a ecrits et aujourd'hui -- et portent desormais leur
#: `criterion`. Les autres attendent une relecture, une par verdict.
#:
#: COMBIEN SONT DEJA RE-POINTES : LA COMMANDE LE DIT, CE COMMENTAIRE NON.
#: Ce paragraphe a porte << les QUATRE derniers sont prouves RE-POINTES >> le
#: jour ou `--unanchored-drift` en imprimait vingt-deux. C'est le meme defaut que
#: le compte perime retire de `finished_work_audit.py` le 2026-08-21, soigne dans
#: un fichier et laisse dans le voisin : un nombre retape est faux des que le
#: document bouge, et il bouge chaque semaine.
#:
#:     python scripts/check_ledger_anchors.py --unanchored-drift
#:
#: rend les deux categories separement -- RE-POINTES (la cle nomme aujourd'hui
#: un autre critere) et REECRITS SUR PLACE (la cle nomme toujours le meme
#: critere, dont les mots ont change). Seuls les premiers sont une fausse
#: attestation ; les seconds demandent une relecture de la preuve, pas de la
#: correspondance. Le gate ne lit ni l'un ni l'autre : il ne lit que le cliquet
#: ci-dessous.
# 2026-08-22, story 67.22 : 121 -> 119. Le cliquet etait DEPASSE (123), et le
#: rejeu du compte sur les 40 dernieres revisions du ledger a nomme la cause
#: exactement : quatre verdicts ecrits APRES `1ab0f3ca` sans le seul champ que
#: cette garde demande -- `analyze-and-test[10]` (`8d255505`) et
#: `context-hub[17..19]` (`48d1d836`), tous du 2026-08-16. Trois d'entre eux
#: avaient DEJA glisse de +1 et attestaient depuis six jours un critere que
#: personne n'a juge. Deplaces vers leur puce, puis ancres ; le quatrieme etait
#: reste sur la sienne, verifie en relisant `incomplete_if` a son commit. Aucun
#: `evidence` n'a ete touche : ce n'est pas une re-mesure, c'est une reunion.
# 2026-08-30, story 49-6 lot 1 : 119 -> 102. DIX-SEPT ancres posees d'un coup, et
#: toutes sur `context-hub`, qui n'en portait aucune de [0] a [16]. Ce n'est pas
#: un tampon : la liste de ce document n'a bouge qu'UNE FOIS depuis que ces
#: verdicts ont ete ecrits -- l'insertion du 2026-08-17 a l'index 15 -- donc les
#: cles [0..14] nomment la puce qu'elles ont jugee, verifie evidence par evidence
#: contre le texte de la puce avant l'ecriture de l'ancre. Les deux qui avaient
#: glisse ne sont PAS ancrees sur place : [15] portait la preuve `mdm_references`
#: (elle juge la puce d'aujourd'hui [16]) et [16] portait `SkillEditorDrawer`
#: (elle juge [17]) ; chacune est allee a sa puce, et [15] a recu le verdict de
#: la puce inseree. [16] reste OUVERT avec sa mesure ecrite : la fermer serait
#: fermer le critere d'un autre lot.
# 2026-08-30, story 67-22 : 102 -> 1. Le reste du registre est ancre, et la
#: repartition est celle que `--unanchored-drift` imprimait au depart : 86 verdicts
#: encore sur leur puce, ancres avec les mots de cette puce ; 12 re-pointes,
#: deplaces vers la cle qui porte AUJOURD'HUI la puce qu'ils ont jugee
#: (`datastream[7..13]` -> [8],[9],[10],[12],[13],[15],[16] ; `overview[5..8]` ->
#: [6..9]) ; 1 reecrit sur place (`project-settings[2]`, dont la preuve prouve
#: encore la clause elargie a `Placement Mapping`, re-lue et re-jouee).
#: DEUX DES QUATORZE N'ONT PAS BOUGE, et c'est une limite de l'instrument, pas du
#: registre : `data[7]` et `data[8]` ont ete ecrits a 23:00:31 le 2026-08-04
#: (`7b7ce4f5`) et les deux puces qu'ils jugent sont arrivees dans `data.md` a
#: 23:02:32 le meme soir (`c09866b4`). `unanchored_drift` lit le document au commit
#: qui a ecrit la cle, donc il lisait la liste d'AVANT l'amendement et les declarait
#: re-pointes. Leur preuve nomme la section `Two publications, two pointers` et
#: `PublicationReviewModal` : elle juge les puces [7] et [8] d'aujourd'hui. Ancrees
#: sur place.
#: `overview[9]` (2026-07-29) jugeait la puce qui est aujourd'hui [10], ou un
#: verdict ANCRE du 2026-08-25 juge deja le meme critere avec ses commandes ; il est
#: reuni dans celui-la plutot que re-cle.
#: LE DERNIER SANS ANCRE EST `data[10]`, et il n'est pas a moi de le fermer : ecrit
#: dans le meme commit de 23:00:31, il portait la cle 10 d'une liste qui allait en
#: compter onze. Sa preuve (AI-180, `DataAccessGrantsPanel`) juge mot pour mot la
#: puce qui est aujourd'hui `data[19]` -- << a person cannot see, add or revoke the
#: external principals ... >> -- tandis que la cle 10 nomme depuis un critere sur
#: les adresses du routeur que personne n'a juge. Le deplacement revient au
#: proprietaire de `data.md`.
RECORDED_UNANCHORED = 0

#: Ce qu'il faut d'ouverture commune pour dire << la meme puce, reecrite >>
#: plutot que << une autre puce a la meme place >>.
#:
#: LE SEUIL SEUL NE DECIDE RIEN, et c'est voulu : l'ouverture partagee doit AUSSI
#: ne designer qu'UNE puce de la liste du jour. Mesure du 2026-08-21 sur les 22
#: verdicts derives : le seul cas de reecriture sur place partage 19 caracteres
#: qui n'ouvrent qu'une puce (`project-settings[2]`, << Country, Tax & Fees >>),
#: tandis que les coincidences entre puces etrangeres partagent 0, 1, 2 ou 9
#: caracteres et en ouvrent 2, 20, 21 ou 28. Les deux bornes sont epinglees dans
#: `server/tests/core/test_criteria_parser.py`.
REWRITE_MIN_OPENING = 12


def bullets(surface: str) -> list[str] | None:
    """The `Incomplete if` bullets of *surface*, or None when it has no document.

    TWO PARSERS OF ONE LIST IS TWO ANSWERS TO "WHAT DOES INDEX 13 NAME?".
    This file used to re-read the document itself: first `## incomplete if`
    heading, dashed bullets only, stop at the next `## `. `finished_work_audit`
    read it differently, and on 2026-08-17 both were repaired to read every
    block an amendment adds — a divergence here would have made a legitimate
    verdict look out of range, or hidden one that truly was. The surface-to-
    document map comes from the same table too, so the six surfaces whose page
    is not `<key>.md` (`capabilities/`, the Datastream workbench) resolve
    instead of being reported as unverifiable.
    """
    doc = _DOC_BY_SURFACE.get(surface, DOCS / f"{surface}.md")
    if not doc.is_file():
        return None
    listing = incomplete_if(doc)
    return listing or None


def audit() -> tuple[list[str], list[str], int, list[str]]:
    """``(drifted, out_of_range, unanchored_count, unresolvable_surfaces)``."""
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    drifted: list[str] = []
    out_of_range: list[str] = []
    unanchored = 0
    unresolvable: list[str] = []

    for surface, entries in sorted(ledger.items()):
        if surface.startswith("_") or not isinstance(entries, dict):
            continue
        listing = bullets(surface)
        if listing is None:
            unresolvable.append(surface)
        for key, entry in sorted(entries.items(), key=lambda kv: kv[0]):
            if not key.isdigit() or not isinstance(entry, dict):
                continue
            anchor = entry.get("criterion")
            if anchor is None:
                unanchored += 1
            if listing is None:
                continue
            index = int(key)
            if index >= len(listing):
                out_of_range.append(
                    f"{surface}[{key}] points past the list "
                    f"({len(listing)} bullets) -- a criterion that does not exist "
                    "cannot have been judged"
                )
                continue
            if anchor and not listing[index].startswith(anchor[:ANCHOR_CHARS]):
                drifted.append(
                    f"{surface}[{key}] anchor no longer matches its bullet.\n"
                    f"      recorded: {anchor[:ANCHOR_CHARS]}\n"
                    f"      bullet  : {listing[index][:ANCHOR_CHARS]}"
                )
    return drifted, out_of_range, unanchored, unresolvable


#: The ledger's own history, which is what makes an UNANCHORED verdict checkable
#: after all. The verdict carries no anchor, but the repository knows the commit
#: that first wrote its key, and therefore what the document said at that index
#: on that day. If that text is still at that index, the verdict has not moved
#: and whoever re-reads it can anchor it in one step. If it is NOT, the verdict
#: is attesting a criterion nobody judged -- silently, exactly as
#: `analyze-and-test[10]` did for nine days before anyone opened the two lists
#: side by side.
#:
#: This is measurement, not anchoring. It does not write a `criterion` into
#: anything: stamping today's text into a verdict whose mapping is unknown is
#: the one move this file exists to prevent. It produces the work list, worst
#: first, so the re-reading the header asks for stops being 121 opaque rows.
#:
#: It shells out to git and is therefore kept OFF the gate path: a gate must
#: not depend on the history of the checkout it runs in.
_LEDGER_PATH = "docs/product-architecture/completeness-ledger.json"


def _git(*args: str) -> str | None:
    import subprocess  # noqa: PLC0415

    done = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return done.stdout if done.returncode == 0 else None


def _bullets_at(rev: str, doc: pathlib.Path) -> list[str] | None:
    """The criteria list of *doc* as it stood at *rev*, read by TODAY's parser.

    Today's parser on yesterday's text is the deliberate choice: the question is
    "does index k name the same bullet then and now", and two different parsers
    would answer a different question.
    """
    import tempfile  # noqa: PLC0415

    text = _git("show", f"{rev}:{doc.relative_to(REPO).as_posix()}")
    if text is None:
        return None
    scratch = pathlib.Path(tempfile.mkdtemp()) / doc.name
    scratch.write_text(text, encoding="utf-8")
    return incomplete_if(scratch)


def _common_opening(left: str, right: str) -> str:
    """The characters the two bullets open on, before the first difference."""
    size = 0
    for a, b in zip(left, right):
        if a != b:
            break
        size += 1
    return left[:size]


def _same_criterion(judged: str, candidate: str, listing: list[str]) -> bool:
    """True when *candidate* is *judged* REWRITTEN, and not another criterion.

    The rule is not a similarity score. Two bullets are the same criterion when
    they open on the same words AND those words open nothing else in the list --
    the same argument `ANCHOR_CHARS` is built on, applied to a prefix the
    document itself produced instead of a fixed width. A bullet REPLACED in place
    by a different criterion (`mcp-tool-surface[4]`, 2026-08-18) opens on other
    words, so it stays what it is: a judged criterion that exists at no index.
    """
    opening = _common_opening(judged, candidate)
    if len(opening) < REWRITE_MIN_OPENING:
        return False
    return sum(1 for bullet in listing if bullet.startswith(opening)) == 1


def unanchored_drift() -> tuple[list[str], list[str], list[str], list[str]]:
    """``(moved, rewritten, stable, unreadable)`` for every verdict with no anchor.

    THE COUNT USED TO BE A CEILING, AND IT SAID SO NOWHERE. The comparison was
    `then[index] == now[index]`, so anything else was reported as a re-pointing.
    That merges two situations a reader has to tell apart:

      * RE-POINTED -- the judged bullet is at another index, or at none. The key
        now names a criterion nobody judged. That is a false attestation.
      * REWRITTEN IN PLACE -- the key still names the criterion it judged; the
        document reworded it. The evidence may have aged, the CORRESPONDENCE has
        not moved.

    Measured 2026-08-21: 22 verdicts differ from the day they were written, and
    exactly one of them (`project-settings[2]`, "Country, Tax & Fees or
    Competitors" become "Country, Tax & Fees, Competitors or Placement Mapping")
    is a rewrite at its own index. Reporting it as re-pointed puts a re-reading
    on the work order that has nothing to re-point.
    """
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    wanted: set[tuple[str, int]] = set()
    for surface, entries in ledger.items():
        if surface.startswith("_") or not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            if key.isdigit() and isinstance(entry, dict) and entry.get("criterion") is None:
                wanted.add((surface, int(key)))

    history = _git("log", "--format=%H|%ad", "--date=short", "--", _LEDGER_PATH)
    if history is None:
        return [], [], [], ["git history unavailable; nothing could be compared"]

    # Oldest first, so the first commit that carries a key is the one that wrote it.
    born: dict[tuple[str, int], tuple[str, str]] = {}
    for line in reversed(history.strip().splitlines()):
        rev, date = line.split("|", 1)
        blob = _git("show", f"{rev}:{_LEDGER_PATH}")
        if blob is None:
            continue
        try:
            snapshot = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for surface, index in wanted - set(born):
            entry = snapshot.get(surface, {})
            if isinstance(entry, dict) and isinstance(entry.get(str(index)), dict):
                born[(surface, index)] = (rev, date)

    moved: list[str] = []
    rewritten: list[str] = []
    stable: list[str] = []
    unreadable: list[str] = []
    cache: dict[tuple[str, str], list[str] | None] = {}
    for surface, index in sorted(wanted):
        doc = _DOC_BY_SURFACE.get(surface, DOCS / f"{surface}.md")
        if (surface, index) not in born or not doc.is_file():
            unreadable.append(f"{surface}[{index}] -- no commit wrote this key, or no document")
            continue
        rev, date = born[(surface, index)]
        if (rev, surface) not in cache:
            cache[(rev, surface)] = _bullets_at(rev, doc)
        then = cache[(rev, surface)]
        now = incomplete_if(doc)
        if then is None or index >= len(then) or index >= len(now):
            unreadable.append(f"{surface}[{index}] recorded {date} -- the list was shorter then")
            continue
        if then[index] == now[index]:
            stable.append(f"{surface}[{index}] recorded {date}")
            continue
        if _same_criterion(then[index], now[index], now):
            rewritten.append(
                f"{surface}[{index}] recorded {date} judged the criterion that is STILL "
                f"at [{index}], reworded since.\n"
                f"      judged : {then[index][:ANCHOR_CHARS]}\n"
                f"      today  : {now[index][:ANCHOR_CHARS]}"
            )
            continue
        landed = next((i for i, b in enumerate(now) if b == then[index]), None)
        if landed is not None:
            where = f"now at [{landed}]"
        else:
            # No twin, byte for byte. The bullet may still have MOVED and been
            # reworded on the way; only an opening that names one bullet says so.
            reworded = next(
                (i for i, b in enumerate(now) if _same_criterion(then[index], b, now)), None
            )
            where = (
                f"rewritten and now at [{reworded}]" if reworded is not None
                else "gone from the list"
            )
        moved.append(
            f"{surface}[{index}] recorded {date} judged a bullet that is {where}.\n"
            f"      judged : {then[index][:ANCHOR_CHARS]}\n"
            f"      at [{index}] today: {now[index][:ANCHOR_CHARS]}"
        )
    return moved, rewritten, stable, unreadable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true")
    parser.add_argument(
        "--unanchored-drift",
        action="store_true",
        help="which UNANCHORED verdicts already point at another bullet (reads git; never gates)",
    )
    args = parser.parse_args(argv)

    if args.unanchored_drift:
        moved, rewritten, stable, unreadable = unanchored_drift()
        print(
            f"unanchored verdicts: {len(stable)} still on their bullet, "
            f"{len(moved)} ALREADY RE-POINTED, {len(rewritten)} rewritten in place, "
            f"{len(unreadable)} unverifiable"
        )
        print("\nRE-POINTED -- the key names a criterion nobody judged:")
        for line in moved:
            print(f"  {line}")
        print(
            "\nREWRITTEN IN PLACE -- the key still names the criterion it judged, in "
            "other words:"
        )
        for line in rewritten:
            print(f"  {line}")
        if unreadable:
            print("\nUNVERIFIABLE:")
            for line in unreadable:
                print(f"  {line}")
        print(
            "\nA re-pointed verdict attests a criterion nobody judged, and re-reading it "
            "is the repair.\nA rewritten one attests the right criterion in older words: "
            "its EVIDENCE is what needs\nre-reading, not its correspondence. Neither is "
            "anchored here; this list is the work order."
        )
        return 0

    drifted, out_of_range, unanchored, unresolvable = audit()
    print(
        f"ledger: {unanchored} verdicts sans ancre, {len(drifted)} ancres qui ont "
        f"derive, {len(out_of_range)} cles hors liste"
    )
    if unresolvable:
        print(
            "  surfaces sans document, donc NON VERIFIEES ici : "
            + ", ".join(unresolvable)
        )
    for line in drifted + out_of_range:
        print(f"  {line}")

    if not args.gate:
        return 0
    if drifted or out_of_range:
        print("\nREFUSE: un verdict ne pointe plus le critere qu'il a juge.", file=sys.stderr)
        return 1
    if unanchored > RECORDED_UNANCHORED:
        print(
            f"\nREFUSE: {unanchored} verdicts sans ancre, {RECORDED_UNANCHORED} "
            "enregistres. Un verdict neuf porte son `criterion`.",
            file=sys.stderr,
        )
        return 1
    if unanchored < RECORDED_UNANCHORED:
        print(
            f"\nREFUSE: {unanchored} < {RECORDED_UNANCHORED} -- une ancre a ete "
            "posee, baissez le chiffre dans ce fichier.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
