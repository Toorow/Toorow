import {
  Button,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Panel,
  PanelBody,
  PanelHeader,
  Stack,
  Status,
  Textarea,
} from "../ui";
import {
  V2_ASSERTION_TYPES,
  emptyV2AssertionDraft,
  type SelectorDraft,
  type V2AssertionDraft,
  type V2AssertionType,
} from "./goldenQuestionClient";

function toleranceKinds(type: V2AssertionType): Array<V2AssertionDraft["tolerance_kind"]> {
  if (type === "value") return ["exact", "numeric", "temporal"];
  if (type === "row_set") return ["exact", "set"];
  if (type === "cardinality") return ["exact", "numeric"];
  return ["exact"];
}

function SelectorEditor({
  assertionIndex,
  selectors,
  update,
}: {
  assertionIndex: number;
  selectors: SelectorDraft[];
  update: (selectors: SelectorDraft[]) => void;
}) {
  return (
    <Panel flush>
      <PanelHeader
        title="Row selectors"
        description="Optional preconditions over the observed rows. A selector is not an expected value."
        actions={
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={selectors.length >= 8}
            onClick={() => update([...selectors, { field: "", operator: "eq", value_json: "" }])}
          >
            Add selector
          </Button>
        }
      />
      {selectors.length === 0 ? (
        <EmptyState
          title="No selector"
          description="This assertion applies without narrowing the observed rows."
        />
      ) : (
        <PanelBody className="grid gap-4">
          {selectors.map((selector, selectorIndex) => (
            <div
              key={selectorIndex}
              className="grid gap-3 md:grid-cols-[1fr_1fr_2fr_auto] md:items-end"
            >
              <Field label={`Assertion ${assertionIndex + 1} selector ${selectorIndex + 1} field`} required>
                {(field) => (
                  <Input
                    {...field}
                    value={selector.field}
                    onChange={(event) => {
                      const next = [...selectors];
                      next[selectorIndex] = { ...selector, field: event.target.value };
                      update(next);
                    }}
                  />
                )}
              </Field>
              <Field label={`Assertion ${assertionIndex + 1} selector ${selectorIndex + 1} operator`} required>
                {(field) => (
                  <NativeSelect
                    {...field}
                    value={selector.operator}
                    onChange={(event) => {
                      const next = [...selectors];
                      const operator = event.target.value as SelectorDraft["operator"];
                      next[selectorIndex] = {
                        ...selector,
                        operator,
                        value_json: operator === "exists" ? "" : selector.value_json,
                      };
                      update(next);
                    }}
                  >
                    {(["eq", "ne", "in", "exists"] as const).map((operator) => (
                      <option key={operator} value={operator}>{operator}</option>
                    ))}
                  </NativeSelect>
                )}
              </Field>
              <Field
                label={`Assertion ${assertionIndex + 1} selector ${selectorIndex + 1} JSON value`}
                required={selector.operator !== "exists"}
                hint={selector.operator === "exists" ? "Forbidden for the exists operator." : "A JSON scalar, array or object."}
              >
                {(field) => (
                  <Input
                    {...field}
                    value={selector.value_json}
                    disabled={selector.operator === "exists"}
                    onChange={(event) => {
                      const next = [...selectors];
                      next[selectorIndex] = { ...selector, value_json: event.target.value };
                      update(next);
                    }}
                  />
                )}
              </Field>
              <Button
                type="button"
                variant="ghost"
                onClick={() => update(selectors.filter((_, index) => index !== selectorIndex))}
              >
                Remove
              </Button>
            </div>
          ))}
        </PanelBody>
      )}
    </Panel>
  );
}

