/**
 * TopicViewsPanel — the third binding family of an Answerable Topic: which
 * Semantic Views it may read, and by which join paths.
 *
 * ── Why it exists ────────────────────────────────────────────────────────────
 * Story 75-5 opened three REST doors (`GET/POST …/{topic_key}/views`,
 * `DELETE …/views/{binding_id}`) and no screen reached them, so a project could
 * declare nothing and an agent went on guessing its joins. Guessing a join is
 * how a fan-out silently multiplies a measure — the failure
 * `semantic_view_version_relationships.fan_out_policy` exists to make unstorable.
 *
 * ── The two questions, and the second is reduced by the first ────────────────
 * 1. WHICH View version — a list, never a typed `svv_…`, read from
 *    `GET …/answerable-topics/semantic-views`: every `published` or `candidate`
 *    version of this project, each carrying the relations it ratified. Only the
 *    two pinnable statuses are offered, because those are exactly the two
 *    `bind_view` accepts; offering a draft would be offering a refusal.
 * 2. WHICH path — composed FROM THE RELATIONS THAT DOOR LISTED for the chosen
 *    version, one leg at a time. After the first leg the list narrows to the
 *    relations that leave where the chain has arrived, so `path_not_chained`
 *    and `duplicate_relation` cannot be composed here at all. A path is never
 *    free text: the server refuses a relation name the pinned version did not
 *    ratify, so asking an operator to remember `campaign_to_account` was asking
 *    them to be refused.
 *
 * ── Two absences, never one ──────────────────────────────────────────────────
 * "I read the list and it is empty" and "I could not read the list" are
 * different facts and render differently, exactly as the queries and knowledge
 * sections beside this one do. Turning a failed read into `[]` would make the
 * panel claim the topic declared nothing.
 *
 * ── A refusal names the gesture ──────────────────────────────────────────────
 * The server refuses by CODE (`version_not_pinnable`, `binding_conflict`, …).
 * A code is a fact about the store; what a reader needs is the move that
 * repairs it, so each one is rendered as its repair and the code never reaches
 * the screen.
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, apiGet, apiJson } from "../lib/apiFetch";
import { Badge, Button, Cluster, Field, NativeSelect, ObjectId, SectionHeader, Stack, Status } from "../ui";

/** One relation of a Semantic View version, as the model holds it today. */
export interface ViewRelation {
  relation_id: string;
  from?: string | null;
  to?: string | null;
  cardinality?: string | null;
  fan_out_policy?: string | null;
}

/** One governed join path, resolved against the pinned version. */
interface ViewPath {
  relation_ids: string[];
  relations?: ViewRelation[];
  resolved?: boolean;
  from?: string | null;
  to?: string | null;
  fan_out_policy?: string | null;
}

/** One declaration: this topic may read this exact View version. */
interface ViewBinding {
  binding_id: string;
  view_id: string;
  view_version_id: string;
  view_version_number?: number | null;
  view_name?: string | null;
  status?: string | null;
  /** True when the pinned version has left the pinnable set. Shown, never
   *  dropped: "this View moved on" is not "this topic declared nothing". */
  stale: boolean;
  paths: ViewPath[];
  note?: string | null;
}

interface ViewsResponse {
  views?: ViewBinding[];
}

/** One View version this topic MAY pin, with the relations it ratified. */
interface ViewChoice {
  view_id: string;
  view_version_id: string;
  view_version_number?: number | null;
  view_name?: string | null;
  status?: string | null;
  relations?: ViewRelation[];
}

interface ChoicesResponse {
  semantic_views?: ViewChoice[];
  truncated?: boolean;
}

/** The declaration in progress: one chosen version, the paths already added,
 *  and the chain being composed leg by leg. */
interface ViewBinderState {
  versionId: string;
  paths: string[][];
  legs: string[];
}

/**
 * A refusal code → the gesture that repairs it.
 *
 * The server's codes are precise and they are ITS vocabulary; a reader needs the
 * move, not the diagnosis. Anything not listed falls back to the server's own
 * sentence, which already names a relation or a version.
 */
