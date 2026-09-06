/**
 * AI-290 — CE TIROIR RENDAIT SANS AUCUN STYLE, et il est monte (`Procedures.tsx`).
 *
 * `grep -rl skill-editor ui/admin/src` rendait DEUX fichiers, tous deux `.tsx`,
 * ZERO `.css` : les trente-trois classes `skill-editor-*` sur lesquelles il
 * s'appuyait n'etaient definies nulle part, et ses champs etaient des `input`
 * nus. Un ecran vivant, sans typographie, sans espacement, sans etat de focus.
 *
 * Il est desormais porte sur les primitives de `ui/admin/src/ui`, comme les
 * autres ecrans. Les libelles, les roles et les `data-testid` sont INCHANGES :
 * ce qui change est la peau, pas ce que la personne peut faire ni ce que les
 * tests interrogent.
 *
 * Le tiroir ne jette plus le formulaire en silence : Escape, le clic sur le
 * fond, la croix et Cancel passent tous par `requestClose`, qui demande une
 * confirmation des qu'une edition a eu lieu. Sans edition, Escape ferme tout de
 * suite — une question qu'on pose pour rien est une question de trop.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { apiGet } from "../lib/apiFetch";
import { Button, ConfirmDialog, Input, NativeSelect, Status, Textarea } from "../ui";
import type { ContextHubContentContext } from "./ContextHubLayout";
import type { TaxonomyType } from "./businessTaxonomyApi";
import SkillStepList, {
  parseAcceptance,
  parseCommonErrors,
  parseSkillSteps,
  parseStringList,
  serializeSkillFrontmatter,
  type CommonError,
  type MdmReferences,
  type StandardFrontmatter,
  type StepAction,
} from "./SkillStepList";

/**
 * Le vocabulaire d'action, ferme cote serveur (`context_store.STEP_ACTIONS`).
 * Il est repris ici parce qu'un editeur qui offrirait une septieme valeur
 * ferait un 422 a l'enregistrement au lieu d'un refus a la saisie.
 */
const STEP_ACTIONS: StepAction[] = ["read", "run", "edit", "analyze", "suggest", "create-issue"];

export interface SkillEditorInitialValue {
  mode: "create" | "edit";
  procedureId: string | null;
  name: string;
  description: string;
  owner: string;
  frontmatterYaml: string;
  bodyMd: string;
  /**
   * AI-157 : ce que la Skill designe du modele de donnees, resolu PAR LE
   * SERVEUR. `SkillStepList` savait le rendre depuis le 2026-08-04 et rien ne
   * le remplissait -- la resolution ne vivait que dans l'outil MCP
   * `get_procedure`. `null` = pas encore lu (la liste ne le porte pas), et le
   * panneau se tait plutot que d'affirmer « aucun champ ».
   */
  mdmReferences?: MdmReferences | null;
}

export interface SkillEditorSaveValue {
  name: string;
  description: string;
  owner: string;
  frontmatterYaml: string;
  bodyMd: string;
  toolBindings: Array<{ step: number; tool: string; viz_tag?: string }>;
  mdmTags: string[];
  /**
   * Les cinq blocs standardises du frontmatter, valides par
   * `core/context_store.py` depuis le 2026-08-03 et jusqu'ici editables NULLE
   * PART : la console les rendait (SkillStepList) sans pouvoir les ecrire, donc
   * une Skill creee ici ne pouvait pas etre standardisee.
   */
  standard: StandardFrontmatter;
  businessLink: {
    taxonomy_type: TaxonomyType;
    taxonomy_id: string;
    relation_type: string;
  } | null;
}

interface SkillEditorDrawerProps {
  projectId: string;
  initial: SkillEditorInitialValue;
  businessContext?: ContextHubContentContext;
  saving: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (value: SkillEditorSaveValue) => void;
}

interface SkillTool {
  name: string;
  description: string;
  profile: string;
  effect: string;
  data_class: string;
  confirmation_mode: string;
}

interface TargetField {
  name: string;
  display_name: string;
  field_kind: "metric" | "dimension";
  description: string | null;
  status?: "draft" | "approved";
}

/**
 * UN pas, et un seul modele pour les deux moities qui existaient.
 *
 * L'editeur ecrivait `## Step N` dans le corps et `tool_bindings` dans le
 * frontmatter ; `SkillStepList` rendait `steps:` -- que personne ne pouvait
 * saisir. Les deux moities parlaient du meme pas sans se rencontrer. Ici le pas
 * porte son genre d'action, son libelle, ce sur quoi il agit et sa branche
 * d'arret, en plus de sa prose.
 */
interface SkillStep {
  id: string;
  instruction: string;
  tool: string;
  vizTag: string;
  action: StepAction | "";
  label: string;
  target: string;
  command: string;
  stopIf: string;
}

const EMPTY_STEP: Omit<SkillStep, "id"> = {
  instruction: "",
  tool: "",
  vizTag: "",
  action: "",
  label: "",
  target: "",
  command: "",
  stopIf: "",
};

interface TaxonomyOption {
  key: string;
  type: TaxonomyType;
  id: string;
  label: string;
  name: string;
}