function ToleranceEditor({
  index,
  assertion,
  update,
}: {
  index: number;
  assertion: V2AssertionDraft;
  update: (patch: Partial<V2AssertionDraft>) => void;
}) {
  const kinds = toleranceKinds(assertion.assertion_type);
  return (
    <div className="grid gap-4 md:grid-cols-3">
      <Field label={`Assertion ${index + 1} tolerance`} required>
        {(field) => (
          <NativeSelect
            {...field}
            value={assertion.tolerance_kind}
            disabled={kinds.length === 1}
            onChange={(event) => update({ tolerance_kind: event.target.value as V2AssertionDraft["tolerance_kind"] })}
          >
            {kinds.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
          </NativeSelect>
        )}
      </Field>
      {assertion.tolerance_kind === "numeric" && (
        <>
          <Field label={`Assertion ${index + 1} numeric mode`} required>
            {(field) => (
              <NativeSelect
                {...field}
                value={assertion.tolerance_mode}
                onChange={(event) => update({ tolerance_mode: event.target.value as "absolute" | "relative" })}
              >
                <option value="absolute">absolute</option>
                <option value="relative">relative</option>
              </NativeSelect>
            )}
          </Field>
          <Field label={`Assertion ${index + 1} numeric amount`} required>
            {(field) => (
              <Input {...field} type="number" min="0" step="any" value={assertion.tolerance_amount} onChange={(event) => update({ tolerance_amount: event.target.value })} />
            )}
          </Field>
        </>
      )}
      {assertion.tolerance_kind === "temporal" && (
        <Field label={`Assertion ${index + 1} temporal seconds`} required>
          {(field) => (
            <Input {...field} type="number" min="0" step="any" value={assertion.tolerance_seconds} onChange={(event) => update({ tolerance_seconds: event.target.value })} />
          )}
        </Field>
      )}
      {assertion.tolerance_kind === "set" && (
        <>
          <Field label={`Assertion ${index + 1} maximum missing rows`} required>
            {(field) => (
              <Input {...field} type="number" min="0" step="1" value={assertion.tolerance_max_missing} onChange={(event) => update({ tolerance_max_missing: event.target.value })} />
            )}
          </Field>
          <Field label={`Assertion ${index + 1} maximum extra rows`} required>
            {(field) => (
              <Input {...field} type="number" min="0" step="1" value={assertion.tolerance_max_extra} onChange={(event) => update({ tolerance_max_extra: event.target.value })} />
            )}
          </Field>
        </>
      )}
    </div>
  );
}

export default function V2ExpectedResultEditor({
  assertions,
  onChange,
}: {
  assertions: V2AssertionDraft[];
  onChange: (assertions: V2AssertionDraft[]) => void;
}) {
  const patch = (index: number, change: Partial<V2AssertionDraft>) => {
    const next = [...assertions];
    next[index] = { ...next[index], ...change };
    onChange(next);
  };

  return (
    <Stack>
      <Panel flush>
        <PanelHeader
          title="Golden Question v2 assertions"
          description="Authored expected truth only. The contested Result is source evidence and is never copied into these fields."
          actions={
            <Button
              type="button"
              variant="secondary"
              disabled={assertions.length >= 32}
              onClick={() => onChange([...assertions, emptyV2AssertionDraft()])}
            >
              Add assertion
            </Button>
          }
        />
        {assertions.length === 0 && (
          <EmptyState
            title="No authored expectation"
            description="Create at least one typed assertion with Add assertion, above. Nothing is inferred from the observed Result."
          />
        )}
      </Panel>

      {assertions.map((assertion, index) => (
        <Panel key={index} flush>
          <PanelHeader
            title={`Assertion ${index + 1}`}
            description="One member of the closed golden-question.v2 assertion union."
            actions={
              <Button type="button" variant="ghost" onClick={() => onChange(assertions.filter((_, candidate) => candidate !== index))}>
                Remove assertion
              </Button>
            }
          />
          <PanelBody className="grid gap-4">
            <Field label={`Assertion ${index + 1} type`} required>
              {(field) => (
                <NativeSelect
                  {...field}
                  value={assertion.assertion_type}
                  onChange={(event) => {
                    const next = [...assertions];
                    next[index] = emptyV2AssertionDraft(event.target.value as V2AssertionType);
                    onChange(next);
                  }}
                >
                  {V2_ASSERTION_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
                </NativeSelect>
              )}
            </Field>

            {assertion.assertion_type === "value" && (
              <div className="grid gap-4 md:grid-cols-2">
                <Field label={`Assertion ${index + 1} field`} required>
                  {(field) => <Input {...field} value={assertion.field} onChange={(event) => patch(index, { field: event.target.value })} />}
                </Field>
                <Field label={`Assertion ${index + 1} expected JSON`} required>
                  {(field) => <Input {...field} value={assertion.expected_json} onChange={(event) => patch(index, { expected_json: event.target.value })} />}
                </Field>
              </div>
            )}
            {assertion.assertion_type === "row_set" && (
              <div className="grid gap-4">
                <Field label={`Assertion ${index + 1} fields`} required hint="Comma-separated, up to 16 fields.">
                  {(field) => <Input {...field} value={assertion.fields} onChange={(event) => patch(index, { fields: event.target.value })} />}
                </Field>
                <Field label={`Assertion ${index + 1} expected rows JSON`} required hint="A JSON array of up to 100 row objects.">
                  {(field) => <Textarea {...field} value={assertion.expected_rows_json} onChange={(event) => patch(index, { expected_rows_json: event.target.value })} />}
                </Field>
              </div>
            )}
            {assertion.assertion_type === "ordering" && (
              <div className="grid gap-4 md:grid-cols-2">
                <Field label={`Assertion ${index + 1} fields`} required hint="Comma-separated, up to 16 fields.">
                  {(field) => <Input {...field} value={assertion.fields} onChange={(event) => patch(index, { fields: event.target.value })} />}
                </Field>
                <Field label={`Assertion ${index + 1} ordering`} required>
                  {(field) => (
                    <NativeSelect {...field} value={assertion.operator} onChange={(event) => patch(index, { operator: event.target.value })}>
                      <option value="ascending">ascending</option>
                      <option value="descending">descending</option>
                    </NativeSelect>
                  )}
                </Field>
              </div>
            )}
            {assertion.assertion_type === "cardinality" && (
              <Field label={`Assertion ${index + 1} expected cardinality`} required>
                {(field) => <Input {...field} type="number" min="0" step="1" value={assertion.expected_json} onChange={(event) => patch(index, { expected_json: event.target.value })} />}
              </Field>
            )}
            {assertion.assertion_type === "invariant" && (
              <div className="grid gap-4 md:grid-cols-2">
                <Field label={`Assertion ${index + 1} fields`} required hint="Comma-separated, up to 16 fields.">
                  {(field) => <Input {...field} value={assertion.fields} onChange={(event) => patch(index, { fields: event.target.value })} />}
                </Field>
                <Field label={`Assertion ${index + 1} invariant`} required>
                  {(field) => (
                    <NativeSelect {...field} value={assertion.operator} onChange={(event) => patch(index, { operator: event.target.value })}>
                      <option value="unique">unique</option>
                      <option value="non_null">non_null</option>
                    </NativeSelect>
                  )}
                </Field>
              </div>
            )}
            {(assertion.assertion_type === "empty" || assertion.assertion_type === "degraded" || assertion.assertion_type === "refused") && (
              <Status tone="neutral">This state assertion has no expected value. Its operator is <code>is</code> and its tolerance is exact.</Status>
            )}

            <ToleranceEditor index={index} assertion={assertion} update={(change) => patch(index, change)} />
          </PanelBody>
          <PanelBody>
            <SelectorEditor
              assertionIndex={index}
              selectors={assertion.selectors}
              update={(selectors) => patch(index, { selectors })}
            />
          </PanelBody>
        </Panel>
      ))}
    </Stack>
  );
}
