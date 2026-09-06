/**
 * Analyze > Reports > Pacing — plan-versus-actual, in the console (Story 67.26).
 *
 * WHAT THIS CLOSES. `analyze-and-test.md:162` has said since Epic 22: "**Console
 * parity is owed and not delivered.** ... no console route reads the pacing
 * Result", and `:510` makes it a criterion — a plan-versus-actual Result
 * "readable in the MCP App and unreachable from the console" leaves Analyze
 * incomplete. The MCP App has carried the whole Result for four epics through
 * the context card `mediaplan_pacing`. This screen is the same Result, in the
 * console.
 *
 * IT COMPUTES NOTHING, and that is the point rather than a limitation.
 * `visualization-and-rendering.md` (*Surface parity*) requires the console to
 * reach the SAME pinned Result as the MCP App — so this screen calls the same
 * `get_card` the MCP tool calls, through its REST mirror, and renders the
 * envelope it returns. Every number here is a number the server returned; the
 * columns, their labels and their order are the server's too. A percentage
 * computed in this file would be the second answer to one question that the
 * `Placements` tab already refuses to become.
 *
 * WHY IT IS A LENS AND NOT A `report` OBJECT. An `app.analysis_reports` row pins
 * one Query Spec version. Pacing has no Query Spec — it is a mart read whose
 * authority is the card. Minting one to make pacing fit the object model would
 * create the second definition `analyze-and-test.md` forbids ("Analyze creates
 * another source of metric definitions"). The lens is the shape the target
 * already ratified for a second reading of a section (story 52.1, Topics).
 *
 * THE CURRENCY IS DRAWN, NEVER ASSUMED (amendment 61.4). Every amount appears
 * under the currency that produced it, and a plan whose budget and spend are in
 * two currencies shows both and no composed figure. The suppression is the
 * mart's and the label is the card's; this file only refuses to hide either.
 *
 * Composed only from `ui/admin/src/ui/index.ts`.
 */
import { useEffect, useMemo, useState } from "react";

import { ApiError, apiGet } from "../lib/apiFetch";
import {
  NO_CARRIER_DESCRIPTION,
  NO_MEDIA_PLAN_DESCRIPTION,
  NO_MEDIA_PLAN_TITLE,
  carrierDoorLabel,
  carrierSentence,
} from "./mediaPlanAbsence";
import {
  Badge,
  Button,
  Cluster,
  EmptyState,
  Failure,
  Retry,
  Field,
  Loading,
  NativeSelect,
  NoScope,
  PageHeader,
  Panel,
  PanelHeader,
  Stack,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  wireWord,
} from "../ui";

/** One plan of the Project, as `GET /api/projects/{id}/mediaplans` returns it. */
export type PlanSummary = {
  id: string;
  name?: string | null;
  currency?: string | null;
};

/**
 * A Datastream of this Project able to carry a media plan — the ADDRESS of the
 * gesture this lens depends on (ratified 2026-08-24).
 *
 * `plan_id` is `null` on a carrier that carries none yet, and that is the whole
 * difference between the two doors: an empty carrier is where a plan is named,
 * a taken one is where its next dated revision is imported. The console does not
 * decide which is which — the server answers it, from the same template
 * declaration the import engine routes on.
 */
export type PlanCarrier = {
  datastream_id: string;
  name: string;
  plan_id?: string | null;
  plan_name?: string | null;
};

type Column = { key: string; label: string; numeric?: boolean };

type TableBlock = {
  type: "table";
  title?: string;
  binding?: { source?: string };
  data?: {
    columns?: Column[];
    rows?: Record<string, unknown>[];
    estimate_columns?: string[];
    plan_only_badge_column?: string;
    note?: string;
  };
};

type CommentBlock = { type: "comment"; data?: { text?: string } };

type Block = TableBlock | CommentBlock | { type: string; data?: unknown };

