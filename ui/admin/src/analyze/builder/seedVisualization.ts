/**
 * The entry act: turn an immutable Result into a saved presentation identity.
 *
 * WHY THIS EXISTS. The canonical router refuses to build an object address
 * without an identifier — `shell/router.tsx` throws "Object routes require type
 * and identifier" — so there is no `/object/visualization/new` to navigate to,
 * and the Builder's create path had no way in from a browser at all. The
 * Visualization identity therefore has to exist BEFORE the address does. This
 * module is that one call, and it is where the Result workbench's affordance
 * leads.
 *
 * IT KEEPS NO COMPATIBILITY RULE. The family it falls back to is `table`, and
 * that is not a preference: `table` is the family the architecture obliges every
 * visual to fall back to (`visualization-and-rendering.md:189`, AC8), it needs no
 * time grain and no comparison, and its required wells are the two roles that
 * have a server source today. Which wells `table` requires and which roles they
 * accept are read off the SERVER's registry; which members exist is read off the
 * SERVER's `/visualization-options`. If the seed does not satisfy the server, the
 * server refuses it and the refusal is shown — this module never decides that a
 * document is acceptable.
 *
 * THE FALLBACK IS NO LONGER THE NORMAL CASE (story 72.5, AC22). The starting
 * point of a Visualization is a CHART TEMPLATE when the Project has one that
 * fits the Result, and the constant below only when it has none. The fallback
 * STAYS — a Project with no template must still be able to open a Builder — but
 * it became the case that is NAMED rather than the case that happens silently.
 * `compatibleTemplatesForResult` is how a caller offers the choice first, and
 * `FALLBACK_REASON` is the sentence that says why it is falling back when the
 * caller does not.
 */
import {
  applyChartTemplate,
  listChartTemplates,
  type ChartTemplateRow,
} from "../templates/chartTemplateClient";
import {
  createVisualization,
  fetchVisualizationFamilies,
  fetchVisualizationOptions,
  type VisualizationFamily,
  type VisualizationMember,
  type VisualizationOptions,
  type VisualizationRegistry,
} from "../visualizationClient";

/**
 * The universal fallback family. See the module docstring for why it is not a
 * taste, and why it is a fallback rather than a default.
 *
 * It was called `SEED_FAMILY` while it WAS the seed. It is not any more: a
 * constant that names the normal case and a constant that names the last resort
 * are two different statements, and reading `SEED_FAMILY = "table"` in
 * `ResultWorkbench` was how "every Visualization of this product starts as a
 * table" became true without anybody deciding it.
 */
export const FALLBACK_FAMILY = "table";

/** Why a Builder opened on the fallback rather than on a template. Named, always. */
export const FALLBACK_REASON =
  "No Chart Template of this Project fits this Result, so the Visualization starts from a " +
  "table — the one family every Result can be presented in. Change it in the Builder, or " +
  "create a template from it once it says what you want.";

export class NoSeedableFamily extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NoSeedableFamily";
  }
}

/**
 * Fill each REQUIRED well of `family` with members of the roles it declares it
 * accepts, up to the count it declares it takes. Purely a projection of the
 * server's own registry onto the server's own member list.
 */
export function seedBindings(
  family: VisualizationFamily,
  options: VisualizationOptions,
): Record<string, string[]> {
  const byRole: Record<string, VisualizationMember[]> = {
    measure: options.measures,
    dimension: options.dimensions,
    time: options.time,
    classification: options.classifications,
  };
  const bindings: Record<string, string[]> = {};
  for (const well of family.wells) {
    if (!well.required) continue;
    const candidates = well.accepts.flatMap((role) => byRole[role] ?? []);
    if (candidates.length === 0) {
      throw new NoSeedableFamily(
        `This Result's query selected no ${well.accepts.join(" or ")}, so the ` +
          `${family.label} family's ${well.label} well cannot be filled. Add one in Explore.`,
      );
    }
    bindings[well.name] = candidates.slice(0, well.max_members).map((member) => member.id);
  }
  return bindings;
}

export function seedDocument(
  registry: VisualizationRegistry,
  family: VisualizationFamily,
  options: VisualizationOptions,
): Record<string, unknown> {
  return {
    spec_contract_version: registry.spec_contract_version,
    schema_version: registry.schema_version,
    family: family.id,
    bindings: seedBindings(family, options),
  };
}

/**
 * The Chart Templates of this Project that FIT one exact Result.
 *
 * The verdict is the server's and is binding: this function filters on the state
 * it was given and computes nothing. A caller offers what comes back; when it is
 * empty, the fallback below is the named case rather than the silent one.
 */
export async function compatibleTemplatesForResult(
  projectId: string,
  resultId: string,
  init?: RequestInit,
): Promise<ChartTemplateRow[]> {
  const collection = await listChartTemplates(projectId, init, { resultId });
  return collection.templates.filter((row) => row.verdict?.state === "compatible");
}

/**
 * Create the Visualization identity for one Result's Query Spec version.
 *
 * Returns its id, which is the missing half of the address. Any refusal travels
 * out as the `ApiError` the caller renders — nothing is retried with a quieter
 * document, because a presentation the server refused is not one to smuggle past
 * it with fewer bindings.
 *
 * TWO WAYS IN, AND THE FIRST IS THE INTENDED ONE (AC22). Given a template and
 * the Result it was judged compatible with, the Visualization is MATERIALISED
 * from that template — one server call, through
 * `template_materialization.materialize_template`, which asks the verdict again
 * before writing anything. Given no template, the fallback family is seeded, and
 * the caller is expected to have said so on screen.
 */
export async function createVisualizationFromResult(
  projectId: string,
  querySpecVersionId: string,
  init?: RequestInit,
  startingPoint?: { templateVersionId?: string | null; resultId?: string | null },
): Promise<string> {
  const templateVersionId = startingPoint?.templateVersionId ?? null;
  const resultId = startingPoint?.resultId ?? null;
  if (templateVersionId && resultId) {
    const created = await applyChartTemplate(
      projectId,
      templateVersionId,
      { result_id: resultId },
      init,
    );
    return created.visualization_id;
  }

  const [registry, options] = await Promise.all([
    fetchVisualizationFamilies(projectId, init),
    fetchVisualizationOptions(projectId, querySpecVersionId, init),
  ]);
  const family = registry.families.find((candidate) => candidate.id === FALLBACK_FAMILY);
  if (!family) {
    throw new NoSeedableFamily(
      `The server's registry declares no \`${FALLBACK_FAMILY}\` family, so there is no ` +
        "family every Result can be presented in. Nothing has been created.",
    );
  }
  const body = {
    query_spec_version_id: querySpecVersionId,
    spec: seedDocument(registry, family, options),
  };
  const created = init
    ? await createVisualization(projectId, body, init)
    : await createVisualization(projectId, body);
  return created.visualization_id;
}
