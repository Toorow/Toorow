/**
 * Ce que le front SERT est en anglais — la moitié front de AD-34.
 *
 * LE CLIQUET ANGLAIS EXISTAIT ET NE VOYAIT QUE `server/core`.
 * `server/tests/conformance/test_operator_messages_are_english.py` balaie les
 * phrases d'opérateur du serveur depuis le 2026-08-08, avec son registre de
 * dette par fichier. Le front, lui, n'avait **aucun** instrument : la copie
 * française y vivait sans que rien ne rougisse — 67-14 l'a nommée « classe sans
 * instrument », et une classe sans instrument revient.
 *
 * Quatre phrases servies étaient en français au 2026-08-25 :
 * `Sparkline.tsx` (`Tendance sur la période`), `FeedbackBar.tsx` (deux : l'erreur
 * d'envoi et la question posée au lecteur) et `KpiTile.tsx` (`Définition de la
 * métrique`). Traduites dans le commit qui pose ce fichier ; le cliquet est à
 * **zéro**, donc la cinquième rougit.
 *
 * DEUX PIÈGES QUE LA GARDE SERVEUR NE POUVAIT PAS RENCONTRER, et qui sont la
 * raison pour laquelle ce fichier n'est pas une transcription de l'autre :
 *
 *   1. **`Plus Jakarta Sans`.** `plus` et `sans` sont deux marqueurs français,
 *      et la pile de polices du produit les met côte à côte — sept fois dans le
 *      seul `FeedbackBar.tsx`. Une transcription naïve aurait accusé la
 *      typographie. Python n'a pas de piles de polices ; le front, si.
 *   2. **Le texte JSX.** Une phrase entre deux balises n'est pas un littéral de
 *      chaîne. `FeedbackBar.tsx:356` en était une, et c'est exactement le cas
 *      que la relecture du 24 signalait comme invisible à l'extracteur.
 *
 * Les COMMENTAIRES sont hors sujet : ce dépôt les écrit en français
 * délibérément. Seul ce qui est servi compte.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";

const REPO = resolve(__dirname, "..", "..", "..", "..");

/** Les arbres front. `web` est inclus s'il existe : la vitrine sert aussi. */
const TREES = ["ui", "web"];

/**
 * Des mots-outils français qui ne sont PAS des mots anglais — la liste de la
 * garde serveur, moins `plus` et `sans`.
 *
 * CES DEUX-LÀ SONT RETIRÉS ET C'EST UNE MESURE, pas une commodité :
 * `'Plus Jakarta Sans', sans-serif` est la police du produit, et elle porte les
 * deux. Les garder aurait demandé une exception par site de style ; les retirer
 * coûte quoi ? Une phrase française qui ne contiendrait que `plus` et `sans` et
 * aucun des soixante autres marqueurs. Mesuré sur les quatre cas réels : aucun
 * n'en dépend.
 */
const FRENCH_MARKERS = new Set([
  "de", "du", "des", "le", "la", "les", "un", "une", "aux", "ce", "ces",
  "cette", "ne", "se", "qui", "que", "quoi", "ses", "leur", "nos", "vos",
  "dont", "car", "afin", "lors", "selon", "entre", "chaque", "aucun",
  "aucune", "avant", "apres", "après", "avec", "dans", "depuis", "doit",
  "elle", "est", "etait", "était", "etaient", "étaient", "etre", "être",
  "faire", "impossible", "jamais", "mais", "meme", "même", "pour",
  "pourquoi", "quand", "sont", "toujours", "tous", "toutes", "vous",
  "ainsi", "alors", "donc", "puis", "aussi", "encore", "par", "sur",
]);

/** DEUX marqueurs, pas un. Un seul est la façon dont naît un faux positif. */
const MIN_MARKERS = 2;

/** Les lettres de n'importe quelle langue, sans chiffres ni blancs. */
const WORD = /[\p{L}]+/gu;

