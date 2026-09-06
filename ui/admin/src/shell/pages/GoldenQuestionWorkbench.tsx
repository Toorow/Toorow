/**
 * The Golden Question Workbench — Level 3, five tabs (Story 51.1 AC8).
 *
 * `docs/product-architecture/analyze-and-test.md:290` contracts exactly these
 * five, in this order: Definition, Expected Result, Expected AI Path, Coverage,
 * Versions. They are the tabs declared on the `golden-question` contract in
 * `shell/navigation.ts`, so each one is a real address a person can share.
 *
 * Three things this screen refuses to do:
 *
 *   - Invent a catalogue. Every governed choice — Business Domain versions,
 *     pinnable Semantic View versions, result types, severities, assertion
 *     types, tolerance kinds, provenance link kinds, the AI Path vocabulary —
 *     comes from `/golden-questions/options`. A list maintained here would keep
 *     offering a version the server had already stopped accepting.
 *   - Rewrite a version. Saving creates the NEXT version; the one being read is
 *     immutable and the database refuses to change it.
 *   - Dress an absence as a verdict. Render (50.4/50.5/50.7), evaluated MCP App
 *     behaviour (50.6), Evaluation Runs (51.2) and observed AI Path evidence
 *     (49.6) do not exist. The Coverage tab prints the server's `Unverifiable`
 *     with its reason code and owner story — never `pass`, never `fail`, never a
 *     placeholder that reads like evidence.
 *
 * It composes only from `ui/admin/src/ui/index.ts`: no stylesheet, no hex
 * colour, no literal spacing, no per-screen class prefix
 * (`spec-console-visual-system.md`).
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import { Badge, Button, Cluster, EmptyState, Failure, Field, formatCount, formatNumber, Input, Loading, Metric, NativeSelect, NavTabs, NO_VALUE, ObjectHeader, ObjectId, ObjectNotFound, Panel, PanelHeader, Retry, SectionHeader, Stack, stateLabel, stateTone, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, Textarea, Timestamp, type NavTab, wireWord } from "../../ui";
import { createGoldenQuestionVersion, definitionPayload, draftFromVersion, emptyPathNodeDraft, fetchGoldenQuestion, fetchGoldenQuestionCoverage, fetchGoldenQuestionOptions, refusalsOf, setGoldenQuestionLifecycle, type CreatedVersionReceipt, type DefinitionDraft, type GoldenQuestionCoverage, type GoldenQuestionDetail, type GoldenQuestionOptions, type GoldenQuestionVersion, type ProvenanceState, type Refusal } from "../../test/goldenQuestionClient";
import V2ExpectedResultEditor from "../../test/V2ExpectedResultEditor";
import { buildPath, useRoute } from "../router";

/** The five contracted tabs. The slug is the address; the label is the copy. */
export const GOLDEN_QUESTION_TABS = [
  { key: "definition", label: "Definition" },
  { key: "expected-result", label: "Expected Result" },
  { key: "expected-ai-path", label: "Expected AI Path" },
  { key: "coverage", label: "Coverage" },
  { key: "versions", label: "Versions" },
] as const;

const EDITABLE_TABS = new Set(["definition", "expected-result", "expected-ai-path"]);

type Phase =
  | { status: "loading" }
  | { status: "ready"; detail: GoldenQuestionDetail; options: GoldenQuestionOptions }
  | { status: "not-found" }
  | { status: "error"; message: string };

type CoverageState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; coverage: GoldenQuestionCoverage }
  | { status: "error"; message: string };

function dimensionTone(verdict: string): "neutral" | "success" | "error" {
  // `unverifiable` is NOT amber-as-a-warning and NOT red-as-a-failure: nothing
  // was judged. It takes the neutral mark, which the tone scale renders as an
  // open dotted ring — the one shape that cannot be read as a result.
  if (verdict === "pass") return "success";
  if (verdict === "fail") return "error";
  return "neutral";
}

/** `expected-ai-path` reads as "Expected AI Path" in a sentence. */
function tabLabel(tab: string): string {
  return GOLDEN_QUESTION_TABS.find((candidate) => candidate.key === tab)?.label ?? "Definition";
}

// ---------------------------------------------------------------------------
// The structured refusal, shown exactly as the server wrote it.
// ---------------------------------------------------------------------------

export function RefusalList({
  title,
  message,
  refusals,
}: {
  title: string;
  message: string;
  refusals: Refusal[];
}) {
  return (
    <Status as="block" tone="error" title={title} data-testid="golden-question-refusal">
      <p className="m-0">{message}</p>
      {refusals.length > 0 && (
        <ul className="mt-2 list-disc pl-5">
          {refusals.map((refusal, index) => (
            <li key={`${refusal.code}-${index}`} className="text-ui">
              <code className="text-technical">{refusal.subject ?? refusal.code}</code> —{" "}
              {refusal.message}
            </li>
          ))}
        </ul>
      )}
    </Status>
  );
}

// ---------------------------------------------------------------------------
// Definition.
// ---------------------------------------------------------------------------

/** Exported so the collection's create form edits the SAME document as the
 *  workbench. A second authoring form would be a second idea of the contract. */
