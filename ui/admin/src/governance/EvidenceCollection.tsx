/**
 * The three Evidence lens compositions, and the server-owned filters above them.
 *
 * NOT a second page shell. `GovernanceCollection` still owns the layout, the
 * lens tabs, the states and the ownership notes; this file supplies the typed
 * columns each lens needs and the controls that change the ADDRESS. Every
 * filter here is a route parameter the server applies — nothing below narrows a
 * list in the browser, because a client-side filter shows a count the server
 * never computed.
 *
 * A generic seven-column table was what the three lenses shared before. It
 * could not show a trace horizon, a version's approval state or an audit
 * outcome, so all three read as the same undifferentiated ledger.
 */
import { useEffect, useState } from "react";
import { Button, formatTimestamp, Input, NativeSelect, stateTone, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, wireWord, type Tone } from "../ui";
import type { AdapterCoverage, GovernanceObject } from "./governanceSurface";

/** The five Evidence lifecycle words, mapped to tone AND kept as text.
 *  Colour is never the only carrier: the word is always rendered. */
export function availabilityTone(value: string): Tone {
  switch (value) {
    case "available":
      return "success";
    case "owner_unavailable":
    case "retained_away":
      return "warning";
    case "quarantined":
      return "error";
    default:
      return "neutral";
  }
}

/** The console's one state vocabulary; every outcome word this screen renders
 *  is in the union, so the seventh local map this function used to be is gone. */
function outcomeTone(value: string | null | undefined): Tone {
  return stateTone((value ?? "").toLowerCase());
}

function moment(value: unknown): string {
  if (typeof value !== "string" || !value) return "No recorded time";
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? "No recorded time" : formatTimestamp(parsed);
}

function summaryOf(item: GovernanceObject): Record<string, unknown> {
  return (item.summary ?? {}) as Record<string, unknown>;
}

function text(value: unknown): string {
  return typeof value === "string" && value ? value : "—";
}

/** Correlations, shown as `kind:id` pairs so a trace id is never mistaken for
 *  an operation id. Bounded, because a node can carry many. */
function correlationLabel(summary: Record<string, unknown>): string {
  const items = Array.isArray(summary.correlations) ? summary.correlations : [];
  if (items.length === 0) return "None";
  return items
    .slice(0, 3)
    .map((raw) => {
      const entry = raw as Record<string, unknown>;
      return `${String(entry.kind)}:${String(entry.id)}`;
    })
    .join(" · ");
}

export const EVIDENCE_COLUMNS: Record<string, string[]> = {
  "lineage-provenance": [
    "Trace",
    "Owner",
    "Horizon",
    "Linked steps",
    "Correlation",
    "Completeness",
    "Availability",
  ],
  "versions-approvals": [
    "Object version",
    "Owner",
    "Version",
    "Recorded",
    "Approval",
    "Diff",
    "Availability",
  ],
  "audit-activity": ["Event", "Occurred", "Actor", "Action", "Outcome", "Target", "Correlation"],
};

/**
 * One row as TEXT, in the column order of its lens.
 *
 * There is one of these and both readers use it: the table below renders these
 * strings, and the export writes them. A second formatter would let a
 * downloaded file say something the screen never said, which is the whole
 * failure mode of an export bolted onto a table.
 */
export function evidenceCells(item: GovernanceObject, lens: string): string[] {
  const summary = summaryOf(item);
  const availability = String(summary.availability ?? "available").replaceAll("_", " ");
  const name = item.object_ref.label;

  if (lens === "lineage-provenance") {
    const linked = Number(summary.linked_record_count ?? 0);
    const references = Number(summary.owner_reference_count ?? 0);
    return [
      name,
      text(summary.owner_workspace),
      summary.horizon_start
        ? `${moment(summary.horizon_start)} → ${moment(summary.horizon_end)}`
        : moment(summary.occurred_at),
      `${linked} linked · ${references} owner refs`,
      correlationLabel(summary),
      String(summary.completeness ?? "partial") === "linked" ? "Linked chain" : "Single node",
      availability,
    ];
  }

  if (lens === "versions-approvals") {
    return [
      name,
      text(summary.owner_object_type),
      text(summary.owner_version_id),
      moment(summary.occurred_at),
      String(summary.approval_state ?? "unapproved") === "approved"
        ? "Approved"
        : "No approval recorded",
      // "No pinned predecessor" is not "identical". Saying which one it is
      // is the whole difference between an honest Diff and an invented one.
      summary.diff_available ? "Predecessor pinned" : "No pinned predecessor",
      availability,
    ];
  }

  return [
    name,
    moment(summary.occurred_at),
    text(summary.actor),
    text(summary.action),
    text(summary.outcome),
    text(summary.governed_target),
    correlationLabel(summary),
  ];
}

