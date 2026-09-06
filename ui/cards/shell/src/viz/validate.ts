/**
 * Story 50.5 AC2/AC3 -- the runtime validates its envelope BEFORE it touches a
 * renderer, and refuses anything else.
 *
 * The refusals are structured and each one NAMES the offending field, because a
 * message that says "invalid input" makes the reader guess and guessing is how a
 * fixture ends up presented as data.
 *
 * THE TWO VERSION KEYS DO DIFFERENT JOBS, and confusing them is a real bug this
 * validator is written to make impossible:
 *   - `spec_contract_version` is the STRING literal "visualization-spec.v1". It
 *     anchors the database CHECK (`server/core/visualization_specs.py:78`,
 *     mirroring `query_specs.py:43`) and is asserted for EQUALITY.
 *   - `schema_version` is an INTEGER. It is the only key a range comparison may
 *     touch. Comparing a range against the string literal is a type error dressed
 *     as a check -- it would pass on any string and prove nothing.
 *
 * AC3 IN ONE SENTENCE: a raw ECharts `option`, a D3 selection instruction, a
 * function, a string of HTML/CSS, a URL or an event handler is refused wherever
 * it arrives from -- Console, MCP App, share page, connector seed, LLM proposal
 * or test fixture -- and is never persisted.
 */

import {
  DISPLAY_KEYS,
  type DisplayState,
  type RenderInput,
  type VizRefusal,
  type VizSpecDocument,
} from "./contracts";
import {
  isResponsiveProfile,
  KNOWN_UNSUPPORTED_PROFILES,
  RESPONSIVE_PROFILES,
} from "./responsive";
import { isPlaceholderPin } from "./buildInfo";
import type { RendererDeclaration } from "./registry";

export const SPEC_CONTRACT_VERSION = "visualization-spec.v1";

/** The integer range THIS runtime supports. Integers, never a string, never "latest". */
export const SUPPORTED_SCHEMA_VERSIONS = { min: 1, max: 1 } as const;

const PIN_FIELDS = [
  "theme_version",
  "formatter_version",
  "renderer_build",
  "runtime_build",
] as const;

/**
 * Keys and value shapes that mean "someone handed the runtime a renderer
 * configuration". Refused at the top level of the envelope AND inside the spec
 * document, because the second is the door a reader would not think to check.
 */
const FORBIDDEN_KEYS = [
  "option",
  "options",
  "series_data",
  "seriesData",
  "echarts",
  "chartOptions",
  "d3",
  "selection_instruction",
  "html",
  "innerHTML",
  "css",
  "style",
  "styles",
  "script",
  "url",
  "src",
  "href",
  "onClick",
  "onclick",
  "onEvent",
  "handler",
  "formatter",
  "renderItem",
  "tooltipFormatter",
];

const URL_LIKE = /^\s*(https?:|data:|javascript:|blob:|\/\/)/i;
const MARKUP_LIKE = /<\s*(script|iframe|img|svg|style|link|object|embed)\b/i;

function refuse(code: string, field: string, message: string): VizRefusal {
  return { code, field, message };
}

function scanForRawConfiguration(
  value: unknown,
  pointer: string,
  refusals: VizRefusal[],
  depth = 0,
): void {
  if (depth > 8 || value === null || value === undefined) return;

  if (typeof value === "function") {
    refusals.push(
      refuse(
        "executable_value",
        pointer,
        `\`${pointer}\` is a function. The runtime accepts data, never behaviour: a ` +
          `formatter, an event handler or a render callback supplied from outside would ` +
          `be a second presentation authority.`,
      ),
    );
    return;
  }
  if (typeof value === "string") {
    if (URL_LIKE.test(value)) {
      refusals.push(
        refuse(
          "url_value",
          pointer,
          `\`${pointer}\` carries a URL. A renderer never loads anything: every asset is ` +
            `bundled and every value comes from the Result.`,
        ),
      );
    } else if (MARKUP_LIKE.test(value)) {
      refusals.push(
        refuse(
          "markup_value",
          pointer,
          `\`${pointer}\` carries markup. Labels and titles are text; the runtime never ` +
            `renders supplied HTML.`,
        ),
      );
    }
    return;
  }
  if (Array.isArray(value)) {
    for (let i = 0; i < Math.min(value.length, 200); i += 1) {
      scanForRawConfiguration(value[i], `${pointer}[${i}]`, refusals, depth + 1);
    }
    return;
  }
  if (typeof value === "object") {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      if (FORBIDDEN_KEYS.includes(key)) {
        refusals.push(
          refuse(
            "raw_renderer_configuration",
            `${pointer}.${key}`,
            `\`${pointer}.${key}\` is renderer configuration. The compiler is the only ` +
              `producer of a chart configuration; nothing may supply, forward or persist ` +
              `one.`,
          ),
        );
        continue;
      }
      scanForRawConfiguration(child, `${pointer}.${key}`, refusals, depth + 1);
    }
  }
}

