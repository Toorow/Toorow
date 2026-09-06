/**
 * The right rail: presentation, responsive profiles and evidence (AC7).
 *
 * Every control here writes ONE declared key of the grammar. There is no free
 * text field, no colour picker and no pixel input, and that is the point: a
 * colour value would be a renderer decision the theme owns, a breakpoint would be
 * CSS, and a free label would be the one door through which markup enters a
 * contract that a share page later renders.
 *
 * The responsive profiles come from the server's registry, which reads them from
 * the single definition site in `core.visualization_specs`. This file does not
 * carry a list of profile names -- two declarations of one enum are two
 * authorities, and the second one drifts.
 */
import { Checkbox, Label, NativeSelect, Panel, PanelHeader } from "../../ui";

export type Draft = Record<string, unknown>;

/**
 * The only thing this rail reads from a registry, so it is the only thing it
 * asks for. `VisualizationRegistry` satisfies it, and so does the Chart
 * Template vocabulary — both carry the profiles from the ONE definition site in
 * `core.visualization_specs`. Asking for the whole registry would have made the
 * Chart Template workbench fetch a second catalogue to reuse one control set,
 * and two rails over one grammar is exactly the drift this file refuses.
 */
export interface ResponsiveProfileSource {
  responsive_profiles: string[];
}

function get<T>(draft: Draft, path: string[], fallback: T): T {
  let node: unknown = draft;
  for (const key of path) {
    if (typeof node !== "object" || node === null) return fallback;
    node = (node as Record<string, unknown>)[key];
  }
  return (node as T) ?? fallback;
}

function set(draft: Draft, path: string[], value: unknown): Draft {
  const [head, ...rest] = path;
  const current = (draft[head] ?? {}) as Record<string, unknown>;
  if (rest.length === 0) return { ...draft, [head]: value };
  return { ...draft, [head]: set(current, rest, value) };
}

function Choice({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{label}</Label>
      <NativeSelect id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </NativeSelect>
    </div>
  );
}

function Toggle({
  id,
  label,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <Checkbox id={id} checked={checked} onCheckedChange={(v) => onChange(v === true)} />
      <Label htmlFor={id}>{label}</Label>
    </div>
  );
}