export function EvidenceRow({
  item,
  lens,
  nameCell,
}: {
  item: GovernanceObject;
  lens: string;
  nameCell: React.ReactNode;
}) {
  const summary = summaryOf(item);
  const availability = String(summary.availability ?? "available");
  const cells = evidenceCells(item, lens);

  if (lens === "lineage-provenance") {
    return (
      <TableRow>
        <TableCell>{nameCell}</TableCell>
        <TableCell className="text-text-secondary">{cells[1]}</TableCell>
        <TableCell className="text-ui text-text-secondary">{cells[2]}</TableCell>
        <TableCell className="font-numeric text-text-secondary">{cells[3]}</TableCell>
        <TableCell className="text-ui text-text-secondary">{cells[4]}</TableCell>
        <TableCell>
          <Status tone={cells[5] === "Linked chain" ? "success" : "warning"}>{cells[5]}</Status>
        </TableCell>
        <TableCell>
          <Status tone={availabilityTone(availability)}>{cells[6]}</Status>
        </TableCell>
      </TableRow>
    );
  }

  if (lens === "versions-approvals") {
    return (
      <TableRow>
        <TableCell>{nameCell}</TableCell>
        <TableCell className="text-text-secondary">{cells[1]}</TableCell>
        <TableCell className="text-ui text-text-secondary">{cells[2]}</TableCell>
        <TableCell className="text-ui text-text-secondary">{cells[3]}</TableCell>
        <TableCell>
          <Status tone={cells[4] === "Approved" ? "success" : "neutral"}>{cells[4]}</Status>
        </TableCell>
        <TableCell>
          <Status tone={cells[5] === "Predecessor pinned" ? "success" : "neutral"}>{cells[5]}</Status>
        </TableCell>
        <TableCell>
          <Status tone={availabilityTone(availability)}>{cells[6]}</Status>
        </TableCell>
      </TableRow>
    );
  }

  return (
    <TableRow>
      <TableCell>{nameCell}</TableCell>
      <TableCell className="text-ui text-text-secondary">{cells[1]}</TableCell>
      <TableCell className="text-text-secondary">{cells[2]}</TableCell>
      <TableCell className="text-text-secondary">{cells[3]}</TableCell>
      <TableCell>
        <Status tone={outcomeTone(summary.outcome as string | null)}>{cells[4]}</Status>
      </TableCell>
      <TableCell className="text-ui text-text-secondary">{cells[5]}</TableCell>
      <TableCell className="text-ui text-text-secondary">{cells[6]}</TableCell>
    </TableRow>
  );
}

/**
 * WHAT EACH WIRE SLUG IS CALLED, and nothing else.
 *
 * The console owns the WORDS; the server owns the LIST. `evidence_index.py`
 * validates every filter against `OWNER_WORKSPACES:71`, `CORRELATION_KINDS:56`
 * and `LENS_RECORD_KIND:50`, and since 2026-08-17 it declares all three on the
 * envelope (`filter_options`, `governance_read_model.py:_evidence_filter_options`).
 * The menus below are built from that declaration, so a kind added on the
 * server appears here without an edit — and one removed there stops being
 * offered, instead of surviving as a menu entry that answers 400.
 *
 * The maps are keyed by SLUG because the served list is a list of slugs. A slug
 * with no entry is still offered, under its own name: an option a person cannot
 * read is a worse answer than one the product has not named yet, and hiding it
 * would silently narrow the vocabulary the server just declared.
 *
 * `virtual_pull` and `ai_path` in a menu are database words, and a menu of
 * database words is the one thing the console vocabulary rule forbids — so the
 * label is what the option READS, and the slug survives as its title for
 * whoever is debugging a shared address.
 */
const WORKSPACE_LABEL: Record<string, string> = {
  data: "Data",
  governance: "Governance",
  analyze: "Analyze",
  "context-hub": "Context Hub",
  test: "Test",
};

