/**
 * Rendre la sequence d'une Skill : une icone, un libelle, ce sur quoi on agit.
 *
 * POURQUOI. Jean, 2026-08-03 : « 1 "Read" ca t'affiche une icone de read et la
 * description, 2 "Run" ca affiche la description, 3 "Edit" t'as un crayon et ca
 * montre ce que ca fait. [...] Comme ca en plus tu peux voir avec le resume et
 * des icones ce que ca fait. »
 *
 * Le validateur accepte depuis le meme jour `steps`, `acceptance` et
 * `common_errors` (`core/context_store.py`). Sans ce composant, ces cles
 * existaient en base et n'etaient rendues nulle part -- exactement le defaut que
 * ce depot passe sa journee a trouver : un champ valide, stocke, et consomme par
 * personne.
 *
 * ZERO LIGNE DE CSS, et c'est ce qui a fini par valoir a son voisin d'etre
 * repare. Cette phrase disait que `SkillEditorDrawer.tsx` s'appuyait sur le
 * vocabulaire herite `skill-editor-*` -- vocabulaire qu'AUCUNE feuille du depot
 * ne definissait (AI-290, mesure 2026-08-15). Le tiroir est desormais porte sur
 * les primitives de `ui/admin/src/ui` comme ce fichier l'est sur Tailwind : ni
 * couleur en dur, ni valeur d'espacement litterale, ni classe morte.
 */
import { BookOpen, CircleAlert, CircleCheck, Lightbulb, Pencil, Play, Search } from "lucide-react";
import type { ComponentType } from "react";
import { wireWord } from "../ui/glossary";

export type StepAction = "read" | "run" | "edit" | "analyze" | "suggest" | "create-issue";

/** What each step ACTION reads as, beside the union it belongs to.
 *
 *  The six words are `STEP_ACTIONS` (`server/core/context_store.py:92`), which
 *  refuses anything else at write time — so this map is total and TypeScript
 *  says so. Until story 76-3 the token was printed raw and uppercased by CSS,
 *  which is the base's spelling wearing a text-transform;
 *  `console-presentation.md` §4 asks for the person's word. `create-issue` is
 *  the only one whose word is not its token. */
const STEP_ACTION_LABEL: Record<StepAction, string> = {
  read: "Read",
  run: "Run",
  edit: "Edit",
  analyze: "Analyze",
  suggest: "Suggest",
  "create-issue": "Create issue",
};

export function stepActionLabel(action: string): string {
  return STEP_ACTION_LABEL[action as StepAction] ?? wireWord(action);
}

export interface SkillStep {
  step: number;
  action: StepAction;
  label: string;
  target?: string;
  command?: string;
  tool?: string;
  stop_if?: string;
}

export interface CommonError {
  symptom: string;
  causes: string[];
}

/**
 * Le vocabulaire est FERME cote serveur (`STEP_ACTIONS`). Une septieme valeur
 * n'arrive donc jamais jusqu'ici -- et si elle arrivait, `ACTION_ICON` la rendrait
 * sans icone plutot que de casser la page.
 */
const ACTION_ICON: Record<StepAction, ComponentType<{ className?: string }>> = {
  read: BookOpen,
  run: Play,
  edit: Pencil,
  analyze: Search,
  suggest: Lightbulb,
  "create-issue": CircleAlert,
};

const ACTIONS = new Set<string>(Object.keys(ACTION_ICON));

/** Le contenu d'un bloc `cle:` du frontmatter, lignes indentees comprises. */
function block(raw: string, key: string): string[] {
  const lines = raw.split(/\r?\n/);
  const start = lines.findIndex((line) => line.trimEnd() === `${key}:`);
  if (start < 0) return [];
  const out: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() === "") continue;
    if (!/^\s/.test(line)) break;
    out.push(line);
  }
  return out;
}