export default function PresentationRail({
  draft,
  registry,
  onChange,
}: {
  draft: Draft;
  registry: ResponsiveProfileSource;
  onChange: (draft: Draft) => void;
}) {
  const update = (path: string[], value: unknown) => onChange(set(draft, path, value));
  const profiles = get<string[]>(draft, ["responsive", "profiles"], ["console"]);

  return (
    <Panel flush>
      <PanelHeader
        title="Presentation"
        description="Declared intent only. No colour value, no pixel, no code."
      />
      <div className="flex flex-col gap-5 p-5">
        <section aria-labelledby="rail-axes">
          <h3 id="rail-axes" className="m-0 mb-2 text-label font-semibold text-text-secondary">
            Axes and legend
          </h3>
          <div className="flex flex-col gap-3">
            <Choice
              id="axis-y-scale"
              label="Value axis scale"
              value={get(draft, ["axes", "y", "scale"], "linear")}
              options={["linear", "log", "categorical", "ordinal"]}
              onChange={(v) => update(["axes", "y", "scale"], v)}
            />
            <Toggle
              id="axis-y-zero"
              label="Start the value axis at zero"
              checked={get(draft, ["axes", "y", "zero_baseline"], true)}
              onChange={(v) => update(["axes", "y", "zero_baseline"], v)}
            />
            <Choice
              id="legend-position"
              label="Legend position"
              value={get(draft, ["legend", "position"], "right")}
              options={["top", "right", "bottom", "left", "none"]}
              onChange={(v) => update(["legend", "position"], v)}
            />
          </div>
        </section>

        <section aria-labelledby="rail-formatting">
          <h3 id="rail-formatting" className="m-0 mb-2 text-label font-semibold text-text-secondary">
            Formatting
          </h3>
          <div className="flex flex-col gap-3">
            <Choice
              id="number-style"
              label="Number style"
              value={get(draft, ["formatting", "number_style"], "auto")}
              options={["auto", "integer", "decimal", "percent", "currency", "compact"]}
              onChange={(v) => update(["formatting", "number_style"], v)}
            />
            <Choice
              id="date-style"
              label="Date style"
              value={get(draft, ["formatting", "date_style"], "auto")}
              options={["auto", "iso", "short", "long", "month", "year"]}
              onChange={(v) => update(["formatting", "date_style"], v)}
            />
            <Choice
              id="unit-source"
              label="Units come from"
              value={get(draft, ["formatting", "unit_source"], "semantic_view")}
              options={["semantic_view", "none"]}
              onChange={(v) => update(["formatting", "unit_source"], v)}
            />
          </div>
        </section>

        <section aria-labelledby="rail-colour">
          <h3 id="rail-colour" className="m-0 mb-2 text-label font-semibold text-text-secondary">
            Colour
          </h3>
          <div className="flex flex-col gap-3">
            <Choice
              id="colour-role"
              label="Colour role"
              value={get(draft, ["color", "role"], "none")}
              options={["none", "single", "categorical", "sequential", "diverging"]}
              onChange={(v) => update(["color", "role"], v)}
            />
            <Choice
              id="colour-direction"
              label="Semantic direction"
              value={get<string>(draft, ["color", "semantic_direction"], "none")}
              options={["none", "higher_is_better", "lower_is_better"]}
              onChange={(v) => update(["color", "semantic_direction"], v)}
            />
            {/* Status is never colour-only: the direction is stated in words here
                as well as expressed as a hue by the runtime. */}
            <p className="m-0 text-caption text-text-secondary">
              Direction in words:{" "}
              {get<string>(draft, ["color", "semantic_direction"], "none") === "higher_is_better"
                ? "a higher value is better"
                : get<string>(draft, ["color", "semantic_direction"], "none") === "lower_is_better"
                  ? "a lower value is better"
                  : "no direction is claimed"}
              .
            </p>
          </div>
        </section>

        <section aria-labelledby="rail-responsive">
          <h3 id="rail-responsive" className="m-0 mb-2 text-label font-semibold text-text-secondary">
            Responsive profiles
          </h3>
          <div className="flex flex-col gap-2">
            {registry.responsive_profiles.map((profile) => (
              <Toggle
                key={profile}
                id={`profile-${profile}`}
                label={profile}
                checked={profiles.includes(profile)}
                onChange={(checked) =>
                  update(
                    ["responsive", "profiles"],
                    checked
                      ? [...profiles, profile]
                      : profiles.filter((p) => p !== profile),
                  )
                }
              />
            ))}
            <p className="m-0 text-caption text-text-secondary">
              Profiles are named surfaces, never pixel breakpoints: a breakpoint in a saved
              spec would be CSS, which this contract never carries.
            </p>
          </div>
        </section>

        <section aria-labelledby="rail-interactions">
          <h3
            id="rail-interactions"
            className="m-0 mb-2 text-label font-semibold text-text-secondary"
          >
            Interactions
          </h3>
          <div className="flex flex-col gap-2">
            {(["hover", "select", "zoom", "legend_toggle", "local_filter"] as const).map((key) => (
              <Toggle
                key={key}
                id={`interaction-${key}`}
                label={key.replace(/_/g, " ")}
                checked={get(draft, ["interactions", key], key !== "zoom" && key !== "local_filter")}
                onChange={(v) => update(["interactions", key], v)}
              />
            ))}
            <p className="m-0 text-caption text-text-secondary">
              Every interaction is local to the rows the server returned. It hides or selects;
              it never reaggregates, and it never changes the totals or the truncation
              disclosures.
            </p>
          </div>
        </section>

        <section aria-labelledby="rail-evidence">
          <h3 id="rail-evidence" className="m-0 mb-2 text-label font-semibold text-text-secondary">
            Evidence
          </h3>
          <Choice
            id="mark-binding"
            label="A mark resolves to"
            value={get(draft, ["evidence", "mark_binding"], "datum")}
            options={["none", "datum", "series", "category"]}
            onChange={(v) => update(["evidence", "mark_binding"], v)}
          />
          <Choice
            id="summary-source"
            label="Accessible summary comes from"
            value={get(draft, ["accessibility", "summary_source"], "result_manifest")}
            options={["result_manifest", "family_default"]}
            onChange={(v) => update(["accessibility", "summary_source"], v)}
          />
          <p className="m-0 mt-2 text-caption text-text-secondary">
            The accessible table fallback is required on every family and cannot be turned
            off.
          </p>
        </section>
      </div>
    </Panel>
  );
}