const CORRELATION_KIND_LABEL: Record<string, string> = {
  w3c_trace: "Distributed trace",
  operation: "Operation",
  pull: "Collection Run",
  virtual_pull: "Virtual Run",
  execution: "Datastream execution",
  publication: "Publication",
  result: "Analyze Result",
  render: "Render",
  ai_path: "AI path",
  evaluation_run: "Evaluation run",
  confirmation: "Confirmation",
  audit: "Audit event",
};

const RECORD_KIND_LABEL: Record<string, string> = {
  evidence_trace: "Evidence trace",
  object_version: "Object version",
  audit_event: "Audit event",
};

/**
 * The lists to use when the envelope carries none.
 *
 * FALLBACK, NOT SOURCE. An older envelope — a cached page, a deployment mid-way
 * through — still has to draw a usable bar rather than three empty menus, so
 * the last known vocabulary stands in. It is the ONLY reason these arrays still
 * exist; the served lists win whenever they are present, including when the
 * server declares fewer entries than this file remembers.
 */
const FALLBACK_OWNER_WORKSPACES = ["data", "governance", "analyze", "context-hub", "test"];

const FALLBACK_CORRELATION_KINDS = [
  "w3c_trace",
  "operation",
  "pull",
  "virtual_pull",
  "execution",
  "publication",
  "result",
  "render",
  "ai_path",
  "evaluation_run",
  "confirmation",
  "audit",
];

/**
 * The record kind each lens indexes — `LENS_RECORD_KIND` (`evidence_index.py:50`).
 *
 * A LENS SHOWS EXACTLY ONE KIND, AND THE SERVER REFUSES THE OTHER TWO: passing
 * a `record_kind` that is not this lens's raises `EvidenceFilterInvalid`
 * ("record_kind does not belong to this lens"). So the control offers this
 * lens's kind and nothing else. A three-option menu here would offer two 400s,
 * which is why the parameter has been declared by the route and offered by no
 * control until now.
 */
const FALLBACK_LENS_RECORD_KIND: Record<string, string> = {
  "lineage-provenance": "evidence_trace",
  "versions-approvals": "object_version",
  "audit-activity": "audit_event",
};

/** The vocabulary a lens accepts, as the envelope declares it. */
export interface EvidenceFilterOptions {
  owner_workspaces?: string[];
  correlation_kinds?: string[];
  record_kinds?: string[];
}

/** The served list, or the remembered one — never a merge of the two. Merging
 *  would keep offering a value the server has just stopped accepting.
 *
 *  An EMPTY list reads as "this envelope declares nothing", not as "this lens
 *  narrows by nothing": the index validates against three non-empty tuples, so
 *  an empty one on the wire is an envelope that predates the declaration, and
 *  answering it with three empty menus would take away the only controls that
 *  can undo the narrowing a person is looking at. */
function served(list: string[] | undefined, fallback: string[]): string[] {
  return list && list.length > 0 ? list : fallback;
}

/** A value safe in a file name, with its own length bound. */
function slug(value: string): string {
  return value.replace(/[^A-Za-z0-9._-]+/g, "-").slice(0, 40);
}

/** RFC 4180: quote a field that carries a separator, a quote or a newline, and
 *  double the quotes inside it. */