/** The plan-level currency evidence the card carries since 61.4. */
export type PacingMoney = {
  reporting_currency?: string | null;
  money_policy_version_id?: string | null;
  plan_currency?: string[];
  actual_currency?: string[];
  comparable?: boolean;
  gap_codes?: string[];
  withheld_line_count?: number;
  fx_evidence?: {
    native_currency?: string[];
    as_of_start?: string | null;
    as_of_end?: string | null;
    source?: string[];
    tier?: string[];
    method?: string[];
  };
};

export type PacingEnvelope = {
  data?: {
    plan_name?: string | null;
    plan_version_id?: string | null;
    as_of_day?: string | null;
    composition?: Block[];
    money?: PacingMoney;
    pacing_meta?: {
      pull_ids?: string[];
      estimate_label?: string;
      pace_formula?: string;
      honesty_note?: string;
    };
  };
};

type Phase<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "no-scope" }
  | { status: "empty" }
  | { status: "unavailable"; message: string }
  | { status: "error"; message: string };

/**
 * The plan list has no `unavailable`, and saying so in the type is not pedantry.
 * Plans live in Postgres; only the pacing figures live in the warehouse that can
 * be unreachable. One union covering both would let a reviewer believe an empty
 * plan list might mean "temporarily unknown", which is exactly the confusion
 * between absent and unavailable that AD-9 exists to prevent.
 */
type PlansPhase =
  | { status: "loading" }
  | { status: "ready"; data: PlanSummary[] }
  | { status: "no-scope" }
  | { status: "empty" }
  | { status: "error"; message: string };

/**
 * The gap codes the marts mint, in the words a person can act on.
 *
 * Mirrored from `dbt/macros/money_evidence.sql:73`, which states the same two
 * values and pins them to `server/core/money_derivation.py`. An unknown code is
 * shown verbatim rather than swallowed: a refusal nobody can name is worse than
 * one nobody has translated yet.
 */
const GAP_SENTENCES: Record<string, string> = {
  native_currency_missing:
    "Some spend arrived without the currency it was measured in, so it is not stated at all rather than stated smaller.",
  fx_rate_unavailable:
    "No exchange rate could be resolved for some days, so those days are withheld rather than summed as if they were zero.",
};

function money(block: PacingMoney | undefined): PacingMoney {
  return block ?? {};
}

/** A cell exactly as the server sent it — `null` reads `—`, and never `0`. */
function cell(value: unknown, isEstimate: boolean): React.ReactNode {
  if (value === null || value === undefined || value === "") {
    return <span className="text-text-secondary">—</span>;
  }
  if (typeof value === "boolean") {
    return value ? <Badge tone="neutral">Plan only</Badge> : <span className="text-text-secondary">—</span>;
  }
  const text = String(value);
  if (!isEstimate) return text;
  return (
    <Cluster>
      <span>{text}</span>
      <Badge tone="warning">Estimate</Badge>
    </Cluster>
  );
}