const REPAIR: Record<string, string> = {
  version_not_pinnable:
    "That version is a draft, or it has been moved on from. Publish it in Governance › Semantic Model, or pick a published one.",
  unknown_semantic_view_version:
    "That version is not one of this project's Semantic View versions. Pick one from the list.",
  binding_conflict:
    "This topic already reads that View version. Withdraw the existing declaration first, or pick another version.",
  path_not_chained:
    "A path is a walk: each relation must start where the one before it arrived. Compose the chain from the list, which only offers relations that continue it.",
  duplicate_relation:
    "A path crosses each relation once. Remove the repeated leg.",
  ambiguous_relation:
    "That relation name means more than one join in this View version, so no single join can be pinned. Its author renames one in Governance › Semantic Model.",
  relation_not_in_view:
    "That relation belongs to another Semantic View version. Compose the path from the relations listed for the version you picked.",
  unknown_relation:
    "That relation is not one this project ratified. Compose the path from the relations listed for the version you picked.",
  pin_is_not_exact:
    "A declaration names one exact version. Pick one from the list instead of following the newest.",
};

function repairFor(err: unknown): string {
  if (err instanceof ApiError) return REPAIR[err.code] || err.message;
  return err instanceof Error ? err.message : "Request failed";
}

/** `campaigns → accounts → markets`, from the legs the model resolved. */
export function chainLabel(relations: ViewRelation[]): string {
  const nodes: string[] = [];
  for (const relation of relations) {
    const from = relation.from || "?";
    if (nodes.length === 0) nodes.push(from);
    nodes.push(relation.to || "?");
  }
  return nodes.join(" → ");
}

/** Stable per-attempt key: a retried submit must not declare twice. */
function idempotencyKey(action: string, subject: string): string {
  return `topic-${action}-${subject}-${Date.now()}`;
}