function validateDisplay(
  display: DisplayState | undefined,
  renderer: RendererDeclaration | null,
  refusals: VizRefusal[],
): void {
  if (display === undefined) return;
  if (typeof display !== "object" || display === null || Array.isArray(display)) {
    refusals.push(
      refuse("invalid_display", "display", "`display` is an object of bounded local state."),
    );
    return;
  }
  for (const key of Object.keys(display)) {
    if (!(DISPLAY_KEYS as readonly string[]).includes(key)) {
      refusals.push(
        refuse(
          "undeclared_display_key",
          `display.${key}`,
          `\`display.${key}\` is not a display key any renderer declared. Declared keys ` +
            `are ${DISPLAY_KEYS.join(", ")}.` +
            (renderer ? ` The selected renderer is "${renderer.renderer_id}".` : ""),
        ),
      );
    }
  }
}

/**
 * Every member id a spec document references, with the pointer that referenced it.
 * Used to prove the spec cannot name a field the Result projection does not carry.
 */
function referencedMembers(document: VizSpecDocument): { member: string; pointer: string }[] {
  const out: { member: string; pointer: string }[] = [];
  for (const [well, members] of Object.entries(document.bindings ?? {})) {
    if (!Array.isArray(members)) continue;
    members.forEach((member, i) => {
      if (typeof member === "string" && member !== "") {
        out.push({ member, pointer: `spec.document.bindings.${well}[${i}]` });
      }
    });
  }
  (document.thresholds ?? []).forEach((t, i) => {
    if (t && typeof t.member_id === "string") {
      out.push({ member: t.member_id, pointer: `spec.document.thresholds[${i}].member_id` });
    }
  });
  (document.reference_lines ?? []).forEach((r, i) => {
    if (r && typeof r.member_id === "string") {
      out.push({ member: r.member_id, pointer: `spec.document.reference_lines[${i}].member_id` });
    }
  });
  (document.evidence?.datum_fields ?? []).forEach((m, i) => {
    if (typeof m === "string" && m !== "") {
      out.push({ member: m, pointer: `spec.document.evidence.datum_fields[${i}]` });
    }
  });
  return out;
}

export interface ValidationOutcome {
  refusals: VizRefusal[];
}

/**
 * Validate the envelope. `renderer` is the declaration `resolveRenderer` returned,
 * or null when the family was refused before a renderer existed -- the display-key
 * check still runs against the closed key set in that case.
 */
