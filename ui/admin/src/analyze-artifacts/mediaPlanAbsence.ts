/**
 * Ce qu'il manque quand un Projet n'a pas de plan média — dit UNE fois, avec la
 * porte qui le remplit.
 *
 * Story 67.26, 2026-08-22. Deux écrans répondaient à « où importe-t-on un plan »
 * et ils répondaient **deux choses différentes**, dont aucune n'était vraie :
 *
 *   * `PacingReport.tsx` : « …so it is done through the **media plan API** ».
 *     Un terme de plomberie, pas un geste — et CLAUDE.md l'interdit en toutes
 *     lettres : « un message d'erreur nomme le geste qui répare, pas la cause
 *     technique ».
 *   * `WorkbenchPlacementsPage.tsx` : « **Governance** is where a media plan is
 *     imported and versioned », avec un bouton `Open media plans in Governance`.
 *     Mesuré le 2026-08-22 : `grep -rln "mediaplan" ui/admin/src/governance/`
 *     ne rend **aucun fichier**. L'écran vers lequel il envoie n'existe pas.
 *
 * Les deux fausses phrases sont parties le 2026-08-22, et ce module les a
 * remplacées par une absence honnête : le geste était nommé, sans adresse,
 * parce qu'**une décision manquait** — quel Datastream porte un plan.
 *
 * --------------------------------------------------------------------------
 * AMENDÉ LE 2026-08-24 — LA DÉCISION EST RENDUE, DONC L'ADRESSE EXISTE.
 *
 * Deux arbitrages ratifiés le même jour (commit `a379ec50`) :
 *
 *   * `file-source-ingestion.md` — « a project carries one or several media
 *     plans, each on its own carrier Datastream ». Un projet porte UN OU
 *     PLUSIEURS plans ; chacun vit sur SON Datastream porteur, créé avec le
 *     profil de gabarit propre à ce plan ; les révisions se chargent PAR DATE,
 *     à côté des précédentes et jamais par-dessus.
 *   * `analyze-and-test.md` — « the media plan is created and imported in the
 *     carrier Datastream's Workbench », et « l'état vide du Pacing gagne une
 *     porte ».
 *
 * Donc la phrase ne dit plus « ce n'est pas encore un geste de cette console » :
 * c'en est un, il a une adresse, et les deux écrans qui posent la question
 * l'offrent. Ce qu'elle continue de refuser, c'est d'inventer une destination
 * quand il n'y en a pas : sans Datastream porteur dans le Projet, la phrase
 * nomme le geste qui en crée un — créer une source fichier — et la porte va là.
 */

/** Le titre : ce qui manque, dans les mots de la personne. */
export const NO_MEDIA_PLAN_TITLE = "No media plan in this Project";

/**
 * Ce qu'un plan média EST, et où il se fabrique. Une seule phrase, partout.
 *
 * Elle nomme le **Workbench du Datastream porteur** parce que c'est l'adresse
 * ratifiée, et elle dit pourquoi c'est là : le plan est un fichier, et le cycle
 * du fichier est déjà celui de ce Datastream.
 */
export const NO_MEDIA_PLAN_DESCRIPTION =
  "A media plan is a spreadsheet read by the same file-source engine as every " +
  "other file: its lines become the planned budget this reading compares " +
  "against. A plan is created and each of its dated revisions imported in the " +
  "Workbench of the file-source Datastream that carries it — one Datastream per " +
  "plan, with the template that reads that plan's layout.";

/**
 * La phrase quand le Projet n'a **aucun** Datastream porteur.
 *
 * Elle ne nomme pas un état de déploiement et ne renvoie à aucun document :
 * elle nomme le geste — créer une source fichier avec un gabarit de plan média —
 * et l'appelant offre la porte vers la création de Datastream, qui existe.
 */
export const NO_CARRIER_DESCRIPTION =
  "No Datastream of this Project reads a file as plan lines yet. Create a " +
  "file-source Datastream with a media plan template: its Workbench is then " +
  "where the plan is named and where every dated revision of it is imported.";

/** Le libellé de la porte vers un porteur, composé UNE fois. */
export function carrierDoorLabel(name: string): string {
  return `Open ${name}`;
}

/** Ce qu'un porteur donné permet, dit avec son nom — jamais un terme de base. */
export function carrierSentence(carrier: {
  name: string;
  plan_name?: string | null;
}): string {
  return carrier.plan_name
    ? `${carrier.name} carries the media plan “${carrier.plan_name}”. Its next dated revision is imported there.`
    : `${carrier.name} reads files as plan lines and carries no plan yet. The plan is named there.`;
}