/** Ce qui est du STYLE et non de la copie : une pile de polices, une couleur. */
const CSSISH = /var\(--|-serif|monospace|\d+px|rgba?\(/;

function frenchMarkers(text: string): string[] {
  const found = new Set<string>();
  for (const m of text.matchAll(WORD)) {
    const w = m[0].toLowerCase();
    if (FRENCH_MARKERS.has(w)) found.add(w);
  }
  return [...found].sort();
}

function frontFiles(dir: string, out: string[] = []): string[] {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return out;
  }
  for (const entry of entries) {
    const full = resolve(dir, entry);
    if (statSync(full).isDirectory()) {
      if (["node_modules", "dist", "coverage", "__tests__"].includes(entry)) continue;
      frontFiles(full, out);
      continue;
    }
    if (!/\.(ts|tsx)$/.test(entry)) continue;
    if (/\.(test|spec)\.(ts|tsx)$/.test(entry)) continue;
    // Une FIXTURE est de la donnée d'essai, pas de la copie servie. Le nom est
    // la seule marque disponible, et elle est constante dans ce dépôt.
    if (/fixture|sandbox/i.test(entry)) continue;
    out.push(full);
  }
  return out;
}

/** Les littéraux de chaîne ET le texte JSX — la seconde moitié est le piège.
 *
 * LE TROU DU 2026-08-25, MESURÉ ET BOUCHÉ LE 2026-09-06 (story 76-4). Le motif
 * JSX excluait le saut de ligne de sa classe de caractères, donc une phrase
 * écrite sur DEUX LIGNES — la mise en forme normale d une phrase longue dans du
 * JSX — était invisible pour cette garde. Vingt phrases françaises vivaient dans
 * cet angle mort, dont SEIZE d états vides et de surfaces d erreur : exactement
 * ce que console-presentation.md §5 gouverne, et exactement ce que le cliquet
 * prétendait tenir à zéro. Une garde qui ne lit pas la forme la plus courante de
 * ce qu elle garde n est pas à zéro : elle est aveugle.
 *
 * La classe [^<>{}] fait déjà tout le travail de délimitation — aucune balise,
 * aucune interpolation — donc retirer le saut de ligne ne fait entrer aucun
 * code, seulement du texte. Les blancs sont NORMALISÉS avant comparaison : sans
 * cela une phrase coupée par un retour à la ligne et son indentation ne se lit
 * dans aucun rapport.
 *
 * Ce qui n a pas bougé : les commentaires (français par choix), l exclusion de
 * la pile de polices par CSSISH, et le seuil de DEUX marqueurs. */
function servedStrings(source: string): string[] {
  const stripped = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  const found: string[] = [];
  for (const rx of [
    /"([^"\n]{8,}?)"/g,
    /'([^'\n]{8,}?)'/g,
    /`([^`]{8,}?)`/g,
    />([^<>{}]{12,}?)</g,
  ]) {
    for (const m of stripped.matchAll(rx)) found.push(m[1].replace(/\s+/g, " ").trim());
  }
  return found;
}

function offenders(): string[] {
  const out: string[] = [];
  for (const tree of TREES) {
    for (const file of frontFiles(resolve(REPO, tree))) {
      const source = readFileSync(file, "utf-8");
      for (const value of servedStrings(source)) {
        if (CSSISH.test(value)) continue;
        const markers = frenchMarkers(value);
        if (markers.length >= MIN_MARKERS) {
          out.push(`${file.slice(REPO.length + 1)}: [${markers.join(",")}] ${value.slice(0, 70)}`);
        }
      }
    }
  }
  return out.sort();
}

describe("le front sert de l'anglais", () => {
  it("ne sert aucune phrase française", () => {
    /**
     * LE CLIQUET EST À ZÉRO. Il n'y a pas de registre de dette ici, et c'est un
     * choix : la garde serveur en porte un parce qu'elle est arrivée sur
     * soixante phrases. Le front en avait quatre, elles sont traduites, donc la
     * cinquième doit rougir sur le run qui l'introduit.
     */
    expect(offenders()).toEqual([]);
  });

  it("lit un vrai corpus, et pas zéro fichier", () => {
    /**
     * LA GARDE SUR LA GARDE. Un `TREES` cassé, un `readdirSync` qui lève, un
     * suffixe mal écrit — et le balayage rend zéro fichier, zéro coupable, vert
     * pour toujours. C'est le défaut que ce dépôt a payé quatre fois cette
     * semaine : un instrument qui mesure sa propre indulgence.
     */
    const scanned = TREES.flatMap((t) => frontFiles(resolve(REPO, t)));
    expect(scanned.length).toBeGreaterThan(300);
  });

  it("accuse une phrase française qu'on lui montre", () => {
    /**
     * Et elle doit pouvoir ACCUSER. Les trois formes qui comptent : un littéral
     * simple, un gabarit, et du TEXTE JSX — la troisième est celle que la
     * relecture du 24 signalait comme invisible.
     */
    expect(frenchMarkers("Aucune ligne dans ce plan")).toHaveLength(3);
    expect(servedStrings('<span>Ce rapport vous a-t-il ete utile ?</span>')).toContain(
      "Ce rapport vous a-t-il ete utile ?",
    );
    expect(servedStrings("const a = `Erreur lors de l envoi`;")).toContain(
      "Erreur lors de l envoi",
    );
  });

  it("lit une phrase JSX ECRITE SUR DEUX LIGNES, qui est l angle mort de 2026-08-25", () => {
    /**
     * LA QUATRIEME FORME, et celle qui a coute le plus cher. Le motif excluait
     * le saut de ligne, donc une phrase mise en forme sur deux lignes -- la
     * facon NORMALE d ecrire une phrase longue en JSX -- ne se lisait pas.
     * Vingt phrases francaises y vivaient, dont seize d etats vides et de
     * surfaces d erreur. Prouve ici sur les deux motifs cote a cote, pour que
     * la difference soit dans le fichier et pas seulement dans un commit.
     */
    const twoLine = ["<div>", "      Aucune donnee pour la periode demandee.", "    </div>"].join("\n");
    expect(servedStrings(twoLine)).toContain("Aucune donnee pour la periode demandee.");
    expect(frenchMarkers("Aucune donnee pour la periode demandee.").length)
      .toBeGreaterThanOrEqual(MIN_MARKERS);
    // L ancien motif, celui qui etait vert sur cette meme phrase.
    expect([...twoLine.matchAll(new RegExp(">([^<>{}\\n]{12,}?)<", "g"))]).toHaveLength(0);
  });

  it("normalise les blancs, sinon une phrase coupee ne s apparie avec rien", () => {
    // Sans la normalisation la phrase remonterait avec son retour a la ligne et
    // son indentation au milieu, illisible dans le rapport et impossible a
    // comparer a quoi que ce soit.
    expect(servedStrings(["<p>", "   Une phrase", "   coupee en deux.", "</p>"].join("\n")))
      .toContain("Une phrase coupee en deux.");
  });

  it("n'accuse PAS la pile de polices du produit", () => {
    /**
     * `plus` et `sans` sont deux marqueurs français, et `'Plus Jakarta Sans',
     * sans-serif` les met côte à côte. Sept fois dans le seul `FeedbackBar`.
     * Une garde qui accuse la typographie se fait désarmer dans la semaine.
     */
    const font = "var(--font-primary, 'Plus Jakarta Sans', sans-serif)";
    expect(CSSISH.test(font)).toBe(true);
  });

  it("ne lit pas les commentaires, qui sont français par choix", () => {
    const source = "// Ce commentaire est en francais et ne regarde personne\nconst a = 1;";
    expect(servedStrings(source)).toEqual([]);
  });
});