/** Retire les guillemets d'un scalaire YAML sans pretendre parser du YAML.
 *
 * Un scalaire double-quote est lu par `JSON.parse` : c'est exactement la forme
 * que `serializeSkillFrontmatter` ecrit, et un simple `slice(1, -1)` rendrait
 * `\"` litteralement -- un libelle ne survivrait pas a son propre aller-retour.
 */
function scalar(value: string): string {
  const trimmed = value.trim();
  if (trimmed.startsWith('"')) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (typeof parsed === "string") return parsed;
    } catch {
      /* une double-quote mal formee retombe sur la lecture naive ci-dessous */
    }
  }
  if (trimmed.length > 1 && /^(".*"|'.*')$/.test(trimmed)) return trimmed.slice(1, -1);
  return trimmed;
}

export function parseSkillSteps(raw: string): SkillStep[] {
  const steps: SkillStep[] = [];
  let current: Partial<SkillStep> | null = null;

  const flush = () => {
    if (!current) return;
    const { step, action, label } = current;
    // Un pas incomplet est IGNORE, jamais rendu a moitie : le serveur refuse
    // deja ces formes, donc une ici veut dire que la lecture a derive.
    if (typeof step === "number" && step >= 1 && action && ACTIONS.has(action) && label) {
      steps.push(current as SkillStep);
    }
    current = null;
  };

  for (const line of block(raw, "steps")) {
    const started = /^\s*-\s*step:\s*(.+)$/.exec(line);
    if (started) {
      flush();
      current = { step: Number(scalar(started[1])) };
      continue;
    }
    const field = /^\s+(action|label|target|command|tool|stop_if):\s*(.+)$/.exec(line);
    if (field && current) {
      const [, name, value] = field;
      (current as Record<string, unknown>)[name] = scalar(value);
    }
  }
  flush();
  return steps.sort((a, b) => a.step - b.step);
}

/** Un bloc de liste plate du frontmatter : `acceptance`, `keywords`, `evidence…`. */
export function parseStringList(raw: string, key: string): string[] {
  return block(raw, key)
    .map((line) => /^\s*-\s*(.+)$/.exec(line))
    .filter((match): match is RegExpExecArray => match !== null)
    .map((match) => scalar(match[1]))
    .filter(Boolean);
}

export function parseAcceptance(raw: string): string[] {
  return parseStringList(raw, "acceptance");
}

export function parseCommonErrors(raw: string): CommonError[] {
  const errors: CommonError[] = [];
  let current: CommonError | null = null;
  let inCauses = false;

  for (const line of block(raw, "common_errors")) {
    const symptom = /^\s*-\s*symptom:\s*(.+)$/.exec(line);
    if (symptom) {
      if (current && current.causes.length > 0) errors.push(current);
      current = { symptom: scalar(symptom[1]), causes: [] };
      inCauses = false;
      continue;
    }
    if (/^\s+causes:\s*$/.test(line)) {
      inCauses = true;
      continue;
    }
    const cause = /^\s*-\s*(.+)$/.exec(line);
    if (inCauses && cause && current) current.causes.push(scalar(cause[1]));
  }
  // Un symptome sans cause est un constat, pas une aide -- le serveur le refuse,
  // et cette lecture ne le rend pas davantage.
  if (current && current.causes.length > 0) errors.push(current);
  return errors;
}

/**
 * Les cles du frontmatter que la console SAIT ECRIRE depuis l'editeur.
 *
 * `tool_bindings` et `mdm_tags` n'y sont PAS : ils ont deja leur ecriture en
 * style flow dans `Procedures.tsx`, et deux ecrivains pour une meme cle
 * divergent. Cette liste sert au remplacement : une cle qu'on reecrit doit
 * d'abord etre retiree, sinon le frontmatter en porte deux.
 */
export const STANDARD_FRONTMATTER_KEYS = [
  "steps",
  "acceptance",
  "common_errors",
  "keywords",
  "evidence_requirements",
  "anti_triggers",
] as const;