export default function TopicViewsPanel({
  projectId,
  topicKey,
  busy,
  setBusy,
}: {
  projectId?: string;
  topicKey: string;
  busy: boolean;
  setBusy: (value: boolean) => void;
}) {
  const [views, setViews] = useState<ViewBinding[] | null>(null);
  const [viewsError, setViewsError] = useState("");
  const [choices, setChoices] = useState<ViewChoice[] | null>(null);
  const [choicesError, setChoicesError] = useState("");
  const [choicesTruncated, setChoicesTruncated] = useState(false);
  const [binder, setBinder] = useState<ViewBinderState | null>(null);
  const [refusal, setRefusal] = useState("");

  const base = projectId
    ? `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(topicKey)}/views`
    : "";

  const loadViews = useCallback(async () => {
    if (!projectId) return;
    setViews(null);
    setViewsError("");
    try {
      const body = await apiGet<ViewsResponse>(
        `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(topicKey)}/views`,
      );
      setViews(Array.isArray(body.views) ? body.views : []);
    } catch (err) {
      // The third state. `[]` here would answer, with a made-up fact, the one
      // question the reader opened this panel for.
      setViews(null);
      setViewsError(err instanceof Error ? err.message : "Request failed");
    }
  }, [projectId, topicKey]);

  useEffect(() => {
    void loadViews();
  }, [loadViews]);

  /** The versions this project may pin. Read when the form opens and not
   *  before: a catalog of nine topics must not spend nine reads on a question
   *  nobody has asked. */
  const loadChoices = useCallback(async () => {
    if (!projectId) return;
    setChoices(null);
    setChoicesError("");
    setChoicesTruncated(false);
    try {
      const body = await apiGet<ChoicesResponse>(
        `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/semantic-views`,
      );
      setChoices(Array.isArray(body.semantic_views) ? body.semantic_views : []);
      setChoicesTruncated(Boolean(body.truncated));
    } catch (err) {
      setChoices(null);
      setChoicesError(err instanceof Error ? err.message : "Request failed");
    }
  }, [projectId]);

  const openBinder = () => {
    setRefusal("");
    setBinder({ versionId: "", paths: [], legs: [] });
    void loadChoices();
  };

  const chosen = (choices || []).find((c) => c.view_version_id === binder?.versionId);
  const relations = chosen?.relations || [];
  const byId = new Map(relations.map((r) => [r.relation_id, r]));
  const legRelations = (binder?.legs || [])
    .map((id) => byId.get(id))
    .filter((r): r is ViewRelation => Boolean(r));

  /**
   * The relations that may continue the chain — THE SECOND QUESTION, REDUCED BY
   * THE FIRST. Before any leg: every relation of the chosen version. After one:
   * only those leaving where the chain has arrived, and never one already
   * crossed. Composed this way, `path_not_chained` and `duplicate_relation`
   * cannot be produced by this screen at all.
   */
  const nextRelations = (): ViewRelation[] => {
    if (!binder) return [];
    const arrivedAt = legRelations.length
      ? legRelations[legRelations.length - 1].to
      : null;
    return relations.filter(
      (r) =>
        !binder.legs.includes(r.relation_id) &&
        (arrivedAt === null || r.from === arrivedAt),
    );
  };

  const declare = async () => {
    if (!projectId || !binder) return;
    setBusy(true);
    setRefusal("");
    try {
      await apiJson(base, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey("view-bind", topicKey),
        },
        body: JSON.stringify({
          semantic_view_version_id: binder.versionId,
          allowed_paths: binder.paths.map((relation_ids) => ({ relation_ids })),
        }),
      });
      setBinder(null);
      await loadViews();
    } catch (err) {
      setRefusal(repairFor(err));
    } finally {
      setBusy(false);
    }
  };

  /** Withdraw a declaration. The Semantic View and its relations are untouched. */
  const withdraw = async (bindingId: string) => {
    if (!projectId) return;
    setBusy(true);
    setRefusal("");
    try {
      await apiJson(`${base}/${encodeURIComponent(bindingId)}`, {
        method: "DELETE",
        headers: { "Idempotency-Key": idempotencyKey("view-unbind", bindingId) },
      });
      await loadViews();
    } catch (err) {
      setRefusal(repairFor(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack className="gap-2" data-testid={`views-${topicKey}`}>
      <SectionHeader title="Semantic Views" />

      {viewsError ? (
        <Status tone="error" data-testid={`views-unavailable-${topicKey}`}>
          Couldn&apos;t read the Semantic Views this topic reads, so this list is not an
          answer. {viewsError}
        </Status>
      ) : views === null ? (
        <p>Loading...</p>
      ) : views.length === 0 ? (
        <p data-testid={`views-empty-${topicKey}`}>
          This topic names no Semantic View, so nothing says which joins it may cross. A
          Semantic View is published in Governance &rsaquo; Semantic Model, then read
          here by its exact version.
        </p>
      ) : (
        <ul data-testid={`views-list-${topicKey}`}>
          {views.map((binding) => (
            <li key={binding.binding_id} data-testid={`view-binding-${binding.binding_id}`}>
              <Badge tone="info">{binding.view_name || binding.view_id}</Badge>{" "}
              <Badge outline>
                v{binding.view_version_number ?? "?"}
                {binding.status ? ` · ${binding.status}` : ""}
              </Badge>{" "}
              {binding.stale ? (
                <Status tone="warning" data-testid={`view-stale-${binding.binding_id}`}>
                  This View has moved on from the version this topic reads. Read a
                  published version instead.
                </Status>
              ) : null}
              <ObjectId value={binding.view_version_id} title="Semantic View version" />
              {binding.paths.length === 0 ? (
                <p data-testid={`view-paths-empty-${binding.binding_id}`}>
                  Reads this View and crosses nothing.
                </p>
              ) : (
                <ul data-testid={`view-paths-${binding.binding_id}`}>
                  {binding.paths.map((path) => (
                    <li key={path.relation_ids.join(">")}>
                      {/* The chain, drawn as the walk it is. The relation names
                          ride beside it: a reader repairs a path by its names,
                          and reads it by its datasets. */}
                      <span>{chainLabel(path.relations || [])}</span>{" "}
                      <Badge outline>{path.relation_ids.join(" › ")}</Badge>{" "}
                      {path.resolved === false ? (
                        <Status tone="warning">
                          One relation of this path is no longer declared by the version
                          this topic reads.
                        </Status>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={() => withdraw(binding.binding_id)}
              >
                Withdraw View
              </Button>
            </li>
          ))}
        </ul>
      )}

      {refusal ? (
        // The GESTURE, never the code. A reader repairs a refusal; they do not
        // diagnose one.
        <Status tone="error" data-testid={`view-refusal-${topicKey}`}>
          {refusal}
        </Status>
      ) : null}

      {binder ? null : (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={busy || !projectId}
          onClick={openBinder}
        >
          Read a Semantic View
        </Button>
      )}

      {binder ? (
        <Stack className="gap-2" data-testid={`view-form-${topicKey}`}>
          {choicesError ? (
            <Status tone="error" data-testid="view-choices-unavailable">
              Couldn&apos;t read this project&apos;s Semantic Views, so no version can be
              chosen. Try again in a moment. {choicesError}
            </Status>
          ) : null}
          {choicesTruncated ? (
            <Status tone="warning" data-testid="view-choices-truncated">
              This is the first page of this project&apos;s Semantic View versions, not all
              of them.
            </Status>
          ) : null}

          {choices === null && !choicesError ? <p>Loading...</p> : null}

          {choices !== null && choices.length === 0 ? (
            <p data-testid="view-choices-empty">
              This project has published no Semantic View yet — one is authored and
              published in Governance &rsaquo; Semantic Model. A topic reads a published or
              candidate version, so there is nothing to name here until then.
            </p>
          ) : null}

          {choices !== null && choices.length > 0 ? (
            <Field
              label="Semantic View version"
              hint="Published in Governance › Semantic Model. Only a published or candidate version can be read: those are the two a topic may name."
            >
              {(p) => (
                <NativeSelect
                  {...p}
                  value={binder.versionId}
                  onChange={(e) =>
                    // The version chooses the RELATIONS. Keeping a half-composed
                    // chain across a change would carry one version's joins into
                    // another's.
                    setBinder({ versionId: e.target.value, paths: [], legs: [] })
                  }
                  data-testid="view-pick"
                >
                  <option value="">Choose a version…</option>
                  {choices.map((choice) => (
                    <option key={choice.view_version_id} value={choice.view_version_id}>
                      {choice.view_name || choice.view_id} — v
                      {choice.view_version_number ?? "?"}, {choice.status}
                    </option>
                  ))}
                </NativeSelect>
              )}
            </Field>
          ) : null}

          {/* THE SECOND QUESTION APPEARS ONCE THE FIRST IS ANSWERED. Before a
              version is chosen there are no relations to compose from, so the
              question does not exist yet. */}
          {binder.versionId ? (
            <Stack className="gap-1" data-testid="view-path-composer">
              <SectionHeader title="Join path" />
              <p data-testid="view-chain">
                {legRelations.length
                  ? chainLabel(legRelations)
                  : "No join yet — this topic would read the View and cross nothing."}
              </p>
              {relations.length === 0 ? (
                <p data-testid="view-relations-empty">
                  This version declares no relationship, so there is no join to allow. It
                  can still be read on its own.
                </p>
              ) : nextRelations().length === 0 ? (
                <p data-testid="view-chain-complete">
                  No relationship of this version continues that chain. Add the path, or
                  remove its last leg.
                </p>
              ) : (
                <Field
                  label="Add a leg"
                  hint="Only the relationships that start where the chain has arrived are offered — a join path is a walk."
                >
                  {(p) => (
                    <NativeSelect
                      {...p}
                      value=""
                      onChange={(e) => {
                        const id = e.target.value;
                        if (!id) return;
                        setBinder({ ...binder, legs: [...binder.legs, id] });
                      }}
                      data-testid="view-leg-pick"
                    >
                      <option value="">Choose a relationship…</option>
                      {nextRelations().map((relation) => (
                        <option key={relation.relation_id} value={relation.relation_id}>
                          {relation.relation_id} — {relation.from} → {relation.to}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </Field>
              )}
              <Cluster>
                {binder.legs.length ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setBinder({ ...binder, legs: binder.legs.slice(0, -1) })
                    }
                  >
                    Remove last leg
                  </Button>
                ) : null}
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={binder.legs.length === 0}
                  onClick={() =>
                    setBinder({
                      ...binder,
                      paths: [...binder.paths, binder.legs],
                      legs: [],
                    })
                  }
                  data-testid="view-add-path"
                >
                  Add this path
                </Button>
              </Cluster>

              {binder.paths.length ? (
                <ul data-testid="view-pending-paths">
                  {binder.paths.map((path, index) => (
                    <li key={path.join(">")}>
                      <span>
                        {chainLabel(
                          path
                            .map((id) => byId.get(id))
                            .filter((r): r is ViewRelation => Boolean(r)),
                        )}
                      </span>{" "}
                      <Badge outline>{path.join(" › ")}</Badge>{" "}
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() =>
                          setBinder({
                            ...binder,
                            paths: binder.paths.filter((_, i) => i !== index),
                          })
                        }
                      >
                        Remove path
                      </Button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </Stack>
          ) : null}

          <Cluster>
            <Button
              type="button"
              variant="ghost"
              disabled={busy}
              onClick={() => setBinder(null)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              // A half-composed chain is not silently sent and not silently
              // dropped: it is added, or it is cleared, and the hint below says
              // which.
              disabled={busy || !binder.versionId || binder.legs.length > 0}
              onClick={declare}
              data-testid="view-declare"
            >
              Declare
            </Button>
          </Cluster>
          {binder.legs.length > 0 ? (
            <p data-testid="view-declare-blocked">
              Add the path being composed, or remove its legs, before declaring.
            </p>
          ) : null}
        </Stack>
      ) : null}
    </Stack>
  );
}