export function validateRenderInput(
  input: unknown,
  renderer: RendererDeclaration | null,
): ValidationOutcome {
  const refusals: VizRefusal[] = [];

  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    return {
      refusals: [
        refuse(
          "invalid_envelope",
          "input",
          "The runtime takes exactly one envelope object: { result, spec, pins, profile, display }.",
        ),
      ],
    };
  }
  const envelope = input as Partial<RenderInput> & Record<string, unknown>;

  // --- AC3: nothing that looks like renderer configuration, anywhere. --------
  scanForRawConfiguration(envelope.spec, "spec", refusals);
  for (const key of Object.keys(envelope)) {
    if (!["result", "spec", "pins", "profile", "display"].includes(key)) {
      refusals.push(
        refuse(
          "unknown_field",
          key,
          `\`${key}\` is not a field of the runtime envelope. The five accepted fields are ` +
            `result, spec, pins, profile and display.`,
        ),
      );
    }
  }

  // --- result ---------------------------------------------------------------
  const result = envelope.result;
  if (!result || typeof result !== "object") {
    refusals.push(
      refuse("missing_result", "result", "`result` is required: the runtime renders a Result, " +
        "never a fixture and never a shape no server returns."),
    );
  } else {
    if (typeof result.result_id !== "string" || result.result_id === "") {
      refusals.push(refuse("missing_result_identity", "result.result_id", "`result.result_id` is required."));
    }
    if (typeof result.content_hash !== "string" || result.content_hash === "") {
      refusals.push(
        refuse(
          "missing_result_identity",
          "result.content_hash",
          "`result.content_hash` is required: the datum keys are derived from it, so a Render " +
            "without it could not resolve a mark back to the immutable Result.",
        ),
      );
    }
    if (!Array.isArray(result.rows)) {
      refusals.push(refuse("invalid_result", "result.rows", "`result.rows` is an array of rows."));
    }
  }

  // --- spec -----------------------------------------------------------------
  const spec = envelope.spec;
  if (!spec || typeof spec !== "object") {
    refusals.push(refuse("missing_spec", "spec", "`spec` is required: one immutable Visualization Spec version."));
  } else {
    if (typeof spec.visualization_spec_version_id !== "string" || spec.visualization_spec_version_id === "") {
      refusals.push(
        refuse(
          "missing_spec_identity",
          "spec.visualization_spec_version_id",
          "`spec.visualization_spec_version_id` is required: a Render pins the exact version.",
        ),
      );
    }
    // EQUALITY against the string literal.
    if (spec.spec_contract_version !== SPEC_CONTRACT_VERSION) {
      refusals.push(
        refuse(
          "spec_contract_version_mismatch",
          "spec.spec_contract_version",
          `\`spec.spec_contract_version\` must be exactly "${SPEC_CONTRACT_VERSION}"; received ` +
            `${JSON.stringify(spec.spec_contract_version)}.`,
        ),
      );
    }
    // RANGE against the integer, and only the integer.
    const schemaVersion = spec.schema_version;
    if (typeof schemaVersion !== "number" || !Number.isInteger(schemaVersion)) {
      refusals.push(
        refuse(
          "schema_version_not_integer",
          "spec.schema_version",
          "`spec.schema_version` is an integer. The string contract version is a separate key " +
            "and is compared for equality, never ranged over.",
        ),
      );
    } else if (
      schemaVersion < SUPPORTED_SCHEMA_VERSIONS.min ||
      schemaVersion > SUPPORTED_SCHEMA_VERSIONS.max
    ) {
      refusals.push(
        refuse(
          "schema_version_unsupported",
          "spec.schema_version",
          `\`spec.schema_version\` is ${schemaVersion}; this runtime supports ` +
            `${SUPPORTED_SCHEMA_VERSIONS.min}..${SUPPORTED_SCHEMA_VERSIONS.max}.`,
        ),
      );
    }
    const document = spec.document;
    if (!document || typeof document !== "object") {
      refusals.push(refuse("missing_spec_document", "spec.document", "`spec.document` is required."));
    } else if (result && typeof result === "object" && Array.isArray(result.rows)) {
      // Every referenced field must exist in the Result projection.
      const declared = Array.isArray(result.schema?.fields)
        ? result.schema!.fields!.flatMap((field) => [
            field?.name,
            field?.id,
          ])
        : result.rows.length > 0
          ? Object.keys(result.rows[0]!)
          : [];
      const present = new Set(declared.filter((n): n is string => typeof n === "string"));
      for (const { member, pointer } of referencedMembers(document as VizSpecDocument)) {
        if (!present.has(member)) {
          refusals.push(
            refuse(
              "field_absent_from_result",
              pointer,
              `\`${pointer}\` references "${member}", which the Result projection does not ` +
                `carry. Its fields are: ${[...present].join(", ") || "(none -- the Result " +
                  "returned no rows)"}.`,
            ),
          );
        }
      }
    }
  }

  // --- pins -----------------------------------------------------------------
  const pins = envelope.pins;
  if (!pins || typeof pins !== "object") {
    refusals.push(
      refuse("missing_pins", "pins", "`pins` is required: four non-null build identities."),
    );
  } else {
    for (const field of PIN_FIELDS) {
      const value = (pins as unknown as Record<string, unknown>)[field];
      if (typeof value !== "string" || value.trim() === "") {
        refusals.push(
          refuse(
            "missing_pin",
            `pins.${field}`,
            `\`pins.${field}\` is required and must be a non-empty string. A Render replays ` +
              `through the identity it pinned; an absent pin makes replay a guess.`,
          ),
        );
        continue;
      }
      if (isPlaceholderPin(value)) {
        refusals.push(
          refuse(
            "placeholder_pin",
            `pins.${field}`,
            `\`pins.${field}\` is "${value}", which is a placeholder, not an identity. Name the ` +
              `exact build.`,
          ),
        );
      }
    }
    if (renderer) {
      const declared = (pins as unknown as Record<string, unknown>).renderer_build;
      if (typeof declared === "string" && declared !== "" && declared !== renderer.build) {
        refusals.push(
          refuse(
            "unknown_renderer_build",
            "pins.renderer_build",
            `\`pins.renderer_build\` is "${declared}", which is not a build this runtime knows. ` +
              `The renderer for this family in this build is "${renderer.build}".`,
          ),
        );
      }
    }
  }

  // --- profile --------------------------------------------------------------
  // A KNOWN-but-unsupported profile is declared vocabulary, not an error: AC9
  // requires a clean fallback WITH the substitution stated, so it is resolved by
  // `resolveProfile`, never refused here. That map is empty since `mcp-pip` was
  // built (2026-08-24), and the sentence below says so rather than trailing an
  // empty list after a colon -- a refusal that reads "or a declared
  // known-unsupported profile: " tells the reader nothing and looks like a bug.
  const knownUnsupported = Object.keys(KNOWN_UNSUPPORTED_PROFILES);
  const profileIsKnown =
    isResponsiveProfile(envelope.profile) ||
    (typeof envelope.profile === "string" && envelope.profile in KNOWN_UNSUPPORTED_PROFILES);
  if (!profileIsKnown) {
    refusals.push(
      refuse(
        "invalid_profile",
        "profile",
        `\`profile\` must be one of ${RESPONSIVE_PROFILES.join(", ")}`
          + (knownUnsupported.length > 0
            ? ` (or a declared known-unsupported profile: ${knownUnsupported.join(", ")})`
            : "") +
          `; received ${JSON.stringify(envelope.profile)}. The profile is supplied by the ` +
          `caller and is never inferred from the viewport.`,
      ),
    );
  }

  validateDisplay(envelope.display as DisplayState | undefined, renderer, refusals);

  return { refusals };
}