export interface StandardFrontmatter {
  steps: SkillStep[];
  acceptance: string[];
  commonErrors: CommonError[];
  keywords: string[];
  evidenceRequirements: string[];
  /** Story 45.5 -- quand cette Skill ne doit PAS se declencher. */
  antiTriggers: string[];
}

/** Un scalaire YAML sur, quel que soit son contenu : la forme double-quote. */
function yamlScalar(value: string): string {
  return JSON.stringify(value);
}

/**
 * Ecrire les blocs standardises en style BLOC, et pas en flow.
 *
 * `tool_bindings` est ecrit en JSON parce que rien ne le relit a la main. Une
 * sequence, elle, se lit et s'edite dans le fichier -- et les 50 `SKILL.md` du
 * depot comme la procedure que le produit seme (migration 196) sont en bloc.
 * Ecrire du JSON ici aurait fait deux dialectes pour un meme champ.
 */
export function serializeSkillFrontmatter(value: StandardFrontmatter): string[] {
  const lines: string[] = [];
  if (value.steps.length > 0) {
    lines.push("steps:");
    for (const step of value.steps) {
      lines.push(`  - step: ${step.step}`);
      lines.push(`    action: ${step.action}`);
      lines.push(`    label: ${yamlScalar(step.label)}`);
      for (const key of ["target", "command", "tool", "stop_if"] as const) {
        const acted = step[key];
        if (acted) lines.push(`    ${key}: ${yamlScalar(acted)}`);
      }
    }
  }
  for (const [key, list] of [
    ["acceptance", value.acceptance],
    ["keywords", value.keywords],
    ["evidence_requirements", value.evidenceRequirements],
    ["anti_triggers", value.antiTriggers],
  ] as const) {
    if (list.length === 0) continue;
    lines.push(`${key}:`);
    for (const item of list) lines.push(`  - ${yamlScalar(item)}`);
  }
  if (value.commonErrors.length > 0) {
    lines.push("common_errors:");
    for (const error of value.commonErrors) {
      lines.push(`  - symptom: ${yamlScalar(error.symptom)}`);
      lines.push("    causes:");
      for (const cause of error.causes) lines.push(`      - ${yamlScalar(cause)}`);
    }
  }
  return lines;
}

function StepRow({ step }: { step: SkillStep }) {
  const Icon = ACTION_ICON[step.action];
  const acted = step.target ?? step.command ?? step.tool;
  return (
    <li className="flex gap-3 py-2" data-testid={`skill-step-${step.step}`}>
      <span className="flex size-8 shrink-0 items-center justify-center rounded-full border">
        {Icon ? <Icon className="size-4" aria-hidden /> : null}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-xs tabular-nums opacity-60">{step.step}</span>
          <span className="text-xs tracking-wide opacity-60">{stepActionLabel(step.action)}</span>
          <span className="text-sm font-medium">{step.label}</span>
        </div>
        {acted ? <code className="mt-1 block truncate text-xs opacity-80">{acted}</code> : null}
        {step.stop_if ? (
          <p className="mt-1 flex items-start gap-1 text-xs">
            <CircleAlert className="mt-0.5 size-3 shrink-0" aria-hidden />
            <span>
              <span className="font-medium">Stop if</span> {step.stop_if}
            </span>
          </p>
        ) : null}
      </div>
    </li>
  );
}

/** Ce qu'une Skill designe du modele de donnees, resolu par le serveur. */
export interface MdmReferences {
  inline: { resolved: Array<Record<string, unknown>>; unresolved: string[] };
  tags: { resolved: Array<Record<string, unknown>>; unresolved: string[] };
}

function FieldRow({ field }: { field: Record<string, unknown> }) {
  const kind = [field.field_kind, field.data_type, field.measure].filter(Boolean).join(" · ");
  return (
    <li className="flex flex-wrap items-baseline gap-2 text-sm">
      <code className="text-xs">{`{{${String(field.name)}}}`}</code>
      <span className="font-medium">{String(field.display_name || field.name)}</span>
      <span className="text-xs opacity-60">{kind}</span>
      {field.status === "approved" ? (
        <CircleCheck className="size-3 shrink-0" aria-label="approved" />
      ) : (
        <span className="text-xs opacity-60">{String(field.status ?? "")}</span>
      )}
    </li>
  );
}

