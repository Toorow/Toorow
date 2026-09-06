/**
 * Naming a dimension in the client's own words -- the console door.
 *
 * WHY THIS PANEL EXISTS. `governance.md:1950` ratifies TWO doors onto one
 * state: *"the console (`POST /api/dimension-lineage/labels`) and the model
 * (`set_dimension_label` / `read_dimension_labels`)"*. The model door has been
 * armed and measured; the console door shipped its three routes -- GET, POST
 * and DELETE, `core/dimension_lineage_api.py:392-401` -- and no screen anywhere
 * called them. Measured 2026-08-24: `grep -rn "dimension_labels|dimension-labels"
 * ui/admin/src` returned nothing. The lineage tab beside it already printed
 * "No client name: the stable identifier is standing in" and sent nobody
 * anywhere -- a screen that states a gap and does not carry the gesture that
 * closes it is unfinished.
 *
 * WHAT A PERSON COMES HERE TO DO. A canonical dimension has two names at once:
 * the stable identifier the product joins on (`audience_language`), and the
 * client's own word for it (`Langue`). The identifier never reaches a person;
 * the label never reaches a join. Naming it here is what stops
 * `audience_language` appearing in a chart, a legend, an axis, a narrative
 * sentence or a model answer -- the first clause of the "A client label reaches
 * every surface that shows the number" list.
 *
 * ONE QUESTION AT A TIME, AND THE FIRST REDUCES THE SECOND. Where the name
 * applies is asked first, because the answer decides what the word starts as:
 * choosing the scope that already carries a name pre-fills that name and turns
 * the gesture into a rename; choosing a scope that carries none pre-fills the
 * proposal DERIVED from the identifier. The word field does not exist until the
 * scope is answered.
 *
 * THE PROPOSAL IS DERIVED, NEVER DECIDED. `audience_language` proposes
 * "Audience language" -- the underscore is the line `governance.md:2357` draws
 * between an identifier the product built for joining and a word a person
 * reads. It arrives pre-filled and overridable, and it is never stored until
 * somebody presses the button: a proposal is not a decision, exactly as the
 * `fr-FR` -> `fr` rollup ships as a proposal and not as a conformance.
 *
 * PLATFORM IS NOT ON OFFER. `governance.md:1956` -- platform labels ship with
 * the product seeds and are writable by nobody, through either door. So the
 * scope is never offered, and when the name in force IS the platform one the
 * panel says what overriding it means rather than showing a button that would
 * be refused.
 *
 * THE READING IS THE PARENT'S. `display_label`, `label_scope` and
 * `label_source` arrive in the fed-by envelope the tab already reads. A second
 * read of the same cascade in the same screen is a second answer free to
 * disagree with the one printed above it.
 *
 * AND THE CASCADE IS NOT THE ONLY QUESTION (2026-08-31). It says what a READER
 * sees; a person here is about to WRITE at one exact scope, and the word already
 * stored THERE is what the field must start from. The panel served only the
 * winning label, so the moment a project overrode an organization, choosing
 * "your whole organization" pre-filled the DERIVED proposal over that
 * organization's own chosen word — and pressing the button would have renamed it
 * to a proposal, which `governance.md` calls out by name. So the envelope now
 * carries `scope_labels`, each scope's own word, and the field starts from the
 * word of the scope BEING EDITED.
 */
import { useCallback, useState } from "react";
import {
  Button,
  ChoiceGroup,
  ConfirmDialog,
  Field,
  Input,
  ObjectId,
  Panel,
  PanelHeader,
  Stack,
  Status,
} from "../ui";
import { ApiError, apiDelete, apiPost } from "../lib/apiFetch";

/** The scope, said the way a person holds it -- never the stored token. */
const SCOPE_SENTENCE: Record<string, string> = {
  PROJECT: "this project",
  ORG: "your organization",
  PLATFORM: "the whole platform",
};

/** The same scope, as the SUBJECT of a sentence. */
const SCOPE_SUBJECT: Record<string, string> = {
  PROJECT: "This project",
  ORG: "Your organization",
  PLATFORM: "The platform",
};

/**
 * The word this identifier PROPOSES -- derived from the shape of an identifier
 * and nothing more: the underscores that made it joinable become spaces, and
 * the sentence starts with a capital. It is never stored on its own; a person
 * confirms it or replaces it.
 */