function PacingTable({ block }: { block: TableBlock }) {
  const columns = block.data?.columns ?? [];
  const rows = block.data?.rows ?? [];
  const estimates = new Set(block.data?.estimate_columns ?? []);

  if (rows.length === 0) {
    return (
      <Panel>
        <PanelHeader title={block.title ?? "Table"} />
        <EmptyState
          title="No line to show"
          description="The plan version carries no line for this reading."
        />
      </Panel>
    );
  }

  return (
    <Panel>
      <PanelHeader title={block.title ?? "Table"} />
      <TableScroll label={block.title ?? "Pacing"}>
        <Table>
          <TableHeader>
            <TableRow>
              {columns.map((c) => (
                <TableHead key={c.key}>{c.label}</TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row, i) => (
              <TableRow key={String(row.line_key ?? row.channel ?? i)}>
                {columns.map((c) => (
                  <TableCell key={c.key} data-testid={`pacing-cell-${c.key}`}>
                    {cell(row[c.key], estimates.has(c.key))}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

/**
 * What currency this reading is in — and the refusal when there is no single one.
 *
 * NAMED, NEVER OMITTED. Before 61.4 a plan in USD on a Project reporting in EUR
 * was drawn under a euro sign, composed of a USD budget and a EUR spend, and
 * nothing anywhere compared the two. The composed figures are already NULL in
 * the mart; without this panel they would read as missing data rather than as a
 * deliberate refusal, which is the same lie one step later.
 */
export function MoneyPanel({ money: m }: { money: PacingMoney }) {
  const plan = m.plan_currency ?? [];
  const actual = m.actual_currency ?? [];
  const gaps = m.gap_codes ?? [];
  const withheld = m.withheld_line_count ?? 0;
  const fx = m.fx_evidence ?? {};
  const comparable = m.comparable === true;

  return (
    <Status
      as="block"
      tone={comparable ? "neutral" : "warning"}
      title={
        comparable
          ? "One currency"
          : "The budget and the observed spend are not in the same currency"
      }
      data-testid="pacing-money"
    >
      <Stack>
        <span>
          {comparable
            ? `Every amount below is in ${plan[0] ?? actual[0] ?? "the plan's currency"}.`
            : `Budget in ${plan.join(", ") || "an unstated currency"}; observed spend in ${
                actual.join(", ") || "an unstated currency"
              }. Both are shown, each under its own currency, and no consumed share, pace or remainder is stated — a figure composed of two currencies is not a figure.`}
        </span>
        {m.reporting_currency ? (
          <span className="text-text-secondary">
            This Project reports in {m.reporting_currency}
            {m.money_policy_version_id ? ` (Money Policy ${m.money_policy_version_id})` : ""}.
          </span>
        ) : null}
        {gaps.map((code) => (
          <span key={code} className="text-text-secondary" data-testid={`pacing-gap-${code}`}>
            {GAP_SENTENCES[code] ?? wireWord(code)}
          </span>
        ))}
        {withheld > 0 ? (
          <span className="text-text-secondary" data-testid="pacing-withheld">
            {withheld} line{withheld > 1 ? "s" : ""} state no amount rather than a smaller one.
          </span>
        ) : null}
        {fx.as_of_start || fx.as_of_end ? (
          <span className="text-text-secondary" data-testid="pacing-fx">
            Rates as of {fx.as_of_start ?? "?"} to {fx.as_of_end ?? "?"}
            {fx.source?.length ? `, source ${fx.source.join(", ")}` : ""}
            {fx.tier?.length ? `, tier ${fx.tier.join(", ")}` : ""}
            {fx.method?.length ? `, method ${fx.method.join(", ")}` : ""}. The rate itself is not
            shown for the period: it moves day by day, and one rate printed over a span is a day's
            measurement wearing the span's clothes.
          </span>
        ) : null}
      </Stack>
    </Status>
  );
}

export default function PacingReport({
  projectId,
  onOpenDatastream,
  onAddDatastream,
}: {
  projectId?: string;
  /** The door to a carrier's Workbench. Absent in a host that has no router —
   *  the sentence then stands alone rather than drawing a control that cannot
   *  navigate, which is the dead door this screen was repaired to remove. */
  onOpenDatastream?: (datastreamId: string, tab?: string) => void;
  /** The door to the gesture that MAKES a carrier, when the Project has none. */
  onAddDatastream?: () => void;
}) {
  const [plans, setPlans] = useState<PlansPhase>({ status: "loading" });
  /** Bumped by a failure block's `Retry` (76-4): an error is never a dead end. */
  const [reloadToken, setReloadToken] = useState(0);
  const [carriers, setCarriers] = useState<PlanCarrier[]>([]);
  const [planId, setPlanId] = useState<string>("");
  const [card, setCard] = useState<Phase<PacingEnvelope> | null>(null);

  useEffect(() => {
    if (!projectId) {
      setPlans({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPlans({ status: "loading" });
    apiGet<{ plans?: PlanSummary[]; carriers?: PlanCarrier[] }>(
      `/api/projects/${projectId}/mediaplans`,
      { signal: controller.signal },
    )
      .then((body) => {
        const list = body.plans ?? [];
        setCarriers(body.carriers ?? []);
        setPlans(list.length === 0 ? { status: "empty" } : { status: "ready", data: list });
        setPlanId(list[0]?.id ?? "");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setPlans({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, reloadToken]);

  useEffect(() => {
    if (!projectId || !planId) {
      setCard(null);
      return;
    }
    const controller = new AbortController();
    setCard({ status: "loading" });
    // THE SAME CALL THE MCP APP MAKES. `template` and `plan_id` are the two
    // arguments `get_card` takes for this card; nothing else is passed, because
    // anything else would be this screen shaping the Result.
    apiGet<{ envelope?: PacingEnvelope }>(
      `/api/cards?project_id=${encodeURIComponent(projectId)}&template=mediaplan_pacing&plan_id=${encodeURIComponent(planId)}`,
      { signal: controller.signal },
    )
      .then((body) => setCard({ status: "ready", data: body.envelope ?? {} }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        // 503 IS NOT AN ERROR MESSAGE, it is a state. The warehouse being
        // unreachable must never render as an empty pacing (AD-9), and it must
        // not render as a stack trace either.
        if (error instanceof ApiError && error.status === 503) {
          setCard({
            status: "unavailable",
            message:
              "The warehouse that holds the observed spend is unreachable, so no pacing is shown. Nothing here is zero — it is unknown.",
          });
          return;
        }
        setCard({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, planId, reloadToken]);

  const blocks = card?.status === "ready" ? (card.data.data?.composition ?? []) : [];
  const tables = useMemo(
    () => blocks.filter((b): b is TableBlock => b.type === "table"),
    [blocks],
  );
  const comment = blocks.find((b): b is CommentBlock => b.type === "comment");

  if (plans.status === "no-scope") return <NoScope what="pacing" />;
  if (plans.status === "loading") return <Loading label="media plans" />;
  if (plans.status === "error") {
    return <Failure what="The media plans" message={plans.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  if (plans.status === "empty") {
    /* LA PORTE, ET ELLE MÈNE À UN ÉCRAN QUI EXISTE — arbitrage ratifié le
       2026-08-24 (`analyze-and-test.md`, « the media plan is created and
       imported in the carrier Datastream's Workbench »). L'état vide nommait le
       geste sans adresse, faute d'une décision ; la décision est rendue, donc
       l'adresse est offerte.

       DEUX PORTES, PARCE QU'IL Y A DEUX SITUATIONS, et la seconde n'est pas un
       échec de la première : un Projet qui n'a AUCUN porteur ne se répare pas en
       ouvrant un Workbench, il se répare en créant la source fichier qui portera
       le plan. Offrir la porte du porteur dans ce cas serait exactement la
       porte morte que la story 67.26 a retirée de trois emplacements.

       ET JAMAIS UN CONTRÔLE QUI NE NAVIGUE PAS : sans `onOpenDatastream` (un
       hôte sans routeur), la phrase reste et le bouton n'est pas dessiné. */
    const door = carriers[0];
    return (
      <Stack>
        <PageHeader title="Pacing" description="Plan versus actual, for one media plan." />
        <EmptyState
          title={NO_MEDIA_PLAN_TITLE}
          description={
            door
              ? `Pacing compares a plan's budget with the observed spend, so it needs a plan. ${NO_MEDIA_PLAN_DESCRIPTION} ${carrierSentence(door)}`
              : `Pacing compares a plan's budget with the observed spend, so it needs a plan. ${NO_MEDIA_PLAN_DESCRIPTION} ${NO_CARRIER_DESCRIPTION}`
          }
          action={
            door && onOpenDatastream ? (
              <Button
                data-testid="pacing-open-carrier"
                onClick={() => onOpenDatastream(door.datastream_id, "data")}
              >
                {carrierDoorLabel(door.name)}
              </Button>
            ) : !door && onAddDatastream ? (
              <Button data-testid="pacing-add-carrier" onClick={() => onAddDatastream()}>
                Create a file-source Datastream
              </Button>
            ) : undefined
          }
        />
        {/* PLUSIEURS PORTEURS SE NOMMENT TOUS. Un projet porte un ou plusieurs
            plans, chacun sur son Datastream : n'offrir que le premier ferait
            croire qu'il n'y en a qu'un, ce que la décision interdit. */}
        {carriers.length > 1 && onOpenDatastream ? (
          <Panel>
            <PanelHeader
              title="Datastreams that carry a media plan"
              description="Each plan lives on its own file-source Datastream, with the template that reads that plan's layout."
            />
            <Stack>
              {carriers.slice(1).map((carrier) => (
                <Cluster key={carrier.datastream_id}>
                  <span>{carrierSentence(carrier)}</span>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => onOpenDatastream(carrier.datastream_id, "data")}
                  >
                    {carrierDoorLabel(carrier.name)}
                  </Button>
                </Cluster>
              ))}
            </Stack>
          </Panel>
        ) : null}
      </Stack>
    );
  }

  const selected = plans.data.find((p) => p.id === planId);

  return (
    <Stack>
      <PageHeader
        title="Pacing"
        description="Plan versus actual — the consumed share, the pace against the allocation, the remainder and the extrapolation. The same Result the MCP App carries."
      />

      <Field label="Media plan">
        {(field) => (
          <NativeSelect
            {...field}
            value={planId}
            onChange={(e) => setPlanId(e.currentTarget.value)}
            data-testid="pacing-plan-select"
          >
            {plans.data.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name || p.id}
              </option>
            ))}
          </NativeSelect>
        )}
      </Field>

      {card?.status === "loading" ? <Loading label="pacing" /> : null}
      {card?.status === "error" ? (
        <Failure what="The pacing figures" message={card.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />
      ) : null}
      {card?.status === "unavailable" ? (
        <Status as="block" tone="warning" title="Pacing unavailable" data-testid="pacing-unavailable">
          {card.message}
        </Status>
      ) : null}

      {card?.status === "ready" ? (
        <Stack>
          <MoneyPanel money={money(card.data.data?.money)} />

          {/* WHICH VERSION, AND AS OF WHEN. A pacing figure without its plan
              version and its as-of day is not reproducible, and AD-9 makes the
              citation part of the Result rather than a footnote. */}
          <Status as="block" tone="neutral" title="What this reading is" data-testid="pacing-provenance">
            <Stack>
              <span>
                {selected?.name || planId}
                {card.data.data?.plan_version_id ? ` · version ${card.data.data.plan_version_id}` : ""}
                {card.data.data?.as_of_day ? ` · as of ${card.data.data.as_of_day}` : ""}
              </span>
              {card.data.data?.pacing_meta?.pace_formula ? (
                <span className="text-text-secondary">{card.data.data.pacing_meta.pace_formula}</span>
              ) : null}
              {card.data.data?.pacing_meta?.pull_ids?.length ? (
                <span className="text-text-secondary">
                  Contributing pulls: {card.data.data.pacing_meta.pull_ids.join(", ")}
                </span>
              ) : null}
            </Stack>
          </Status>

          {tables.map((block, i) => (
            <PacingTable key={block.binding?.source ?? i} block={block} />
          ))}

          {comment?.data?.text ? (
            <Panel>
              <PanelHeader title="Comment" />
              <p className="whitespace-pre-wrap" data-testid="pacing-comment">
                {comment.data.text}
              </p>
            </Panel>
          ) : null}
        </Stack>
      ) : null}
    </Stack>
  );
}