export default function SkillStepList({
  frontmatterYaml,
  mdmReferences,
}: {
  frontmatterYaml: string;
  mdmReferences?: MdmReferences | null;
}) {
  const steps = parseSkillSteps(frontmatterYaml);
  const acceptance = parseAcceptance(frontmatterYaml);
  const commonErrors = parseCommonErrors(frontmatterYaml);
  const resolved = mdmReferences?.inline.resolved ?? [];
  const unresolved = mdmReferences?.inline.unresolved ?? [];

  if (
    steps.length === 0 &&
    acceptance.length === 0 &&
    commonErrors.length === 0 &&
    resolved.length === 0 &&
    unresolved.length === 0
  )
    return null;

  return (
    <div className="flex flex-col gap-6">
      {steps.length > 0 ? (
        <section aria-labelledby="skill-sequence-heading">
          <h3 id="skill-sequence-heading" className="text-xs uppercase tracking-wide opacity-60">
            Sequence — {steps.length} {steps.length === 1 ? "step" : "steps"}
          </h3>
          <ol className="mt-2 divide-y">
            {steps.map((step) => (
              <StepRow key={step.step} step={step} />
            ))}
          </ol>
        </section>
      ) : null}

      {acceptance.length > 0 ? (
        <section aria-labelledby="skill-acceptance-heading">
          <h3 id="skill-acceptance-heading" className="text-xs uppercase tracking-wide opacity-60">
            Acceptance — {acceptance.length}
          </h3>
          <ul className="mt-2 flex flex-col gap-1">
            {acceptance.map((criterion) => (
              <li key={criterion} className="flex items-start gap-2 text-sm">
                <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden />
                <span>{criterion}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {resolved.length > 0 || unresolved.length > 0 ? (
        <section aria-labelledby="skill-mdm-heading">
          <h3 id="skill-mdm-heading" className="text-xs uppercase tracking-wide opacity-60">
            Data model — {resolved.length} resolved
            {unresolved.length > 0 ? `, ${unresolved.length} unresolved` : ""}
          </h3>
          <ul className="mt-2 flex flex-col gap-1">
            {resolved.map((field) => (
              <FieldRow key={String(field.name)} field={field} />
            ))}
          </ul>
          {/* Une reference qui ne resout pas est MONTREE. L'omettre effacerait la
              seule trace qu'un champ a disparu -- et cette trace est le retour
              sur la Skill. */}
          {unresolved.length > 0 ? (
            <ul className="mt-2 flex flex-col gap-1">
              {unresolved.map((name) => (
                <li key={name} className="flex items-center gap-2 text-sm">
                  <CircleAlert className="size-4 shrink-0" aria-hidden />
                  <code className="text-xs">{`{{${name}}}`}</code>
                  <span className="text-xs opacity-60">names no governed field</span>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}

      {commonErrors.length > 0 ? (
        <section aria-labelledby="skill-errors-heading">
          <h3 id="skill-errors-heading" className="text-xs uppercase tracking-wide opacity-60">
            Common errors — {commonErrors.length}
          </h3>
          <dl className="mt-2 flex flex-col gap-3">
            {commonErrors.map((error) => (
              <div key={error.symptom}>
                <dt className="flex items-center gap-2 text-sm font-medium">
                  <CircleAlert className="size-4 shrink-0" aria-hidden />
                  {error.symptom}
                </dt>
                <dd className="ml-6 mt-1">
                  <ul className="list-disc pl-4 text-sm opacity-80">
                    {error.causes.map((cause) => (
                      <li key={cause}>{cause}</li>
                    ))}
                  </ul>
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ) : null}
    </div>
  );
}