export function proposedLabel(identifier: string): string {
  const words = identifier.replace(/[_-]+/g, " ").trim();
  if (!words) return "";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A refusal, said as the gesture that repairs it -- never the server's cause. */
function refusalSentence(err: unknown, fallback: string): string {
  if (!(err instanceof ApiError)) return fallback;
  if (err.status === 403) {
    return (
      "Naming a dimension is an owner's or an admin's gesture in this " +
      "organization. Ask one of them to name it."
    );
  }
  if (err.status === 404) {
    return (
      "This project could not be reached from your account. Ask to be invited " +
      "into the organization that holds it."
    );
  }
  if (err.code === "invalid_scope" || err.code === "invalid_param") {
    return "That scope was refused. Name it for this project, or for your organization.";
  }
  if (err.code === "missing_param") {
    return "A name and a scope are both needed. Choose where it applies and type the word.";
  }
  return fallback;
}

export default function DimensionLabelPanel({
  canonicalDimension,
  projectId,
  organizationId,
  displayLabel,
  labelScope,
  labelSource,
  scopeLabels,
  onChanged,
}: {
  /** The stable identifier. This panel never changes it. */
  canonicalDimension: string;
  projectId: string;
  /** Absent on an address naming no organization: the ORG scope is then not offered. */
  organizationId?: string | null;
  /** The label in force, resolved by the cascade PROJECT > ORG > PLATFORM. */
  displayLabel: string | null;
  /** The scope the label in force comes from. */
  labelScope: string | null;
  /** `client` when somebody named it; anything else means the identifier stands in. */
  labelSource: string;
  /**
   * The word EACH scope stores in its own right, `null` where it stores none.
   * Same envelope, same read as the three fields above — it is not a second
   * answer to the cascade, it is the answer to a different question.
   */
  scopeLabels?: Record<string, { display_label: string | null } | null> | null;
  /** Re-read the cascade. The reading lives in the parent, so it reloads it. */
  onChanged: () => void | Promise<void>;
}) {
  const scopeInForce = (labelScope ?? "").toUpperCase();
  const named = labelSource === "client" && Boolean(displayLabel);
  const canNameForOrg = Boolean(organizationId);

  const [chosenScope, setChosenScope] = useState("");
  const [word, setWord] = useState("");
  const [busy, setBusy] = useState(false);
  const [broken, setBroken] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  /**
   * The word a scope already carries, or `null`. It is the SCOPE BEING EDITED
   * that is asked — never the one that happens to win the cascade — because
   * naming is a write and a write lands at one scope.
   */
  const wordAt = useCallback(
    (scope: string): string | null => {
      const stored = scopeLabels?.[scope]?.display_label;
      if (stored) return stored;
      // An envelope with no per-scope answer still knows the winner, and for
      // THAT scope the two questions coincide.
      if (!scopeLabels && scope === scopeInForce && named) return displayLabel;
      return null;
    },
    [displayLabel, named, scopeInForce, scopeLabels],
  );

  // Answering the first question is what makes the second one exist, and what
  // decides where it starts: the scope that already carries a name starts from
  // THAT name — the gesture is then a rename — and a scope that carries none
  // starts from the proposal derived from the identifier.
  const chooseScope = useCallback(
    (value: string) => {
      setChosenScope(value);
      setWord(wordAt(value) ?? proposedLabel(canonicalDimension));
      setBroken(null);
    },
    [canonicalDimension, wordAt],
  );

  const save = useCallback(async () => {
    const trimmed = word.trim();
    if (!chosenScope || !trimmed) return;
    setBusy(true);
    try {
      await apiPost("/api/dimension-lineage/labels", {
        scope_level: chosenScope,
        canonical_dimension: canonicalDimension,
        display_label: trimmed,
        ...(chosenScope === "ORG"
          ? { org_id: organizationId }
          : { project_id: projectId }),
      });
      setChosenScope("");
      setWord("");
      setBroken(null);
      await onChanged();
    } catch (err) {
      setBroken(refusalSentence(err, "That name could not be saved."));
    } finally {
      setBusy(false);
    }
  }, [canonicalDimension, chosenScope, onChanged, organizationId, projectId, word]);

  const restore = useCallback(async () => {
    setBusy(true);
    try {
      const params = new URLSearchParams({ scope_level: scopeInForce });
      if (scopeInForce === "ORG") params.set("org_id", organizationId ?? "");
      if (scopeInForce === "PROJECT") params.set("project_id", projectId);
      await apiDelete(
        `/api/dimension-lineage/labels/${encodeURIComponent(canonicalDimension)}` +
          `?${params.toString()}`,
      );
      setConfirming(false);
      setBroken(null);
      await onChanged();
    } catch (err) {
      setBroken(refusalSentence(err, "That name could not be taken back."));
    } finally {
      setBusy(false);
    }
  }, [canonicalDimension, onChanged, organizationId, projectId, scopeInForce]);

  const choices = [
    {
      value: "PROJECT",
      label: "This project",
      hint: "Only the people working in this project read it.",
    },
    ...(canNameForOrg
      ? [
          {
            value: "ORG",
            label: "Your whole organization",
            hint: "Every project of the organization reads it, unless one of them names it differently.",
          },
        ]
      : []),
  ];

  // Whether the person is naming, renaming, or overriding a less specific
  // scope: the same control, three different sentences. RENAMING is decided by
  // the scope BEING EDITED, not by the one in force -- an organization that
  // already carries a word is being renamed even while a project overrides it.
  const wordHere = chosenScope === "" ? null : wordAt(chosenScope);
  const renaming = wordHere !== null;
  const overriding =
    chosenScope !== "" && wordHere === null && chosenScope !== scopeInForce && named;

  return (
    <Panel data-testid="dimension-label">
      <PanelHeader
        title="What do you call this dimension?"
        description={
          "Every chart, legend, axis, narrative sentence and model answer built " +
          "on this dimension shows this word. The identifier the product joins " +
          "on is never touched."
        }
      />

      {broken && (
        <Status
          as="block"
          tone="error"
          title="That did not go through"
          data-testid="dimension-label-refusal"
        >
          {broken}
        </Status>
      )}

      {named ? (
        <Status
          as="block"
          tone="success"
          title={`Called "${displayLabel}"`}
          data-testid="dimension-label-named"
        >
          Named for {SCOPE_SENTENCE[scopeInForce] ?? "an unstated scope"}. The
          identifier the product joins on is{" "}
          <ObjectId value={canonicalDimension} /> and it has not moved.
        </Status>
      ) : (
        <Status
          as="block"
          tone="warning"
          title="Nobody has named this dimension"
          data-testid="dimension-label-unnamed"
        >
          Everyone reading a number broken down by it sees{" "}
          <ObjectId value={canonicalDimension} /> -- the word the product joins
          on, not a word your team uses. Name it below and it travels to every
          chart, narrative and model answer at once.
        </Status>
      )}

      <Stack className="gap-4">
        <Field
          label="Where does this name apply?"
          hint={
            canNameForOrg
              ? "A project name wins over an organization name for that project."
              : "This address names no organization, so only this project can be named from here."
          }
        >
          {({ id }) => (
            <ChoiceGroup
              id={id}
              aria-label="Where does this name apply?"
              variant="card"
              choices={choices}
              value={chosenScope}
              onValueChange={chooseScope}
            />
          )}
        </Field>

        {/* The second question exists only once the first is answered. */}
        {chosenScope !== "" && (
          <Stack className="gap-3" data-testid="dimension-label-word">
            <Field
              label="The word your team uses"
              hint={
                renaming
                  ? `This is the name ${SCOPE_SENTENCE[chosenScope] ?? "this scope"} already carries. Change it to rename it.`
                  : overriding
                    ? `${SCOPE_SUBJECT[scopeInForce] ?? "Another scope"} calls it "${displayLabel}". A name here overrides that, for ${SCOPE_SENTENCE[chosenScope]} only.`
                    : "Proposed from the identifier. Replace it with the word your team actually uses."
              }
            >
              {({ id, "aria-describedby": describedBy }) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  value={word}
                  onChange={(event) => setWord(event.target.value)}
                  data-testid="dimension-label-input"
                />
              )}
            </Field>
            <div>
              <Button
                disabled={busy || word.trim() === ""}
                onClick={() => void save()}
                data-testid="dimension-label-save"
              >
                {renaming ? "Rename it" : "Name it"}
              </Button>
            </div>
          </Stack>
        )}

        {/* Taking a name back is only offered for a scope this door may write.
            A platform name is the product's: there is nothing to take back
            here, only something to override. */}
        {named && (scopeInForce === "ORG" || scopeInForce === "PROJECT") && (
          <div>
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => setConfirming(true)}
              data-testid="dimension-label-restore"
            >
              Take this name back
            </Button>
          </div>
        )}
        {named && scopeInForce === "PLATFORM" && (
          <Status
            as="block"
            tone="neutral"
            title="This name ships with the product"
            data-testid="dimension-label-platform"
          >
            The platform name is changed by a release, never from here. Naming it
            for this project or for your organization overrides it wherever you
            read it.
          </Status>
        )}
      </Stack>

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Take this name back?"
        description={
          `"${displayLabel}" stops being read for ` +
          `${SCOPE_SENTENCE[scopeInForce] ?? "this scope"}. Whatever the less ` +
          `specific scope carries becomes visible again, and where nothing does, ` +
          `readers see ${canonicalDimension}. You can name it again at any time.`
        }
        confirmLabel="Take it back"
        cancelLabel="Keep the name"
        destructive
        busy={busy}
        onConfirm={() => void restore()}
        data-testid="dimension-label-restore-confirm"
        confirmTestId="dimension-label-restore-confirm-yes"
      />
    </Panel>
  );
}