function csvField(value: string): string {
  return /[",\r\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
}

/** The rows ON SCREEN, with the headers above them. Nothing is fetched, nothing
 *  is recomputed, and no row the reader cannot see is added. */
export function evidenceCsv(lens: string, items: GovernanceObject[]): string {
  const rows = [EVIDENCE_COLUMNS[lens] ?? [], ...items.map((item) => evidenceCells(item, lens))];
  return rows.map((row) => row.map(csvField).join(",")).join("\r\n");
}

/** A name that carries what the file actually holds: the lens, every filter
 *  that produced it, and the row count — so two exports of the same lens under
 *  two narrowings never land on one another in a downloads folder. */
export function evidenceExportName(
  lens: string,
  filters: Record<string, string>,
  count: number,
): string {
  const parts = ["evidence", lens];
  for (const key of Object.keys(filters).sort()) {
    if (key === "cursor" || !filters[key]) continue;
    parts.push(`${key}-${slug(filters[key])}`);
  }
  if (filters.cursor) parts.push("later-page");
  parts.push(`${count}-rows`);
  return `${parts.join("_")}.csv`;
}

/**
 * The page on screen, as a file.
 *
 * NO NEW ROUTE, AND NO PROMISE OF COMPLETENESS. The server pages this list and
 * this button asks it for nothing: what it writes is exactly the rows rendered
 * above it, which is why it counts them in its own label rather than saying
 * "Export". An export that silently held a page of a bigger answer would be
 * read as the answer.
 */
function ExportPage({
  lens,
  filters,
  items,
}: {
  lens: string;
  filters: Record<string, string>;
  items: GovernanceObject[];
}) {
  const download = () => {
    const blob = new Blob([evidenceCsv(lens, items)], { type: "text/csv;charset=utf-8" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = evidenceExportName(lens, filters, items.length);
    anchor.click();
    URL.revokeObjectURL(href);
  };

  return (
    <Button
      variant="secondary"
      disabled={items.length === 0}
      data-testid="evidence-export"
      title="Exactly the rows on this page, with the columns above them. The rest of the lens is paged by the server and is not asked for here."
      onClick={download}
    >
      Export this page ({items.length} row{items.length === 1 ? "" : "s"})
    </Button>
  );
}


/** A text filter that commits on Enter or blur.
 *
 *  Its draft lives here and nowhere else: the ROUTE still holds the committed
 *  value, so the address and the rows stay in agreement. A draft in the route
 *  would put half-typed words in the location bar and in the server's log. */
function CommittedInput({
  label: fieldLabel,
  value,
  placeholder,
  onCommit,
}: {
  label: string;
  value: string;
  placeholder?: string;
  onCommit: (next: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => {
    if (draft.trim() !== value) onCommit(draft.trim());
  };
  return (
    <label className="flex flex-col gap-1 text-caption text-text-secondary">
      <span>{fieldLabel}</span>
      <Input
        value={draft}
        placeholder={placeholder}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          }
        }}
      />
    </label>
  );
}

/**
 * The filter bar. Every control writes the ROUTE, and the route is what the
 * server reads back — so the address in the location bar and the rows on screen
 * cannot disagree, and the link is shareable by construction.
 */
export function EvidenceFilters({
  lens,
  filters,
  items = [],
  options,
  onChange,
}: {
  lens: string;
  filters: Record<string, string>;
  /** The rows currently rendered under this bar. Used by the export and by
   *  nothing else: no control here narrows them in the browser. */
  items?: GovernanceObject[];
  /** The vocabulary this lens accepts, straight off the envelope. Absent only
   *  on an envelope minted before the server declared it. */
  options?: EvidenceFilterOptions;
  onChange: (next: Record<string, string>) => void;
}) {
  const workspaces = served(options?.owner_workspaces, FALLBACK_OWNER_WORKSPACES);
  const correlationKinds = served(options?.correlation_kinds, FALLBACK_CORRELATION_KINDS);
  // One kind per lens, and the server is the one that knows which: a lens it
  // has not declared offers no record-kind control at all, rather than a
  // narrowing this file guessed.
  const recordKind = options?.record_kinds?.[0] ?? FALLBACK_LENS_RECORD_KIND[lens];
  const set = (key: string, value: string) => {
    const next = { ...filters };
    if (value) next[key] = value;
    else delete next[key];
    // Any filter change invalidates the cursor it was minted against. Dropping
    // it here is what stops the server from refusing the request outright.
    delete next.cursor;
    if (key === "correlation_kind" && !value) delete next.correlation_id;
    onChange(next);
  };

  const active = Object.keys(filters).filter((key) => key !== "cursor" && filters[key]);

  return (
    <div className="flex flex-wrap items-end gap-4" data-testid="evidence-filters">
      {/* A free-text filter commits on Enter or on blur, never per keystroke.
          Navigating on every character remounts the field, so the caret is lost
          after the first letter and the server is asked six questions nobody
          meant to ask. A select or a date has one discrete value, so it commits
          immediately. */}
      <label className="flex flex-col gap-1 text-caption text-text-secondary">
        <span>From</span>
        <Input type="date" value={filters.from ?? ""} onChange={(event) => set("from", event.target.value)} />
      </label>
      <label className="flex flex-col gap-1 text-caption text-text-secondary">
        <span>To</span>
        <Input type="date" value={filters.to ?? ""} onChange={(event) => set("to", event.target.value)} />
      </label>

      {lens !== "audit-activity" && (
        <label className="flex flex-col gap-1 text-caption text-text-secondary">
          <span>Owner workspace</span>
          <NativeSelect
            value={filters.owner_workspace ?? ""}
            onChange={(event) => set("owner_workspace", event.target.value)}
          >
            <option value="">Any workspace</option>
            {workspaces.map((value) => (
              <option key={value} value={value} title={value}>
                {WORKSPACE_LABEL[value] ?? wireWord(value)}
              </option>
            ))}
          </NativeSelect>
        </label>
      )}

      {/* Declared by the route (`vocabulary.ts`, EVIDENCE_QUERY) and offered by
          nothing until now. It carries ONE value per lens, because the server
          refuses any other — so this pins what the lens already shows into the
          address, and never proposes a narrowing it would refuse. */}
      {recordKind && (
        <label className="flex flex-col gap-1 text-caption text-text-secondary">
          <span>Record kind</span>
          <NativeSelect
            value={filters.record_kind ?? ""}
            onChange={(event) => set("record_kind", event.target.value)}
          >
            <option value="">Every record of this lens</option>
            <option value={recordKind} title={recordKind}>
              {RECORD_KIND_LABEL[recordKind] ?? recordKind} only
            </option>
          </NativeSelect>
        </label>
      )}

      {lens === "audit-activity" && (
        <CommittedInput
          label="Outcome"
          value={filters.outcome ?? ""}
          placeholder="success"
          onCommit={(next) => set("outcome", next)}
        />
      )}

      <label className="flex flex-col gap-1 text-caption text-text-secondary">
        <span>Correlation kind</span>
        <NativeSelect
          value={filters.correlation_kind ?? ""}
          onChange={(event) => set("correlation_kind", event.target.value)}
        >
          <option value="">Any correlation</option>
          {correlationKinds.map((value) => (
            <option key={value} value={value} title={value}>
              {CORRELATION_KIND_LABEL[value] ?? wireWord(value)}
            </option>
          ))}
        </NativeSelect>
      </label>

      {filters.correlation_kind && (
        <CommittedInput
          label="Correlation id"
          value={filters.correlation_id ?? ""}
          onCommit={(next) => set("correlation_id", next)}
        />
      )}

      {active.length > 0 && (
        <Button variant="secondary" onClick={() => onChange({})}>
          Clear {active.length} filter{active.length === 1 ? "" : "s"}
        </Button>
      )}

      <ExportPage lens={lens} filters={filters} items={items} />
    </div>
  );
}

/**
 * What each adapter could and could not prove, named.
 *
 * A future owner appears here as UNAVAILABLE with the story that owns it. That
 * is deliberately not hidden: a Render that nobody indexed and a Render that
 * does not exist look identical through an empty list, and only one of them is
 * a measurement.
 */
export function EvidenceCoverage({ adapters }: { adapters: AdapterCoverage[] }) {
  if (adapters.length === 0) return null;
  return (
    <TableScroll label="Evidence adapter coverage">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Producer</TableHead>
            <TableHead>Owner</TableHead>
            <TableHead>State</TableHead>
            <TableHead>Indexed</TableHead>
            <TableHead>Why</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {adapters.map((adapter) => (
            <TableRow key={adapter.producer}>
              <TableCell className="font-semibold text-text">{adapter.label}</TableCell>
              <TableCell className="text-text-secondary">{adapter.owner_workspace}</TableCell>
              <TableCell>
                <Status
                  tone={
                    adapter.state === "idle"
                      ? "success"
                      : adapter.state === "unavailable" || adapter.state === "failed"
                        ? "warning"
                        : "neutral"
                  }
                >
                  {adapter.state.replaceAll("_", " ")}
                </Status>
              </TableCell>
              <TableCell className="font-numeric text-text-secondary">
                {adapter.state === "unavailable" || adapter.state === "excluded"
                  ? "—"
                  : (adapter.indexed_count ?? 0)}
              </TableCell>
              <TableCell className="text-ui text-text-secondary">
                {adapter.reason_code ?? adapter.failure_class ?? "—"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}