export function DefinitionTab({
  draft,
  options,
  update,
}: {
  draft: DefinitionDraft;
  options: GoldenQuestionOptions;
  update: (patch: Partial<DefinitionDraft>) => void;
}) {
  const domain = options.business_domains.find((entry) => entry.id === draft.business_domain_id);
  const versionNumbers = domain
    ? Array.from({ length: domain.latest_version_number }, (_, index) => domain.latest_version_number - index)
    : [];
  const classifications = options.business_classifications.filter(
    (entry) => entry.business_domain_id === draft.business_domain_id,
  );

  return (
    <Stack>
      <Panel flush>
        <PanelHeader
          title="Governed pins"
          description="A Golden Question references governed meaning and authors none of it."
        />
        <div className="grid gap-4 p-5 md:grid-cols-2 xl:grid-cols-3">
          <Field label="Business Domain" required>
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.business_domain_id}
                onChange={(event) =>
                  update({
                    business_domain_id: event.target.value,
                    business_domain_version_number: "",
                    business_classification_id: "",
                  })
                }
              >
                <option value="">Not pinned</option>
                {options.business_domains.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.name} ({entry.id})
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field
            label="Business Domain version"
            required
            hint="An exact version number. A domain without its version is not a pin."
          >
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.business_domain_version_number}
                disabled={!domain}
                onChange={(event) => update({ business_domain_version_number: event.target.value })}
              >
                <option value="">Not pinned</option>
                {versionNumbers.map((number) => (
                  <option key={number} value={String(number)}>
                    v{number}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Classification" hint="Optional narrowing inside the pinned domain.">
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.business_classification_id}
                disabled={!domain}
                onChange={(event) => update({ business_classification_id: event.target.value })}
              >
                <option value="">None</option>
                {classifications.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.name}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field
            label="Semantic View version"
            required
            hint="The view and its version are pinned together; neither travels alone."
          >
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.semantic_view_version_id}
                onChange={(event) => {
                  const chosen = options.semantic_view_versions.find(
                    (entry) => entry.semantic_view_version_id === event.target.value,
                  );
                  update({
                    semantic_view_version_id: chosen?.semantic_view_version_id ?? "",
                    semantic_view_id: chosen?.semantic_view_id ?? "",
                  });
                }}
              >
                <option value="">Not pinned</option>
                {options.semantic_view_versions.map((entry) => (
                  <option key={entry.semantic_view_version_id} value={entry.semantic_view_version_id}>
                    {entry.name} v{entry.version_number} ({entry.status})
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Exercised as" required>
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.semantic_view_version_role}
                onChange={(event) => update({ semantic_view_version_role: event.target.value })}
              >
                <option value="">Not declared</option>
                {options.semantic_view_version_roles.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
        </div>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Question"
          description="The business question, and the time boundary it declares."
        />
        <div className="grid gap-4 p-5">
          <Field label="Business question" required>
            {(field) => (
              <Textarea
                {...field}
                value={draft.question}
                onChange={(event) => update({ question: event.target.value })}
              />
            )}
          </Field>
          <div className="grid gap-4 md:grid-cols-4">
            <Field label="Time boundary" hint="Declare none, an as-of date, or a fixed range.">
              {(field) => (
                <NativeSelect
                  {...field}
                  value={draft.time_boundary_kind}
                  onChange={(event) =>
                    update({ time_boundary_kind: event.target.value as DefinitionDraft["time_boundary_kind"] })
                  }
                >
                  <option value="none">None declared</option>
                  <option value="as_of">As of</option>
                  <option value="range">Fixed range</option>
                </NativeSelect>
              )}
            </Field>
            {draft.time_boundary_kind === "as_of" && (
              <Field label="As of">
                {(field) => (
                  <Input
                    {...field}
                    type="date"
                    value={draft.as_of}
                    onChange={(event) => update({ as_of: event.target.value })}
                  />
                )}
              </Field>
            )}
            {draft.time_boundary_kind === "range" && (
              <>
                <Field label="From">
                  {(field) => (
                    <Input
                      {...field}
                      type="date"
                      value={draft.from}
                      onChange={(event) => update({ from: event.target.value })}
                    />
                  )}
                </Field>
                <Field label="To">
                  {(field) => (
                    <Input
                      {...field}
                      type="date"
                      value={draft.to}
                      onChange={(event) => update({ to: event.target.value })}
                    />
                  )}
                </Field>
              </>
            )}
            {draft.time_boundary_kind !== "none" && (
              <Field label="Timezone">
                {(field) => (
                  <Input
                    {...field}
                    value={draft.timezone}
                    onChange={(event) => update({ timezone: event.target.value })}
                  />
                )}
              </Field>
            )}
          </div>
        </div>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="What a run must be able to say it judged"
          description="Result type, capabilities and severity are stored on the version: changing one changes what correctness means."
        />
        <div className="grid gap-4 p-5 md:grid-cols-3">
          <Field label="Result type" required>
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.result_type}
                onChange={(event) => update({ result_type: event.target.value })}
              >
                <option value="">Not declared</option>
                {options.result_types.map((entry) => (
                  <option key={entry} value={entry}>
                    {entry}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Severity" required>
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.severity}
                onChange={(event) => update({ severity: event.target.value })}
              >
                <option value="">Not declared</option>
                {options.severities.map((entry) => (
                  <option key={entry} value={entry}>
                    {entry}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Capability tags" required hint={options.capability_rule}>
            {(field) => (
              <Input
                {...field}
                value={draft.capability_tags}
                placeholder="comma separated"
                onChange={(event) => update({ capability_tags: event.target.value })}
              />
            )}
          </Field>
        </div>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Reference execution paths"
          description="Zero, one or several. Correctness is the typed assertions; a reference path is an approach, never the definition."
          actions={
            <Button
              variant="secondary"
              onClick={() =>
                update({
                  reference_paths: [
                    ...draft.reference_paths,
                    { query_spec_version_id: "", role: options.reference_path_roles[0] ?? "canonical" },
                  ],
                })
              }
            >
              Add reference path
            </Button>
          }
        />
        {draft.reference_paths.length === 0 ? (
          <EmptyState
            title="No reference execution path"
            description="This is valid: a Golden Question is defined by its typed assertions, not by a reference query. No SQL text is stored anywhere in this object."
          />
        ) : (
          <div className="grid gap-3 p-5">
            {draft.reference_paths.map((path, index) => (
              <div key={index} className="grid gap-3 md:grid-cols-[2fr_1fr_auto] md:items-end">
                <Field label={`Query Spec version ${index + 1}`}>
                  {(field) => (
                    <Input
                      {...field}
                      value={path.query_spec_version_id}
                      onChange={(event) => {
                        const next = [...draft.reference_paths];
                        next[index] = { ...path, query_spec_version_id: event.target.value };
                        update({ reference_paths: next });
                      }}
                    />
                  )}
                </Field>
                <Field label={`Role ${index + 1}`}>
                  {(field) => (
                    <NativeSelect
                      {...field}
                      value={path.role}
                      onChange={(event) => {
                        const next = [...draft.reference_paths];
                        next[index] = { ...path, role: event.target.value };
                        update({ reference_paths: next });
                      }}
                    >
                      {options.reference_path_roles.map((role) => (
                        <option key={role} value={role}>
                          {role}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </Field>
                <Button
                  variant="ghost"
                  onClick={() =>
                    update({ reference_paths: draft.reference_paths.filter((_, i) => i !== index) })
                  }
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
        )}
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Expected Result.
// ---------------------------------------------------------------------------

export function ExpectedResultTab({
  draft,
  options,
  update,
}: {
  draft: DefinitionDraft;
  options: GoldenQuestionOptions;
  update: (patch: Partial<DefinitionDraft>) => void;
}) {
  return (
    <Stack>
      {draft.contract_version === "golden-question.v2" ? (
        <V2ExpectedResultEditor
          assertions={draft.v2_assertions}
          onChange={(v2_assertions) => update({ v2_assertions })}
        />
      ) : (
        <Panel flush>
        <PanelHeader
          title="Typed assertions"
          description="Correctness is a set of typed assertions, each with an explicit tolerance. A declared exact tolerance and an undecided one are not the same statement."
          actions={
            <Button
              variant="secondary"
              onClick={() =>
                update({
                  assertions: [
                    ...draft.assertions,
                    { assertion_type: "", description: "", tolerance_kind: "", tolerance_value: "" },
                  ],
                })
              }
            >
              Add assertion
            </Button>
          }
        />
        {draft.assertions.length === 0 ? (
          <EmptyState
            title="No assertion declared"
            description="A version with an empty assertion array is refused by the server and by the database. Declare at least one with Add assertion, above."
          />
        ) : (
          <div className="grid gap-5 p-5">
            {draft.assertions.map((assertion, index) => (
              <div
                key={index}
                className="grid gap-3 border-b border-divider-base pb-5 last:border-b-0 last:pb-0 md:grid-cols-[1fr_2fr_1fr_1fr_auto] md:items-end"
              >
                <Field label={`Assertion type ${index + 1}`} required>
                  {(field) => (
                    <NativeSelect
                      {...field}
                      value={assertion.assertion_type}
                      onChange={(event) => {
                        const next = [...draft.assertions];
                        next[index] = { ...assertion, assertion_type: event.target.value };
                        update({ assertions: next });
                      }}
                    >
                      <option value="">Not declared</option>
                      {options.assertion_types.map((entry) => (
                        <option key={entry} value={entry}>
                          {entry}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </Field>
                <Field label={`What it asserts ${index + 1}`}>
                  {(field) => (
                    <Input
                      {...field}
                      value={assertion.description}
                      onChange={(event) => {
                        const next = [...draft.assertions];
                        next[index] = { ...assertion, description: event.target.value };
                        update({ assertions: next });
                      }}
                    />
                  )}
                </Field>
                <Field
                  label={`Tolerance ${index + 1}`}
                  required
                  hint="Exact is a decision; leaving it undeclared is not."
                >
                  {(field) => (
                    <NativeSelect
                      {...field}
                      value={assertion.tolerance_kind}
                      onChange={(event) => {
                        const next = [...draft.assertions];
                        next[index] = { ...assertion, tolerance_kind: event.target.value };
                        update({ assertions: next });
                      }}
                    >
                      <option value="">Not declared</option>
                      <option value="exact">exact (no drift accepted)</option>
                      {options.tolerance_kinds.map((entry) => (
                        <option key={entry} value={entry}>
                          {entry}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </Field>
                <Field label={`Tolerance value ${index + 1}`}>
                  {(field) => (
                    <Input
                      {...field}
                      value={assertion.tolerance_value}
                      disabled={assertion.tolerance_kind === "" || assertion.tolerance_kind === "exact"}
                      onChange={(event) => {
                        const next = [...draft.assertions];
                        next[index] = { ...assertion, tolerance_value: event.target.value };
                        update({ assertions: next });
                      }}
                    />
                  )}
                </Field>
                <Button
                  variant="ghost"
                  onClick={() => update({ assertions: draft.assertions.filter((_, i) => i !== index) })}
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
        )}
        </Panel>
      )}

      <Panel flush>
        <PanelHeader
          title="Required provenance"
          description="The links this question requires along its chain, named rather than described. A link being present does not mean it holds; whether it holds is judged elsewhere."
        />
        <TableScroll label="Required provenance links">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Link</TableHead>
                <TableHead>Requirement</TableHead>
                {draft.contract_version === "golden-question.v2" && <TableHead>Expected ref</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {draft.provenance.map((entry, index) => (
                <TableRow key={entry.link_kind}>
                  <TableCell className="font-semibold text-text">{wireWord(entry.link_kind)}</TableCell>
                  <TableCell>
                    <Field label={`Requirement for ${entry.link_kind}`}>
                      {(field) => (
                        <NativeSelect
                          {...field}
                          value={entry.state}
                          onChange={(event) => {
                            const next = [...draft.provenance];
                            next[index] = { ...entry, state: event.target.value as ProvenanceState };
                            update({ provenance: next });
                          }}
                        >
                          <option value="absent">Not declared</option>
                          <option value="required">Required</option>
                          <option value="optional">Declared, not required</option>
                        </NativeSelect>
                      )}
                    </Field>
                  </TableCell>
                  {draft.contract_version === "golden-question.v2" && (
                    <TableCell>
                      <Field label={`Expected ref for ${entry.link_kind}`}>
                        {(field) => (
                          <Input
                            {...field}
                            value={entry.expected_ref}
                            onChange={(event) => {
                              const next = [...draft.provenance];
                              next[index] = { ...entry, expected_ref: event.target.value };
                              update({ provenance: next });
                            }}
                          />
                        )}
                      </Field>
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Expected AI Path.
// ---------------------------------------------------------------------------

export function ExpectedAiPathTab({
  draft,
  options,
  update,
}: {
  draft: DefinitionDraft;
  options: GoldenQuestionOptions;
  update: (patch: Partial<DefinitionDraft>) => void;
}) {
  const nodeIndices = draft.required_nodes.map((_, index) => index);
  return (
    <Stack>
      <Status as="block" tone="neutral" title="This is a pattern, not a trace">
        Every node uses the exact vocabulary the observed record can express, so a comparison is
        possible at all. Nothing on this tab compares the pattern to a run, or judges it: it says what
        the question expects, and no more.
      </Status>

      <Panel flush>
        <PanelHeader
          title="Required nodes"
          description="Named in the vocabulary of the observed AI Path."
          actions={
            <Button
              variant="secondary"
              onClick={() => update({ required_nodes: [...draft.required_nodes, emptyPathNodeDraft()] })}
            >
              Add required node
            </Button>
          }
        />
        {draft.required_nodes.length === 0 ? (
          <EmptyState
            title="No required node"
            description="Valid: this question declares no expected path yet. It is stored as an empty pattern, not as an absent field."
          />
        ) : (
          <div className="grid gap-5 p-5">
            {draft.required_nodes.map((node, index) => (
              <div key={index} className="grid gap-3 border-b border-divider-base pb-5 last:border-b-0 last:pb-0">
                <SectionHeader
                  title={`Node ${index + 1}`}
                  actions={
                    <Button
                      variant="ghost"
                      onClick={() =>
                        update({ required_nodes: draft.required_nodes.filter((_, i) => i !== index) })
                      }
                    >
                      Remove
                    </Button>
                  }
                />
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                  <Field label={`Step kind ${index + 1}`} required>
                    {(field) => (
                      <NativeSelect
                        {...field}
                        value={node.step_kind}
                        onChange={(event) => {
                          const next = [...draft.required_nodes];
                          next[index] = { ...node, step_kind: event.target.value };
                          update({ required_nodes: next });
                        }}
                      >
                        <option value="">Not declared</option>
                        {options.path_step_kinds.map((entry) => (
                          <option key={entry} value={entry}>
                            {entry}
                          </option>
                        ))}
                      </NativeSelect>
                    )}
                  </Field>
                  <Field label={`Owner workspace ${index + 1}`} required>
                    {(field) => (
                      <NativeSelect
                        {...field}
                        value={node.owner_workspace}
                        onChange={(event) => {
                          const next = [...draft.required_nodes];
                          next[index] = { ...node, owner_workspace: event.target.value };
                          update({ required_nodes: next });
                        }}
                      >
                        <option value="">Not declared</option>
                        {options.path_owner_workspaces.map((entry) => (
                          <option key={entry} value={entry}>
                            {entry}
                          </option>
                        ))}
                      </NativeSelect>
                    )}
                  </Field>
                  <Field label={`Tool name ${index + 1}`}>
                    {(field) => (
                      <Input
                        {...field}
                        value={node.tool_name}
                        onChange={(event) => {
                          const next = [...draft.required_nodes];
                          next[index] = { ...node, tool_name: event.target.value };
                          update({ required_nodes: next });
                        }}
                      />
                    )}
                  </Field>
                  <Field label={`Owner object type ${index + 1}`} required>
                    {(field) => (
                      <Input
                        {...field}
                        value={node.owner_object_type}
                        onChange={(event) => {
                          const next = [...draft.required_nodes];
                          next[index] = { ...node, owner_object_type: event.target.value };
                          update({ required_nodes: next });
                        }}
                      />
                    )}
                  </Field>
                  <Field label={`Owner object id ${index + 1}`} required>
                    {(field) => (
                      <Input
                        {...field}
                        value={node.owner_object_id}
                        onChange={(event) => {
                          const next = [...draft.required_nodes];
                          next[index] = { ...node, owner_object_id: event.target.value };
                          update({ required_nodes: next });
                        }}
                      />
                    )}
                  </Field>
                  <Field label={`Owner version id ${index + 1}`} hint="An exact version, never a moving pin.">
                    {(field) => (
                      <Input
                        {...field}
                        value={node.owner_version_id}
                        onChange={(event) => {
                          const next = [...draft.required_nodes];
                          next[index] = { ...node, owner_version_id: event.target.value };
                          update({ required_nodes: next });
                        }}
                      />
                    )}
                  </Field>
                  <Field label={`Skill version id ${index + 1}`} hint="Both or neither, with the step below.">
                    {(field) => (
                      <Input
                        {...field}
                        value={node.skill_version_id}
                        onChange={(event) => {
                          const next = [...draft.required_nodes];
                          next[index] = { ...node, skill_version_id: event.target.value };
                          update({ required_nodes: next });
                        }}
                      />
                    )}
                  </Field>
                  <Field label={`Skill step id ${index + 1}`}>
                    {(field) => (
                      <Input
                        {...field}
                        value={node.skill_step_id}
                        onChange={(event) => {
                          const next = [...draft.required_nodes];
                          next[index] = { ...node, skill_step_id: event.target.value };
                          update({ required_nodes: next });
                        }}
                      />
                    )}
                  </Field>
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Forbidden tools"
          description="A tool this question must never be answered with."
          actions={
            <Button
              variant="secondary"
              onClick={() => update({ forbidden_tools: [...draft.forbidden_tools, ""] })}
            >
              Add forbidden tool
            </Button>
          }
        />
        {draft.forbidden_tools.length === 0 ? (
          <EmptyState title="No forbidden tool" description="Nothing is excluded by this pattern." />
        ) : (
          <div className="grid gap-3 p-5">
            {draft.forbidden_tools.map((tool, index) => (
              <div key={index} className="grid gap-3 md:grid-cols-[1fr_auto] md:items-end">
                <Field label={`Forbidden tool ${index + 1}`}>
                  {(field) => (
                    <Input
                      {...field}
                      value={tool}
                      onChange={(event) => {
                        const next = [...draft.forbidden_tools];
                        next[index] = event.target.value;
                        update({ forbidden_tools: next });
                      }}
                    />
                  )}
                </Field>
                <Button
                  variant="ghost"
                  onClick={() =>
                    update({ forbidden_tools: draft.forbidden_tools.filter((_, i) => i !== index) })
                  }
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Order constraints"
          description="Indices into the required nodes above: a constraint naming a node nobody declared is a rule nothing can check."
          actions={
            <Button
              variant="secondary"
              disabled={draft.required_nodes.length < 2}
              onClick={() =>
                update({ order_constraints: [...draft.order_constraints, { before: "", after: "" }] })
              }
            >
              Add order constraint
            </Button>
          }
        />
        {draft.order_constraints.length === 0 ? (
          <EmptyState
            title="No order constraint"
            description={
              draft.required_nodes.length < 2
                ? "At least two required nodes are needed before one can be ordered against another."
                : "The declared nodes may appear in any order."
            }
          />
        ) : (
          <div className="grid gap-3 p-5">
            {draft.order_constraints.map((constraint, index) => (
              <div key={index} className="grid gap-3 md:grid-cols-[1fr_1fr_auto] md:items-end">
                <Field label={`Before ${index + 1}`}>
                  {(field) => (
                    <NativeSelect
                      {...field}
                      value={constraint.before}
                      onChange={(event) => {
                        const next = [...draft.order_constraints];
                        next[index] = { ...constraint, before: event.target.value };
                        update({ order_constraints: next });
                      }}
                    >
                      <option value="">Not chosen</option>
                      {nodeIndices.map((nodeIndex) => (
                        <option key={nodeIndex} value={String(nodeIndex)}>
                          Node {nodeIndex + 1}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </Field>
                <Field label={`After ${index + 1}`}>
                  {(field) => (
                    <NativeSelect
                      {...field}
                      value={constraint.after}
                      onChange={(event) => {
                        const next = [...draft.order_constraints];
                        next[index] = { ...constraint, after: event.target.value };
                        update({ order_constraints: next });
                      }}
                    >
                      <option value="">Not chosen</option>
                      {nodeIndices.map((nodeIndex) => (
                        <option key={nodeIndex} value={String(nodeIndex)}>
                          Node {nodeIndex + 1}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </Field>
                <Button
                  variant="ghost"
                  onClick={() =>
                    update({ order_constraints: draft.order_constraints.filter((_, i) => i !== index) })
                  }
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Declared alternative paths"
          description="Stored on this version and carried through this screen unchanged. Editing them is not offered here, so an edit elsewhere never drops them."
        />
        {draft.alternatives.length === 0 ? (
          <EmptyState
            title="No alternative path declared"
            description="This question declares one way to be answered."
          />
        ) : (
          <TableScroll label="Declared alternative paths">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Label</TableHead>
                  <TableHead>Required nodes</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {draft.alternatives.flatMap((group, groupIndex) =>
                  (group.branches ?? []).map((branch, branchIndex) => (
                    <TableRow key={`${groupIndex}-${branchIndex}`}>
                      <TableCell>{branch.name}</TableCell>
                      <TableCell>
                        {(branch.required_nodes ?? [])
                          .map((node) => node.tool?.tool_name ?? node.key)
                          .join(" → ") || NO_VALUE}
                      </TableCell>
                    </TableRow>
                  )),
                )}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Coverage — read-only, and honest about every owner that does not exist.
// ---------------------------------------------------------------------------

function CoverageTab({ state, reload }: { state: CoverageState; reload: () => void }) {
  if (state.status === "loading" || state.status === "idle") {
    return (
      <Loading label="coverage" />
    );
  }
  if (state.status === "error") {
    return (
      <Failure
        what="Coverage"
        message={`${state.message}. No coverage figure has been substituted for it.`}
        action={<Retry onClick={reload} />}
      />
    );
  }

  const { pinned, dimensions, semantic_view_version: viewVersion } = state.coverage;
  return (
    <Stack>
      <Panel flush>
        <PanelHeader
          title="What this question pins"
          description="Facts read from the current version, not a score."
        />
        <div className="grid gap-2 p-2 md:grid-cols-2 xl:grid-cols-4">
          {/* 76-5 arbitrage 1: every KPI names the POPULATION it is drawn from.
              These four counted the CURRENT VERSION and said so nowhere; two of
              them put an identifier in the hint, where a person reads a sentence. */}
          <Metric
            label="Business Domain"
            value={pinned.business_domain ? `v${formatNumber(pinned.business_domain.version_number)}` : NO_VALUE}
            hint="pinned by the current version"
          />
          <Metric
            label="Semantic View"
            value={pinned.semantic_view?.role ? wireWord(pinned.semantic_view.role) : NO_VALUE}
            hint={
              viewVersion?.status
                ? `pinned by the current version · the pinned version is ${stateLabel(viewVersion.status).toLowerCase()}`
                : "pinned by the current version · the pinned version sent no status"
            }
          />
          <Metric
            label="Typed assertions"
            value={formatNumber(pinned.expected_assertion_count)}
            hint={`in the current version · ${formatCount(pinned.required_provenance_count, "required provenance link")}`}
          />
          <Metric
            label="Reference paths"
            value={formatNumber(pinned.reference_path_count)}
            hint={`in the current version · ${formatCount(pinned.expected_required_node_count, "expected path node")}`}
          />
        </div>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Dimensions"
          description="A dimension whose owner has not been delivered reports Unverifiable with its reason. It is never a pass and never a fail."
        />
        <TableScroll label="Coverage dimensions">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dimension</TableHead>
                <TableHead>Verdict</TableHead>
                <TableHead>Reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {dimensions.map((dimension) => (
                <TableRow key={dimension.dimension}>
                  <TableCell className="font-semibold text-text">{dimension.dimension}</TableCell>
                  <TableCell>
                    <Status tone={dimensionTone(dimension.verdict)} data-testid={`verdict-${dimension.dimension}`}>
                      {dimension.verdict === "unverifiable" ? "Unverifiable" : dimension.verdict}
                    </Status>
                  </TableCell>
                  <TableCell>
                    <span className="text-technical">{dimension.reason_code}</span>
                    <span className="block text-caption text-text-secondary">{dimension.message}</span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Versions — the immutable history.
// ---------------------------------------------------------------------------

function VersionsTab({
  versions,
  currentVersionId,
}: {
  versions: GoldenQuestionVersion[];
  currentVersionId: string | null;
}) {
  if (versions.length === 0) {
    return (
      <EmptyState
        title="No version"
        description="This Golden Question has no stored version. Nothing has been shown in its place."
      />
    );
  }
  return (
    <Panel flush>
      <PanelHeader
        title="Versions"
        description="Immutable. A revision appends the next version; the database refuses to change a stored one."
      />
      <TableScroll label="Golden Question versions">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Version</TableHead>
              <TableHead>Result type</TableHead>
              <TableHead>Severity</TableHead>
              <TableHead>Content hash</TableHead>
              <TableHead>Predecessor</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Render pin</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {versions.map((version) => (
              <TableRow key={version.id}>
                <TableCell className="font-semibold text-text">
                  v{formatNumber(version.version_number)}
                  {version.id === currentVersionId && (
                    <Badge tone="accent" className="ml-2">
                      current
                    </Badge>
                  )}
                </TableCell>
                <TableCell>{wireWord(version.result_type)}</TableCell>
                <TableCell>{wireWord(version.severity)}</TableCell>
                <TableCell><ObjectId value={version.content_hash} title="Content hash" /></TableCell>
                <TableCell>
                  {/* The first version has no predecessor. That is an absence, and
                      an absence takes the console's one dash -- the parenthetical
                      that explained it was a second vocabulary for `—`. */}
                  {version.predecessor_version_id
                    ? <ObjectId value={version.predecessor_version_id} title="Predecessor version" />
                    : <span data-testid="no-predecessor">{NO_VALUE}</span>}
                </TableCell>
                <TableCell>
                  <Timestamp value={version.created_at} absentMeaning="No creation time recorded" />
                  <span className="block text-caption text-text-secondary">
                    {version.created_by ?? NO_VALUE}
                  </span>
                </TableCell>
                <TableCell>
                  {version.expected_render_ref === null ? (
                    <Status tone="neutral">Unverifiable</Status>
                  ) : (
                    <ObjectId value={version.expected_render_ref} title="Expected Render" />
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// The workbench.
// ---------------------------------------------------------------------------

export default function GoldenQuestionWorkbench({
  projectId,
  goldenQuestionId,
  versionId = null,
  tab,
  onNavigateTab,
}: {
  projectId: string;
  goldenQuestionId: string;
  versionId?: string | null;
  tab: string;
  onNavigateTab: (tab: string) => void;
}) {
  const { route } = useRoute();
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [draft, setDraft] = useState<DefinitionDraft | null>(null);
  const [coverage, setCoverage] = useState<CoverageState>({ status: "idle" });
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<{ message: string; refusals: Refusal[] } | null>(null);
  const [receipt, setReceipt] = useState<CreatedVersionReceipt | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const load = useCallback(
    async (signal: AbortSignal) => {
      const [detail, options] = await Promise.all([
        fetchGoldenQuestion(projectId, goldenQuestionId, { signal }),
        fetchGoldenQuestionOptions(projectId, { signal }),
      ]);
      return { detail, options };
    },
    [projectId, goldenQuestionId],
  );

  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setPhase({ status: "loading" });
    load(controller.signal)
      .then(({ detail, options }) => {
        if (!live) return;
        const selectedVersion = versionId
          ? detail.versions.find((candidate) => candidate.id === versionId) ?? null
          : detail.current_version;
        if (versionId && !selectedVersion) {
          setPhase({
            status: "error",
            message: "The pinned Golden Question version in this address is not available",
          });
          setDraft(null);
          return;
        }
        setPhase({ status: "ready", detail, options });
        setDraft(
          selectedVersion
            ? draftFromVersion(selectedVersion, options.provenance_link_kinds)
            : null,
        );
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        // Foreign, denied and absent share one envelope on purpose. The screen
        // repeats that ambiguity instead of guessing which one it was.
        if (error instanceof ApiError && (error.status === 404 || error.unauthenticated)) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [load, reloadToken, versionId]);

  useEffect(() => {
    if (tab !== "coverage") return;
    const controller = new AbortController();
    let live = true;
    setCoverage({ status: "loading" });
    fetchGoldenQuestionCoverage(projectId, goldenQuestionId, { signal: controller.signal })
      .then((value) => {
        if (live) setCoverage({ status: "ready", coverage: value });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        setCoverage({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, goldenQuestionId, tab, reloadToken]);

  // The tab IS the address, so each one is a real anchor a person can copy or
  // middle-click. When the current address cannot produce one — the workbench
  // mounted outside its object route — the tab renders disabled rather than
  // pointing at something that would open a different screen.
  const tabs = useMemo<NavTab[]>(
    () =>
      GOLDEN_QUESTION_TABS.map((candidate) => {
        let href: string | undefined;
        try {
          href = buildPath({ ...route, tab: candidate.key, action: null, versionId });
        } catch {
          href = undefined;
        }
        return { key: candidate.key, label: candidate.label, href };
      }),
    [route, versionId],
  );

  const update = useCallback((patch: Partial<DefinitionDraft>) => {
    setDraft((current) => (current ? { ...current, ...patch } : current));
  }, []);

  const save = useCallback(async () => {
    if (!draft) return;
    setSaving(true);
    setFailure(null);
    setReceipt(null);
    try {
      const created = await createGoldenQuestionVersion(
        projectId,
        goldenQuestionId,
        definitionPayload(draft),
      );
      setReceipt(created);
      setReloadToken((token) => token + 1);
    } catch (error: unknown) {
      // The server's refusal is shown as it was written. Rewriting it would hide
      // which field was refused, and an author who cannot see that guesses.
      if (error instanceof ApiError) {
        setFailure({ message: error.message, refusals: refusalsOf(error.body) });
      } else {
        setFailure({
          message: error instanceof Error ? error.message : String(error),
          refusals: [],
        });
      }
    } finally {
      setSaving(false);
    }
  }, [draft, projectId, goldenQuestionId]);

  const changeLifecycle = useCallback(
    async (lifecycle: string) => {
      setFailure(null);
      try {
        await setGoldenQuestionLifecycle(projectId, goldenQuestionId, lifecycle);
        setReloadToken((token) => token + 1);
      } catch (error: unknown) {
        if (error instanceof ApiError) {
          setFailure({ message: error.message, refusals: refusalsOf(error.body) });
        } else {
          setFailure({
            message: error instanceof Error ? error.message : String(error),
            refusals: [],
          });
        }
      }
    },
    [projectId, goldenQuestionId],
  );

  if (phase.status === "loading") {
    return (
      <Loading label="this Golden Question" />
    );
  }
  if (phase.status === "not-found") {
    return (
      /* The two answers -- it does not exist, you may not see it -- stay one
         answer on purpose. What the block gained is the way back. */
      <ObjectNotFound
        what="Golden Question"
        collection="Golden Questions"
        collectionHref="#/test/golden-questions"
        detail="No Golden Question with this identifier exists in this Project, or it is not available to you. The two answer identically on purpose, and nothing else has been opened in its place."
      />
    );
  }
  if (phase.status === "error") {
    return (
      <Failure
        what="This Golden Question"
        message={`${phase.message}. Nothing has been shown in its place.`}
        action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
      />
    );
  }

  const { detail, options } = phase;
  const editable = EDITABLE_TABS.has(tab) && !versionId;

  return (
    <Stack data-owner={`test/golden-questions/${goldenQuestionId}`}>
      {/* `DESIGN.md:137`: name + concise source/data-role metadata. The opaque
          id belonged to neither and read as noise beside the question's title. */}
      <ObjectHeader name={detail.title} source="Golden Question" />
      <Cluster>
        {/* The lifecycle was the wire word under a hard-coded neutral, and the
            buttons offered to "Move to deprecated" -- the stored token, in the
            imperative. Both take the declared vocabulary now. */}
        <Badge tone={stateTone(detail.lifecycle)}>{stateLabel(detail.lifecycle)}</Badge>
        <span className="text-caption text-text-secondary">Owner: {detail.owner}</span>
        {detail.lifecycle_transitions.map((next) => (
          <Button key={next} variant="secondary" size="sm" onClick={() => void changeLifecycle(next)}>
            Move to {stateLabel(next)}
          </Button>
        ))}
      </Cluster>
      <NavTabs label="Golden Question" tabs={tabs} current={tab} onNavigate={onNavigateTab} />

      {versionId && (
        <Status as="block" tone="neutral" title="Pinned immutable Golden Question version">
          This address reads version <ObjectId value={versionId} title="Golden Question version" />.
          It cannot be rewritten or silently replaced by the current version.
        </Status>
      )}

      {receipt && (
        <Status as="block" tone="success" title={`Version ${formatNumber(receipt.version_number)} created`}>
          The previous version was not modified. Content hash{" "}
          <ObjectId value={receipt.content_hash} title="Content hash" />.
        </Status>
      )}
      {failure && (
        <RefusalList
          title="The request was refused"
          message={failure.message}
          refusals={failure.refusals}
        />
      )}

      {editable && !draft && (
        <Status as="block" tone="warning" title="This Golden Question has no current version">
          There is nothing to revise. A version is created with the question, so this state means the
          head was written without one.
        </Status>
      )}

      {tab === "definition" && draft && (
        <DefinitionTab draft={draft} options={options} update={update} />
      )}
      {tab === "expected-result" && draft && (
        <ExpectedResultTab draft={draft} options={options} update={update} />
      )}
      {tab === "expected-ai-path" && draft && (
        <ExpectedAiPathTab draft={draft} options={options} update={update} />
      )}
      {tab === "coverage" && (
        <CoverageTab state={coverage} reload={() => setReloadToken((token) => token + 1)} />
      )}
      {tab === "versions" && (
        <VersionsTab versions={detail.versions} currentVersionId={detail.current_version_id} />
      )}

      {editable && draft && (
        <Cluster>
          <Button onClick={() => void save()} disabled={saving} data-testid="golden-question-save">
            {saving ? "Saving…" : "Save as a new version"}
          </Button>
          <span className="text-caption text-text-secondary">
            Saving creates the next immutable version of this Golden Question. The {tabLabel(tab)} tab
            edits the same document as the other two, and one save carries all three.
          </span>
        </Cluster>
      )}
    </Stack>
  );
}
