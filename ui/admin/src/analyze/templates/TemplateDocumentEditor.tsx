/**
 * The Presentation tab's editor: the template document, WRITTEN here (AI-347).
 *
 * WHY IT EXISTS. The tab read `GET /chart-template-vocabulary` and rendered the
 * document; the write service was already proven in PostgreSQL
 * (`append_chart_template_version`, exposed by
 * `POST /chart-templates/{id}/versions`) and no screen called it. The ratified
 * table says this tab holds "the template document, edited through the same
 * typed wells and the same role vocabulary as the Builder", and the screen rule
 * is blunt: what a person comes here to do must be doable here.
 *
 * ONE QUESTION AT A TIME, AND EACH ONE REDUCES THE NEXT. The question the
 * template answers comes first; the visual family appears once it is answered;
 * the wells appear once a family is chosen, and they are THAT family's wells at
 * THAT family's bounds. So the screen cannot compose a requirement the server
 * would refuse a moment later: a well the family has not is never offered, a
 * count above what the well holds is not in the list, and a cardinality bound is
 * offered only on a well that carries one.
 *
 * NO SECOND VOCABULARY. Every well name, well label, family label, role,
 * bound, responsive profile and document key below arrives from
 * `template_vocabulary()`. This file declares none of them, and the presentation
 * options are the Builder's own `PresentationRail` rather than a second rail
 * that would drift from it.
 *
 * A REFUSAL NAMES THE GESTURE. The server answers 422 with every unsatisfied
 * point at once, each carrying a `remedy`. The remedy is what a person reads;
 * the JSON pointer is resolved to the well it is about and never printed.
 *
 * Composed only from `ui/admin/src/ui/index.ts` and the Builder's rail.
 */