function scalar(raw: string): string {
  const value = raw.trim();
  if (!value) return "";
  try {
    const parsed = JSON.parse(value);
    return typeof parsed === "string" ? parsed : value;
  } catch {
    return value.replace(/^['"]|['"]$/g, "");
  }
}

function frontmatterBlock(raw: string, key: string): { inline: string; lines: string[] } | null {
  const lines = raw.split(/\r?\n/);
  const start = lines.findIndex((line) => line.startsWith(`${key}:`));
  if (start < 0) return null;
  const inline = lines[start].slice(key.length + 1).trim();
  const block: string[] = [];
  for (let index = start + 1; index < lines.length; index += 1) {
    if (lines[index].trim() && !/^\s/.test(lines[index])) break;
    block.push(lines[index]);
  }
  return { inline, lines: block };
}

interface ParsedToolBindings {
  values: Array<{ step: number; tool: string; viz_tag?: string }>;
  invalid: boolean;
}

function bindingValue(value: unknown): { step: number; tool: string; viz_tag?: string } | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (!Number.isInteger(record.step) || Number(record.step) < 1) return null;
  if (typeof record.tool !== "string" || !record.tool.trim()) return null;
  if (record.viz_tag != null && (typeof record.viz_tag !== "string" || !record.viz_tag.trim())) return null;
  return {
    step: Number(record.step),
    tool: record.tool.trim(),
    ...(typeof record.viz_tag === "string" ? { viz_tag: record.viz_tag.trim() } : {}),
  };
}

function parseToolBindingState(raw: string): ParsedToolBindings {
  const block = frontmatterBlock(raw, "tool_bindings");
  if (!block) return { values: [], invalid: false };
  if (block.inline) {
    try {
      const parsed: unknown = JSON.parse(block.inline);
      if (!Array.isArray(parsed)) return { values: [], invalid: true };
      const values = parsed.map(bindingValue);
      return {
        values: values.filter((value): value is NonNullable<typeof value> => value !== null),
        invalid: values.some((value) => value === null),
      };
    } catch {
      return { values: [], invalid: true };
    }
  }
  const values: Array<{ step: number; tool: string; viz_tag?: string }> = [];
  let current: { step: number; tool: string; viz_tag?: string } | null = null;
  let invalid = false;
  for (const line of block.lines) {
    const stepMatch = /^\s*-\s*step:\s*(.+)$/.exec(line);
    if (stepMatch) {
      if (current) {
        const normalized = bindingValue(current);
        if (normalized) values.push(normalized);
        else invalid = true;
      }
      current = { step: Number(scalar(stepMatch[1])), tool: "" };
      continue;
    }
    const toolMatch = /^\s+tool:\s*(.+)$/.exec(line);
    if (toolMatch && current) current.tool = scalar(toolMatch[1]);
    const vizMatch = /^\s+viz_tag:\s*(.+)$/.exec(line);
    if (vizMatch && current) current.viz_tag = scalar(vizMatch[1]);
  }
  if (current) {
    const normalized = bindingValue(current);
    if (normalized) values.push(normalized);
    else invalid = true;
  }
  return { values, invalid };
}

export function parseToolBindings(raw: string): Array<{ step: number; tool: string; viz_tag?: string }> {
  return parseToolBindingState(raw).values;
}
export function parseMdmTags(raw: string): string[] {
  const block = frontmatterBlock(raw, "mdm_tags");
  if (!block) return [];
  if (block.inline) {
    try {
      const parsed = JSON.parse(block.inline);
      return Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : [];
    } catch {
      return [];
    }
  }
  return block.lines
    .map((line) => /^\s*-\s*(.+)$/.exec(line)?.[1])
    .filter((value): value is string => Boolean(value))
    .map(scalar);
}

function parseSteps(
  bodyMd: string,
  bindings: ReturnType<typeof parseToolBindings>,
  frontmatterYaml: string,
): SkillStep[] {
  const headers = [...bodyMd.matchAll(/^## Step (\d+)\s*$/gm)];
  const byStep = new Map(bindings.map((binding) => [binding.step, binding]));
  // La sequence declaree fait foi sur le genre d'action et le libelle ; le corps
  // fait foi sur la prose. Les deux sont joints par le NUMERO de pas, seule cle
  // que les deux moities partagent.
  const declared = new Map(parseSkillSteps(frontmatterYaml).map((step) => [step.step, step]));
  const declaredFor = (step: number): Pick<SkillStep, "action" | "label" | "target" | "command" | "stopIf"> => {
    const found = declared.get(step);
    return {
      action: found?.action ?? "",
      label: found?.label ?? "",
      target: found?.target ?? "",
      command: found?.command ?? "",
      stopIf: found?.stop_if ?? "",
    };
  };
  if (headers.length === 0) {
    const highest = Math.max(1, ...bindings.map((binding) => binding.step), ...declared.keys());
    return Array.from({ length: highest }, (_, index) => {
      const step = index + 1;
      const binding = byStep.get(step);
      return {
        ...EMPTY_STEP,
        id: `initial-${step}`,
        instruction: step === 1 ? bodyMd.trim() : "",
        tool: binding?.tool ?? declared.get(step)?.tool ?? "",
        vizTag: binding?.viz_tag ?? "",
        ...declaredFor(step),
      };
    });
  }
  return headers.map((header, index) => {
    const step = Number(header[1]);
    const contentStart = (header.index ?? 0) + header[0].length;
    const contentEnd = headers[index + 1]?.index ?? bodyMd.length;
    const binding = byStep.get(step);
    return {
      ...EMPTY_STEP,
      id: `initial-${step}-${index}`,
      instruction: bodyMd.slice(contentStart, contentEnd).trim(),
      tool: binding?.tool ?? declared.get(step)?.tool ?? "",
      vizTag: binding?.viz_tag ?? "",
      ...declaredFor(step),
    };
  });
}

/**
 * Ce qui empeche un pas d'etre standardise -- dit A LA SAISIE, jamais en 422.
 *
 * La regle est celle du serveur : un pas qui declare une action doit porter un
 * libelle ET au moins une des trois facons de dire sur quoi il agit. Un pas qui
 * ne declare RIEN reste licite : une Skill ecrite avant ce jour n'a pas a etre
 * refusee parce que l'editeur a appris un nouveau champ.
 */
function stepRefusal(step: SkillStep): string | null {
  const acts = Boolean(step.target.trim() || step.command.trim() || step.tool.trim());
  const started = Boolean(step.action || step.label.trim() || step.stopIf.trim());
  if (!started) return null;
  if (!step.action) return "pick an action";
  if (!step.label.trim()) return "write a one-line summary";
  if (!acts) return "name a target, a command or a tool";
  return null;
}

function taxonomyOptions(context?: ContextHubContentContext): TaxonomyOption[] {
  if (!context) return [];
  const domainById = new Map(context.domains.map((domain) => [domain.id, domain]));
  const classificationById = new Map(context.classifications.map((item) => [item.id, item]));
  const pathFor = (id: string) => {
    const item = classificationById.get(id);
    if (!item) return id;
    const parts = [item.name];
    const visited = new Set([item.id]);
    let parentId = item.parent_id;
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      const parent = classificationById.get(parentId);
      if (!parent) break;
      parts.unshift(parent.name);
      parentId = parent.parent_id;
    }
    const domain = domainById.get(item.domain_id);
    if (domain) parts.unshift(domain.name);
    return parts.join(" / ");
  };
  return [
    ...context.domains.filter((domain) => domain.status === "active").map((domain) => ({
      key: `business_domain:${domain.id}`,
      type: "business_domain" as const,
      id: domain.id,
      label: domain.name,
      name: domain.name,
    })),
    ...context.classifications.filter((item) => item.status === "active").map((item) => ({
      key: `business_classification:${item.id}`,
      type: "business_classification" as const,
      id: item.id,
      label: pathFor(item.id),
      name: item.name,
    })),
  ];
}

function normalizedBody(steps: SkillStep[]): string {
  return `${steps.map((step, index) => `## Step ${index + 1}\n\n${step.instruction.trim()}`).join("\n\n")}\n`;
}

/** Un bloc de liste plate du frontmatter, saisi ligne a ligne. */
function StringListEditor({
  title,
  subtitle,
  placeholder,
  values,
  onChange,
  disabled,
}: {
  title: string;
  subtitle: string;
  placeholder: string;
  values: string[];
  onChange: (values: string[]) => void;
  disabled: boolean;
}) {
  return (
    <>
      <div className="mt-2 flex items-end justify-between gap-3">
        <div className="flex flex-col gap-0.5 [&>span]:text-caption [&>span]:uppercase [&>span]:tracking-wide [&>span]:text-text-secondary [&>strong]:text-label [&>strong]:font-label [&>strong]:text-text"><span>{title}</span><strong>{subtitle}</strong></div>
        <Button type="button" variant="secondary" size="sm" onClick={() => onChange([...values, ""])} disabled={disabled}>+ Add {title.toLowerCase()}</Button>
      </div>
      <div className="flex flex-col gap-2">
        {values.map((value, index) => (
          <div key={`${title}-${index}`} className="flex flex-wrap items-end gap-2">
            <label className="flex min-w-60 flex-1 flex-col gap-1 text-xs">{title} {index + 1}
              <Input
                value={value}
                onChange={(event) => onChange(values.map((item, position) => position === index ? event.target.value : item))}
                disabled={disabled}
                aria-label={`${title} ${index + 1}`}
                placeholder={placeholder}
              />
            </label>
            <Button type="button" variant="ghost" size="sm" onClick={() => onChange(values.filter((_, position) => position !== index))} disabled={disabled}>Remove</Button>
          </div>
        ))}
      </div>
    </>
  );
}

export default function SkillEditorDrawer({
  projectId,
  initial,
  businessContext,
  saving,
  error,
  onClose,
  onSave,
}: SkillEditorDrawerProps) {
  const nameRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description);
  const [owner, setOwner] = useState(initial.owner);
  const initialBindingState = useMemo(() => parseToolBindingState(initial.frontmatterYaml), [initial.frontmatterYaml]);
  const initialBindings = initialBindingState.values;
  const [steps, setSteps] = useState<SkillStep[]>(() =>
    parseSteps(initial.bodyMd, initialBindings, initial.frontmatterYaml),
  );
  const [mdmTags, setMdmTags] = useState<string[]>(() => parseMdmTags(initial.frontmatterYaml));
  const [acceptance, setAcceptance] = useState<string[]>(() => parseAcceptance(initial.frontmatterYaml));
  const [keywords, setKeywords] = useState<string[]>(() => parseStringList(initial.frontmatterYaml, "keywords"));
  const [evidenceRequirements, setEvidenceRequirements] = useState<string[]>(() =>
    parseStringList(initial.frontmatterYaml, "evidence_requirements"),
  );
  const [antiTriggers, setAntiTriggers] = useState<string[]>(() =>
    parseStringList(initial.frontmatterYaml, "anti_triggers"),
  );
  const [commonErrors, setCommonErrors] = useState<CommonError[]>(() =>
    parseCommonErrors(initial.frontmatterYaml),
  );
  const [tools, setTools] = useState<SkillTool[]>([]);
  const [metrics, setMetrics] = useState<TargetField[]>([]);
  const [toolStatus, setToolStatus] = useState("Loading server tools…");
  const [metricStatus, setMetricStatus] = useState("Loading MDM metrics…");
  const [metricSearch, setMetricSearch] = useState("");
  const [businessKey, setBusinessKey] = useState("");
  const [relationType, setRelationType] = useState("applies_to");
  /**
   * Y a-t-il eu une edition depuis l'ouverture ?
   *
   * Escape et le clic sur le fond fermaient le tiroir SANS RIEN DEMANDER : tout
   * un runbook saisi disparaissait sur une touche. Les saisies de texte, de
   * liste et de case remontent un evenement `change` React jusqu'au `section`
   * ci-dessous ; les gestes de STRUCTURE (ajouter, retirer, reordonner) sont des
   * boutons, donc ils se signalent eux-memes par `markDirty`.
   */
  const [dirty, setDirty] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const markDirty = () => setDirty(true);
  const editing = <T,>(setter: (value: T) => void) => (value: T) => {
    markDirty();
    setter(value);
  };
  /** Fermer coute quelque chose des qu'on a ecrit : on demande d'abord. */
  const requestClose = () => {
    if (saving) return;
    if (dirty) {
      setDiscardOpen(true);
      return;
    }
    onClose();
  };

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.allSettled([
      apiGet<{ tools: SkillTool[] }>(`/api/context/skill-tools?project_id=${encodeURIComponent(projectId)}`, { signal: controller.signal })
        // Une reponse sans `tools` n'est pas une liste vide : elle FAISAIT
        // TOMBER le tiroir (`tools.map` sur undefined). Un catalogue illisible
        // se dit, il ne casse pas l'ecran d'edition.
        .then((result) => { const list = Array.isArray(result?.tools) ? result.tools : []; setTools(list); setToolStatus(list.length ? "" : "No server tools are currently available."); })
        .catch((caught) => { if (!controller.signal.aborted) setToolStatus(caught instanceof Error ? caught.message : "Server tools are unavailable."); }),
      apiGet<TargetField[]>(`/api/datamodel/fields?project_id=${encodeURIComponent(projectId)}&kind=metric`, { signal: controller.signal })
        .then((result) => { const list = Array.isArray(result) ? result : []; setMetrics(list); setMetricStatus(list.length ? "" : "No governed MDM metrics are available."); })
        .catch((caught) => { if (!controller.signal.aborted) setMetricStatus(caught instanceof Error ? caught.message : "MDM metrics are unavailable."); }),
    ]);
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // Le dialogue de confirmation gere sa propre touche Escape ; le tiroir se
      // tait tant qu'il est ouvert, sinon la question se reposerait a elle-meme.
      if (event.key !== "Escape" || saving || discardOpen) return;
      if (dirty) {
        setDiscardOpen(true);
        return;
      }
      onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, saving, dirty, discardOpen]);

  const businessOptions = useMemo(() => taxonomyOptions(businessContext), [businessContext]);
  const currentBusinessLinks = initial.procedureId
    ? businessContext?.links.filter((link) => link.target_type === "procedure" && link.target_id === initial.procedureId) ?? []
    : [];
  const linkedBusinessKeys = new Set(currentBusinessLinks.map((link) => `${link.taxonomy_type}:${link.taxonomy_id}`));
  const availableBusinessOptions = businessOptions.filter((option) => !linkedBusinessKeys.has(option.key));
  const toolByName = new Map(tools.map((tool) => [tool.name, tool]));
  const unavailableTools = steps.map((step) => step.tool).filter((tool) => tool && !toolByName.has(tool));
  const visibleMetrics = metrics.filter((metric) =>
    `${metric.display_name} ${metric.name}`.toLowerCase().includes(metricSearch.trim().toLowerCase()),
  );
  const bodyMd = normalizedBody(steps);
  const refusals = steps
    .map((step, index) => ({ index, reason: stepRefusal(step) }))
    .filter((entry): entry is { index: number; reason: string } => entry.reason !== null);
  const standard: StandardFrontmatter = {
    steps: steps.flatMap((step, index) =>
      stepRefusal(step) === null && step.action
        ? [{
          step: index + 1,
          action: step.action,
          label: step.label.trim(),
          ...(step.target.trim() ? { target: step.target.trim() } : {}),
          ...(step.command.trim() ? { command: step.command.trim() } : {}),
          ...(step.tool.trim() ? { tool: step.tool.trim() } : {}),
          ...(step.stopIf.trim() ? { stop_if: step.stopIf.trim() } : {}),
        }]
        : [],
    ),
    acceptance: acceptance.map((item) => item.trim()).filter(Boolean),
    // Un symptome sans cause est un constat, pas une aide -- et le serveur le
    // refuse. L'editeur ne l'envoie donc pas plutot que de faire un 422.
    commonErrors: commonErrors
      .map((error) => ({ symptom: error.symptom.trim(), causes: error.causes.map((cause) => cause.trim()).filter(Boolean) }))
      .filter((error) => error.symptom && error.causes.length > 0),
    keywords: keywords.map((item) => item.trim()).filter(Boolean),
    evidenceRequirements: evidenceRequirements.map((item) => item.trim()).filter(Boolean),
    antiTriggers: antiTriggers.map((item) => item.trim()).filter(Boolean),
  };
  // L'apercu lit l'ETAT EN COURS, pas le frontmatter charge : un apercu qui rend
  // la version enregistree pendant qu'on edite ment sur ce qu'on est en train
  // d'ecrire.
  const previewYaml = serializeSkillFrontmatter(standard).join("\n");

  const updateStep = (index: number, patch: Partial<SkillStep>) => {
    markDirty();
    setSteps((current) => current.map((step, position) => position === index ? { ...step, ...patch } : step));
  };
  const moveStep = (index: number, direction: -1 | 1) => {
    markDirty();
    setSteps((current) => {
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= current.length) return current;
      const next = [...current];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      return next;
    });
  };
  const submit = () => {
    const selectedBusiness = businessOptions.find((option) => option.key === businessKey);
    const toolBindings = steps.flatMap((step, index) => step.tool ? [{
      step: index + 1,
      tool: step.tool,
      ...(step.vizTag.trim() ? { viz_tag: step.vizTag.trim() } : {}),
    }] : []);
    onSave({
      name,
      description,
      owner,
      frontmatterYaml: initial.frontmatterYaml,
      bodyMd,
      toolBindings,
      mdmTags,
      standard,
      businessLink: selectedBusiness ? {
        taxonomy_type: selectedBusiness.type,
        taxonomy_id: selectedBusiness.id,
        relation_type: relationType,
      } : null,
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose(); }}>
      <section className="flex max-h-[92vh] w-full max-w-[1400px] flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-lg" role="dialog" aria-modal="true" aria-labelledby="skill-editor-title" onChange={markDirty}>
        <header className="flex items-start justify-between gap-4 border-b border-border px-6 py-5">
          <div>
            <span>Governed agent runbook</span>
            <h2 id="skill-editor-title">{initial.mode === "create" ? "Create skill" : `Edit ${initial.name}`}</h2>
            <p>Define the ordered guidance, approved tools and business evidence the agent should follow.</p>
          </div>
          <Button type="button" onClick={requestClose} disabled={saving} aria-label="Close skill editor">×</Button>
        </header>

        <div className="grid flex-1 grid-cols-1 gap-0 overflow-hidden lg:grid-cols-[320px_minmax(0,1fr)_360px]">
          <aside className="flex flex-col gap-3 overflow-y-auto border-border p-6 lg:border-r">
            <div className="flex flex-col gap-0.5 [&>span]:text-caption [&>span]:uppercase [&>span]:tracking-wide [&>span]:text-text-secondary [&>strong]:text-label [&>strong]:font-label [&>strong]:text-text"><span>Identity</span><strong>What this skill governs</strong></div>
            <label>Name<Input ref={nameRef} value={name} onChange={(event) => setName(event.target.value)} disabled={saving} placeholder="e.g. Post-click attribution reconciliation" /></label>
            <label>Description<Textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} disabled={saving} placeholder="One line: what this Skill governs" /></label>
            <label>Owner<Input data-testid="procedure-editor-owner" value={owner} onChange={(event) => setOwner(event.target.value)} disabled={saving} placeholder="owner@example.com" /></label>

            <div className="flex flex-col gap-0.5 [&>span]:text-caption [&>span]:uppercase [&>span]:tracking-wide [&>span]:text-text-secondary [&>strong]:text-label [&>strong]:font-label [&>strong]:text-text"><span>Business scope</span><strong>Governed taxonomy link</strong></div>
            {/* The link's own word, served by `business_taxonomy.list_links` —
                not one this drawer re-derives from the option list it happens to
                have loaded. That derivation printed `bdom_<ULID>` for a link
                whose taxonomy the options did not carry, which is the one case
                where a person needs to be told something is wrong. */}
            {currentBusinessLinks.length > 0 && <div className="flex flex-wrap gap-2 [&>span]:flex [&>span]:flex-col [&>span]:rounded [&>span]:border [&>span]:border-border [&>span]:px-2 [&>span]:py-1 [&_small]:text-caption [&_small]:text-text-secondary">{currentBusinessLinks.map((link) => (
              <span key={link.id}><strong>{link.taxonomy_name ?? "Business key no longer readable"}</strong><small>{link.relation_type.replaceAll("_", " ")}</small></span>
            ))}</div>}
            <label>Add business key<NativeSelect value={businessKey} onChange={(event) => setBusinessKey(event.target.value)} disabled={saving}>
              <option value="">No new business link</option>
              {availableBusinessOptions.map((option) => <option key={option.key} value={option.key}>{option.label}</option>)}
            </NativeSelect></label>
            {businessKey && <label>Relationship<NativeSelect value={relationType} onChange={(event) => setRelationType(event.target.value)} disabled={saving}>
              <option value="applies_to">applies to</option><option value="explains">explains</option><option value="owns">owns</option><option value="uses">uses</option>
            </NativeSelect></label>}

            <div className="flex flex-col gap-0.5 [&>span]:text-caption [&>span]:uppercase [&>span]:tracking-wide [&>span]:text-text-secondary [&>strong]:text-label [&>strong]:font-label [&>strong]:text-text"><span>MDM tags</span><strong>Canonical metrics</strong></div>
            <Input aria-label="Search MDM metrics" value={metricSearch} onChange={(event) => setMetricSearch(event.target.value)} placeholder="Search governed metrics" />
            {metricStatus && <p className="m-0 text-caption text-text-secondary">{metricStatus}</p>}
            <div className="flex max-h-64 flex-col gap-1 overflow-y-auto rounded border border-border p-2">
              {visibleMetrics.map((metric) => <label key={metric.name} className="flex cursor-pointer items-start gap-2 rounded px-1 py-1 hover:bg-surface-muted [&_span]:flex [&_span]:flex-col [&_strong]:text-ui [&_small]:text-caption [&_small]:text-text-secondary">
                <Input type="checkbox" checked={mdmTags.includes(metric.name)} onChange={() => setMdmTags((current) => current.includes(metric.name) ? current.filter((item) => item !== metric.name) : [...current, metric.name])} disabled={saving} />
                <span><strong>{metric.display_name || metric.name}</strong><small>{metric.name}</small></span>
              </label>)}
            </div>
          </aside>

          <main className="flex flex-col gap-4 overflow-y-auto p-6">
            <div className="mt-2 flex items-end justify-between gap-3">
              <div className="flex flex-col gap-0.5 [&>span]:text-caption [&>span]:uppercase [&>span]:tracking-wide [&>span]:text-text-secondary [&>strong]:text-label [&>strong]:font-label [&>strong]:text-text"><span>Execution rail</span><strong>{steps.length} ordered {steps.length === 1 ? "step" : "steps"}</strong></div>
              <Button type="button" onClick={() => { markDirty(); setSteps((current) => [...current, { ...EMPTY_STEP, id: `step-${Date.now()}` }]); }} disabled={saving}>+ Add step</Button>
            </div>
            {toolStatus && <p className="m-0 text-caption text-text-secondary">{toolStatus}</p>}
            {initialBindingState.invalid && <Status as="block" tone="warning" title="Existing tool binding metadata is malformed">Saving replaces it with the ordered steps below.</Status>}
            {unavailableTools.length > 0 && <Status as="block" tone="warning" title="Reassign unavailable tools before saving">{unavailableTools.join(", ")}.</Status>}
            <div className="flex flex-col gap-3">
              {steps.map((step, index) => {
                const selectedTool = toolByName.get(step.tool);
                return <article className="flex gap-3 rounded-lg border border-border p-4" key={step.id}>
                  <div className="flex flex-col items-center gap-2 [&>span]:text-label [&>span]:font-label [&>span]:text-text-secondary [&>i]:block [&>i]:w-px [&>i]:flex-1 [&>i]:bg-border"><span>{String(index + 1).padStart(2, "0")}</span><i /></div>
                  <div className="flex min-w-0 flex-1 flex-col gap-3">
                    <div className="flex items-center justify-between gap-2 [&>span]:flex [&>span]:gap-1">
                      <strong>Step {index + 1}</strong>
                      <span><Button type="button" onClick={() => moveStep(index, -1)} disabled={saving || index === 0} aria-label={`Move step ${index + 1} up`}>↑</Button><Button type="button" onClick={() => moveStep(index, 1)} disabled={saving || index === steps.length - 1} aria-label={`Move step ${index + 1} down`}>↓</Button><Button type="button" onClick={() => { markDirty(); setSteps((current) => current.length === 1 ? current : current.filter((_, position) => position !== index)); }} disabled={saving || steps.length === 1}>Remove</Button></span>
                    </div>
                    {/* Le pas STANDARDISE : genre d'action, libelle, ce sur quoi
                        il agit, et sa branche d'arret. C'est ce que la console
                        rend en icones et ce que `steps:` porte en base. */}
                    <div className="flex flex-wrap gap-3">
                      <label className="flex min-w-40 flex-1 flex-col gap-1 text-xs">Action
                        <NativeSelect
                          value={step.action}
                          onChange={(event) => updateStep(index, { action: event.target.value as StepAction | "" })}
                          disabled={saving}
                          aria-label={`Step ${index + 1} action`}
                        >
                          <option value="">Not standardized</option>
                          {STEP_ACTIONS.map((action) => <option key={action} value={action}>{action}</option>)}
                        </NativeSelect>
                      </label>
                      <label className="flex min-w-60 flex-[2] flex-col gap-1 text-xs">Summary
                        <Input
                          value={step.label}
                          onChange={(event) => updateStep(index, { label: event.target.value })}
                          disabled={saving}
                          aria-label={`Step ${index + 1} summary`}
                          placeholder="One line the console shows next to the icon"
                        />
                      </label>
                    </div>
                    <div className="flex flex-wrap gap-3">
                      <label className="flex min-w-40 flex-1 flex-col gap-1 text-xs">Target
                        <Input value={step.target} onChange={(event) => updateStep(index, { target: event.target.value })} disabled={saving} aria-label={`Step ${index + 1} target`} placeholder="A file, a screen, a route" />
                      </label>
                      <label className="flex min-w-40 flex-1 flex-col gap-1 text-xs">Command
                        <Input value={step.command} onChange={(event) => updateStep(index, { command: event.target.value })} disabled={saving} aria-label={`Step ${index + 1} command`} placeholder="npx vitest run" />
                      </label>
                      <label className="flex min-w-40 flex-1 flex-col gap-1 text-xs">Stop if
                        <Input value={step.stopIf} onChange={(event) => updateStep(index, { stopIf: event.target.value })} disabled={saving} aria-label={`Step ${index + 1} stop condition`} placeholder="The failure branch, if there is one" />
                      </label>
                    </div>
                    {refusals.some((refusal) => refusal.index === index) && (
                      <Status tone="warning" data-testid={`skill-step-refusal-${index + 1}`}>
                        Incomplete step — {refusals.find((refusal) => refusal.index === index)?.reason}.
                      </Status>
                    )}
                    <label>Instruction<Textarea rows={4} value={step.instruction} onChange={(event) => updateStep(index, { instruction: event.target.value })} disabled={saving} placeholder="Describe the decision or analysis performed in this step" /></label>
                    <div className="flex flex-wrap gap-3">
                      <label>Server tool<NativeSelect value={step.tool} onChange={(event) => updateStep(index, { tool: event.target.value })} disabled={saving}>
                        <option value="">No tool binding</option>
                        {step.tool && !toolByName.has(step.tool) && <option value={step.tool}>{step.tool} · unavailable</option>}
                        {tools.map((tool) => <option key={tool.name} value={tool.name}>{tool.name}</option>)}
                      </NativeSelect></label>
                      <label>Visualization tag<Input value={step.vizTag} onChange={(event) => updateStep(index, { vizTag: event.target.value })} disabled={saving || !step.tool} placeholder="Optional rendering contract" /></label>
                    </div>
                    {selectedTool && <div className="flex flex-wrap items-center gap-2 rounded border border-border bg-surface-muted p-3 [&>span]:rounded [&>span]:border [&>span]:border-border [&>span]:px-2 [&>span]:py-0.5 [&>span]:text-caption [&>p]:m-0 [&>p]:w-full [&>p]:text-caption [&>p]:text-text-secondary"><span>{selectedTool.profile}</span><span>{selectedTool.effect}</span><span>{selectedTool.data_class}</span><p>{selectedTool.description || "No catalog description."}</p></div>}
                  </div>
                </article>;
              })}
            </div>

            {/* Les quatre autres blocs standardises. Ils etaient valides par le
                serveur, rendus par la console, et saisissables NULLE PART. */}
            <StringListEditor
              title="Acceptance"
              subtitle="What a correct run leaves behind"
              placeholder="e.g. The gate command exits 0"
              values={acceptance}
              onChange={editing(setAcceptance)}
              disabled={saving}
            />
            <StringListEditor
              title="Keywords"
              subtitle="Free words the search reads"
              placeholder="e.g. reconciliation"
              values={keywords}
              onChange={editing(setKeywords)}
              disabled={saving}
            />
            {/* Story 45.5 -- le seul champ qui RETIRE la Skill d'une question.
                Un terme, pas une phrase : le classement compare des sous-chaines,
                donc une phrase entiere n'exclurait jamais rien. */}
            <StringListEditor
              title="Do not trigger on"
              subtitle="Terms this skill is NOT for"
              placeholder="e.g. billing"
              values={antiTriggers}
              onChange={editing(setAntiTriggers)}
              disabled={saving}
            />
            <StringListEditor
              title="Evidence requirements"
              subtitle="What must be produced to prove the run"
              placeholder="e.g. The command and its number"
              values={evidenceRequirements}
              onChange={editing(setEvidenceRequirements)}
              disabled={saving}
            />

            <div className="mt-2 flex items-end justify-between gap-3">
              <div className="flex flex-col gap-0.5 [&>span]:text-caption [&>span]:uppercase [&>span]:tracking-wide [&>span]:text-text-secondary [&>strong]:text-label [&>strong]:font-label [&>strong]:text-text"><span>Common errors</span><strong>Symptom, then its causes</strong></div>
              <Button type="button" onClick={() => { markDirty(); setCommonErrors((current) => [...current, { symptom: "", causes: [""] }]); }} disabled={saving}>+ Add error</Button>
            </div>
            <div className="flex flex-col gap-3">
              {commonErrors.map((error, index) => (
                <div key={`common-error-${index}`} className="flex flex-col gap-2 border-b pb-3">
                  <div className="flex flex-wrap items-end gap-2">
                    <label className="flex min-w-60 flex-1 flex-col gap-1 text-xs">Symptom
                      <Input
                        value={error.symptom}
                        onChange={(event) => setCommonErrors((current) => current.map((item, position) => position === index ? { ...item, symptom: event.target.value } : item))}
                        disabled={saving}
                        aria-label={`Common error ${index + 1} symptom`}
                        placeholder='e.g. "Connection refused"'
                      />
                    </label>
                    <Button type="button" onClick={() => { markDirty(); setCommonErrors((current) => current.filter((_, position) => position !== index)); }} disabled={saving}>Remove</Button>
                  </div>
                  {error.causes.map((cause, causeIndex) => (
                    <div key={`common-error-${index}-cause-${causeIndex}`} className="flex flex-wrap items-end gap-2 pl-4">
                      <label className="flex min-w-60 flex-1 flex-col gap-1 text-xs">Cause
                        <Input
                          value={cause}
                          onChange={(event) => setCommonErrors((current) => current.map((item, position) => position === index
                            ? { ...item, causes: item.causes.map((value, place) => place === causeIndex ? event.target.value : value) }
                            : item))}
                          disabled={saving}
                          aria-label={`Common error ${index + 1} cause ${causeIndex + 1}`}
                          placeholder="e.g. The MCP server is not running"
                        />
                      </label>
                      <Button
                        type="button"
                        onClick={() => { markDirty(); setCommonErrors((current) => current.map((item, position) => position === index
                          ? { ...item, causes: item.causes.filter((_, place) => place !== causeIndex) }
                          : item)); }}
                        disabled={saving || error.causes.length === 1}
                      >Remove</Button>
                    </div>
                  ))}
                  <div className="pl-4">
                    <Button type="button" onClick={() => { markDirty(); setCommonErrors((current) => current.map((item, position) => position === index ? { ...item, causes: [...item.causes, ""] } : item)); }} disabled={saving}>+ Add cause</Button>
                  </div>
                </div>
              ))}
            </div>
          </main>

          <aside className="flex flex-col gap-3 overflow-y-auto border-border bg-surface-muted p-6 lg:border-l" data-testid="procedure-preview">
            <div className="flex flex-col gap-0.5 [&>span]:text-caption [&>span]:uppercase [&>span]:tracking-wide [&>span]:text-text-secondary [&>strong]:text-label [&>strong]:font-label [&>strong]:text-text"><span>Markdown preview</span><strong>Skill body</strong></div>
            {/* La sequence, les criteres et les erreurs courantes du frontmatter.
                Valides par `core/context_store.py` depuis le 2026-08-03, ils
                existaient en base sans etre rendus nulle part. Le composant se
                rend NUL quand la Skill n'en porte aucun : rien ne change pour
                celles ecrites avant. */}
            <SkillStepList
              frontmatterYaml={previewYaml}
              mdmReferences={initial.mdmReferences}
            />
            <pre>{bodyMd}</pre>
            <div className="flex flex-wrap gap-3 border-t border-border pt-3 [&>span]:flex [&>span]:flex-col [&>span]:text-caption [&>span]:text-text-secondary [&_strong]:text-label [&_strong]:font-label [&_strong]:text-text"><span><strong>{steps.filter((step) => step.tool).length}</strong> tool bindings</span><span><strong>{mdmTags.length}</strong> MDM tags</span><span><strong>{standard.steps.length}</strong> standardized steps</span><span><strong>{currentBusinessLinks.length + (businessKey ? 1 : 0)}</strong> business keys</span></div>
          </aside>
        </div>

        <footer className="flex items-center justify-end gap-3 border-t border-border px-6 py-4">
          <div className="mr-auto">{error && <Status as="block" tone="error" data-testid="procedure-editor-error">{error}</Status>}</div>
          <Button type="button" onClick={requestClose} disabled={saving}>Cancel</Button>
          <Button type="button" className="is-primary" onClick={submit} disabled={saving || unavailableTools.length > 0 || refusals.length > 0}>{saving ? "Saving…" : "Save"}</Button>
        </footer>
      </section>

      {/* Ce qui a ete ecrit n'est nulle part ailleurs : le tiroir ne le jette pas
          sans le dire. Le geste qui garde le travail est de continuer a editer,
          donc c'est l'annulation qui est le chemin sur. */}
      <ConfirmDialog
        open={discardOpen}
        onOpenChange={(open) => { if (!open) setDiscardOpen(false); }}
        title="Discard changes?"
        description={`The edits to ${initial.name ? `“${initial.name}”` : "this skill"} have not been saved. Closing now loses them — cancel and use Save to keep them.`}
        confirmLabel="Discard changes"
        destructive
        onConfirm={() => { setDiscardOpen(false); onClose(); }}
        data-testid="skill-editor-discard-confirm"
        cancelTestId="skill-editor-discard-cancel"
        confirmTestId="skill-editor-discard-accept"
      />
    </div>
  );
}