import { useCallback, useMemo, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import {
  Button,
  Checkbox,
  Cluster,
  EmptyState,
  Field,
  Input,
  Label,
  NativeSelect,
  Panel,
  PanelHeader,
  Stack,
  Status,
} from "../../ui";
import PresentationRail from "../builder/PresentationRail";
import {
  appendChartTemplateVersion,
  type AppendedTemplateVersion,
  type ChartTemplateDetail,
  type TemplateFamilyChoice,
  type TemplateFamilyWell,
  type TemplateVocabulary,
} from "./chartTemplateClient";

type Document = Record<string, unknown>;
type Requirement = { min?: number; max?: number; accepts?: string[]; max_cardinality?: number };
type Requires = Record<string, Requirement>;

/**
 * The one empty state of this tab, and it is a measurement: without a family a
 * deployment can draw, nothing here can say how an answer is shown.
 */
export const NO_FAMILY_TO_WRITE_WITH = {
  title: "No visual family can be chosen yet",
  description:
    "A Chart Template says how a class of answer is shown, so it cannot be written until there is a way to show one. The families come from the same registry the Builder binds against — open this tab again once it answers.",
};

/** The head of the document, never typed twice: the contract says what it is. */
function identity(vocabulary: TemplateVocabulary): Document {
  return {
    spec_contract_version: vocabulary.contract_version,
    schema_version: vocabulary.schema_version,
  };
}

/**
 * The current version's document, reduced to the keys the SERVER declares.
 *
 * A head whose current version does not validate under the shipped contract is
 * still editable — that is half the point of an editor — but nothing it carries
 * outside `document_keys` travels into the successor. Carrying an unknown key
 * forward would resubmit the very thing that made the version unreadable.
 */
function carriedOver(document: Document | null, vocabulary: TemplateVocabulary): Document {
  const known = new Set(vocabulary.document_keys);
  const carried: Document = {};
  for (const [key, value] of Object.entries(document ?? {})) {
    if (known.has(key)) carried[key] = value;
  }
  return carried;
}

function requirementsOf(document: Document): Requires {
  const requires = document.requires;
  return typeof requires === "object" && requires !== null ? ({ ...requires } as Requires) : {};
}

/**
 * The requirements re-read against ONE family.
 *
 * Choosing a family is the question that reduces the next one: a well the family
 * has not is dropped, a count above what the well holds is clamped to it, and a
 * cardinality bound on a well that carries no cardinality risk is removed —
 * every one of them a point `_check_requires_against_family` refuses by name.
 * A well the family REQUIRES is required here too, because a template that does
 * not ask for it materialises into a Spec the ordinary validator refuses.
 */
function reconcile(requires: Requires, family: TemplateFamilyChoice): Requires {
  const next: Requires = {};
  for (const well of family.wells) {
    const previous = requires[well.name];
    if (!well.available) continue;
    if (previous === undefined && !well.required) continue;
    const min = Math.min(Math.max(Number(previous?.min ?? 1) || 1, 1), well.max_members);
    const entry: Requirement = { min };
    const max = Number(previous?.max ?? NaN);
    if (Number.isFinite(max)) entry.max = Math.min(Math.max(max, min), well.max_members);
    const cardinality = Number(previous?.max_cardinality ?? NaN);
    if (Number.isFinite(cardinality) && well.max_cardinality !== null) {
      entry.max_cardinality = Math.min(Math.max(cardinality, 1), well.max_cardinality);
    }
    // Whatever role narrowing the version already carried is kept as it is: it
    // is a predicate a person wrote, and this screen does not silently widen or
    // drop one. A narrowing the family cannot honour is dropped with the well.
    const accepts = previous?.accepts;
    if (Array.isArray(accepts) && accepts.length > 0) {
      const honoured = accepts.filter((role) => well.accepts.includes(role));
      if (honoured.length > 0) entry.accepts = honoured;
    }
    next[well.name] = entry;
  }
  return next;
}

/** `/requires/dimension/max_cardinality` → "Dimension". Never the pointer itself. */
function refusalSubject(pointer: string | null, vocabulary: TemplateVocabulary): string | null {
  if (!pointer) return null;
  const segments = pointer.split("/").filter(Boolean);
  if (segments[0] !== "requires" || segments.length < 2) return null;
  return vocabulary.wells.find((well) => well.name === segments[1])?.label ?? null;
}

function WellRow({
  well,
  requirement,
  familyLabel,
  onChange,
}: {
  well: TemplateFamilyWell;
  requirement: Requirement | undefined;
  familyLabel: string;
  onChange: (next: Requirement | undefined) => void;
}) {
  const required = well.required;
  const on = requirement !== undefined;
  const min = requirement?.min ?? 1;
  const counts = Array.from({ length: well.max_members }, (_, index) => index + 1);

  if (!well.available) {
    return (
      <div className="flex flex-col gap-1 border-b border-border py-3 last:border-b-0">
        <div className="flex items-center gap-2">
          <Checkbox id={`well-${well.name}`} checked={false} disabled />
          <Label htmlFor={`well-${well.name}`}>{well.label}</Label>
        </div>
        <p className="m-0 text-caption text-text-secondary">
          {well.unavailable_reason}
          {well.unavailable_owner ? ` ${well.unavailable_owner} owns it.` : null}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 border-b border-border py-3 last:border-b-0">
      <div className="flex items-center gap-2">
        <Checkbox
          id={`well-${well.name}`}
          checked={on}
          disabled={required}
          onCheckedChange={(value) => onChange(value === true ? { min: 1 } : undefined)}
          data-testid={`chart-template-well-${well.name}`}
        />
        <Label htmlFor={`well-${well.name}`}>{well.label}</Label>
      </div>
      <p className="m-0 text-caption text-text-secondary">
        {required
          ? `The ${familyLabel} family needs a member here, so this template asks for one.`
          : `Optional on the ${familyLabel} family.`}{" "}
        Accepts {well.accepts.join(" or ")}.
      </p>
      {on ? (
        <Cluster>
          <Field label="Members needed">
            {(field) => (
              <NativeSelect
                {...field}
                value={String(min)}
                onChange={(event) => {
                  const value = Number(event.target.value);
                  const next: Requirement = { ...requirement, min: value };
                  if (next.max !== undefined && next.max < value) next.max = value;
                  onChange(next);
                }}
                data-testid={`chart-template-min-${well.name}`}
              >
                {counts.map((count) => (
                  <option key={count} value={count}>
                    {count}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Members allowed at most">
            {(field) => (
              <NativeSelect
                {...field}
                value={requirement?.max === undefined ? "" : String(requirement.max)}
                onChange={(event) => {
                  const next: Requirement = { ...requirement, min };
                  if (event.target.value === "") delete next.max;
                  else next.max = Number(event.target.value);
                  onChange(next);
                }}
                data-testid={`chart-template-max-${well.name}`}
              >
                <option value="">
                  As many as the {familyLabel} family holds ({well.max_members})
                </option>
                {counts
                  .filter((count) => count >= min)
                  .map((count) => (
                    <option key={count} value={count}>
                      {count}
                    </option>
                  ))}
              </NativeSelect>
            )}
          </Field>
          {well.max_cardinality !== null ? (
            <Field
              label="Distinct values at most"
              hint={`The ${familyLabel} family reads at most ${well.max_cardinality} here.`}
            >
              {(field) => (
                <Input
                  {...field}
                  type="number"
                  min={1}
                  max={well.max_cardinality ?? undefined}
                  value={requirement?.max_cardinality ?? ""}
                  onChange={(event) => {
                    const next: Requirement = { ...requirement, min };
                    const value = Number(event.target.value);
                    if (event.target.value === "" || !Number.isFinite(value)) {
                      delete next.max_cardinality;
                    } else {
                      next.max_cardinality = Math.min(
                        Math.max(Math.trunc(value), 1),
                        well.max_cardinality ?? value,
                      );
                    }
                    onChange(next);
                  }}
                  data-testid={`chart-template-cardinality-${well.name}`}
                />
              )}
            </Field>
          ) : null}
        </Cluster>
      ) : null}
    </div>
  );
}

export default function TemplateDocumentEditor({
  projectId,
  template,
  vocabulary,
  onVersionAppended,
}: {
  projectId: string;
  template: ChartTemplateDetail;
  vocabulary: TemplateVocabulary;
  /** The workbench re-reads the template: the header, Overview and Versions all move. */
  onVersionAppended?: (appended: AppendedTemplateVersion) => void;
}) {
  const currentDocument = useMemo<Document>(() => {
    const version = template.versions.find((entry) => entry.id === template.current_version_id);
    return carriedOver((version?.document ?? null) as Document | null, vocabulary);
  }, [template, vocabulary]);

  const [draft, setDraft] = useState<Document>(currentDocument);
  const [saving, setSaving] = useState(false);
  const [appended, setAppended] = useState<AppendedTemplateVersion | null>(null);
  const [refusal, setRefusal] = useState<{
    message: string;
    points: { subject: string | null; remedy: string }[];
  } | null>(null);

  const question = typeof draft.answers_question === "string" ? draft.answers_question : "";
  const familyId = typeof draft.family === "string" ? draft.family : "";
  const family = vocabulary.family_catalogue.find((entry) => entry.id === familyId) ?? null;
  const requires = requirementsOf(draft);

  const chooseFamily = useCallback(
    (id: string) => {
      const chosen = vocabulary.family_catalogue.find((entry) => entry.id === id) ?? null;
      setDraft((previous) => ({
        ...previous,
        family: id,
        requires: chosen ? reconcile(requirementsOf(previous), chosen) : {},
      }));
      setAppended(null);
      setRefusal(null);
    },
    [vocabulary],
  );

  const save = useCallback(async () => {
    if (!family) return;
    setSaving(true);
    setRefusal(null);
    setAppended(null);
    try {
      const document: Document = {
        ...draft,
        ...identity(vocabulary),
        family: family.id,
        answers_question: question,
        requires: reconcile(requires, family),
      };
      const created = await appendChartTemplateVersion(projectId, template.id, document);
      setAppended(created);
      onVersionAppended?.(created);
    } catch (error) {
      const body = error instanceof ApiError ? (error.body as Record<string, unknown> | null) : null;
      const points =
        (body?.refusals as { message: string; subject: string | null; remedy?: string | null }[] | undefined) ?? [];
      setRefusal({
        message: (error as Error).message,
        points: points.map((point) => ({
          subject: refusalSubject(point.subject, vocabulary),
          // The remedy is the sentence a person reads. The fact behind it is
          // second, and the pointer is never shown at all.
          remedy: point.remedy || point.message,
        })),
      });
    } finally {
      setSaving(false);
    }
  }, [draft, family, onVersionAppended, projectId, question, requires, template.id, vocabulary]);

  if (vocabulary.family_catalogue.length === 0) {
    return (
      <EmptyState
        title={NO_FAMILY_TO_WRITE_WITH.title}
        description={NO_FAMILY_TO_WRITE_WITH.description}
      />
    );
  }

  const unmetRequired = family
    ? family.wells.filter((well) => well.required && well.available && !requires[well.name])
    : [];
  const complete = Boolean(question.trim()) && family !== null && unmetRequired.length === 0;

  return (
    <Stack>
      {template.seed_origin !== "project" ? (
        <Status
          as="block"
          tone="warning"
          title="Saving takes this template over"
          data-testid="chart-template-seed-notice"
        >
          It arrived as {template.origin_label.toLowerCase()}. The first version saved here makes it
          owned by this Project, and whoever seeded it keeps no claim on it. The version it came with
          stays exactly as it is — this appends a successor and rewrites nothing.
        </Status>
      ) : null}

      <Panel>
        <PanelHeader
          title="What this template answers"
          description="A Chart Template is a class of answer, not a chart type. Changing what it claims to answer changes what it is, so it is saved as a new version."
        />
        <Field
          label="The question this template answers"
          hint="In the words a person would ask it, never a field name."
          required
        >
          {(field) => (
            <Input
              {...field}
              value={question}
              onChange={(event) => setDraft({ ...draft, answers_question: event.target.value })}
              data-testid="chart-template-question"
            />
          )}
        </Field>
      </Panel>

      {question.trim() ? (
        <Panel>
          <PanelHeader
            title="How it is shown"
            description="The visual family decides which wells this template may ask for, and how much each one holds."
          />
          <Field
            label="Visual family"
            hint={family?.description ?? "Choose the family before saying what a Result must offer."}
            required
          >
            {(field) => (
              <NativeSelect
                {...field}
                value={familyId}
                onChange={(event) => chooseFamily(event.target.value)}
                data-testid="chart-template-family"
              >
                <option value="">Choose a visual family</option>
                {vocabulary.family_catalogue.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.label}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
        </Panel>
      ) : null}

      {family ? (
        <Panel>
          <PanelHeader
            title="What a Result must offer"
            description="A template names a well and a role; it never names a member. Only the wells the chosen family declares are offered, at the counts it holds."
          />
          <div className="flex flex-col">
            {family.wells.map((well) => (
              <WellRow
                key={well.name}
                well={well}
                familyLabel={family.label}
                requirement={requires[well.name]}
                onChange={(next) => {
                  const updated: Requires = { ...requires };
                  if (next === undefined) delete updated[well.name];
                  else updated[well.name] = next;
                  setDraft({ ...draft, requires: updated });
                }}
              />
            ))}
          </div>
        </Panel>
      ) : null}

      {family ? (
        <PresentationRail
          draft={draft}
          registry={{ responsive_profiles: vocabulary.responsive_profiles }}
          onChange={(next) => setDraft(next)}
        />
      ) : null}

      {refusal ? (
        <Status
          as="block"
          tone="error"
          title="This version was not saved"
          data-testid="chart-template-editor-refusal"
        >
          <Stack>
            {refusal.points.length === 0 ? <p className="m-0">{refusal.message}</p> : null}
            {refusal.points.map((point, index) => (
              <p className="m-0" key={index}>
                {point.subject ? `${point.subject}: ` : null}
                {point.remedy}
              </p>
            ))}
          </Stack>
        </Status>
      ) : null}

      {appended ? (
        <Status
          as="block"
          tone="success"
          title="A successor version was saved"
          data-testid="chart-template-editor-saved"
        >
          Version {appended.version_number}, content hash {appended.content_hash.slice(0, 12)}… — it
          succeeds the version it was edited from, which is unchanged.
          {appended.became_project_owned
            ? " This template is now owned by this Project."
            : null}
        </Status>
      ) : null}

      <Cluster>
        <Button onClick={() => void save()} disabled={saving || !complete} data-testid="chart-template-save">
          {saving ? "Saving…" : "Save as a new version"}
        </Button>
        {!complete && question.trim() && family === null ? (
          <span className="text-caption text-text-secondary">
            Choose a visual family to say how this answer is shown.
          </span>
        ) : null}
        {unmetRequired.length > 0 ? (
          <span className="text-caption text-text-secondary">
            Ask for a member in {unmetRequired.map((well) => well.label).join(" and ")}.
          </span>
        ) : null}
      </Cluster>
    </Stack>
  );
}
