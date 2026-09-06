/**
 * Values waiting to be mapped — S1 and S2 of `unresolved-values.md`.
 *
 * ONE COMPONENT FOR BOTH SURFACES, and that is an acceptance criterion rather
 * than a convenience. The target refuses a product where "the Workbench and the
 * Governance lens answer with two different numbers for the same Project"
 * (:473) and where "the same count is answered differently by the `Map` tab and
 * the `Value Tables` lens" (:482). Two components reading one route would still
 * be two places free to round, filter or fold differently, so there is one
 * component and one route behind it; `scope` only decides which address it reads
 * and whether a Datastream column appears.
 *
 * IT ADDS NO NAVIGATION NODE. "This is not a project capability: no toggle, no
 * new tab, no new navigation node, nothing added to `page-structure.md`"
 * (:369-370). S1 is rendered below the field table of the Workbench `Map` tab —
 * because the value question only exists once the field question is answered —
 * and S2 above the table list of the `Value Tables` lens.
 *
 * THREE STATES, AND THE SCREEN INVENTS NONE OF THEIR SENTENCES. `measured`,
 * `unavailable` and `not_applicable` arrive from the server with the words a
 * person reads, exactly as `stage_relation_resolver` and
 * `collected_mapped_reader` already do it: a sentence written in a component is
 * a second answer free to disagree with the first. In particular a count that
 * could not be read prints `unknown` and never `0` (:398, :471).
 *
 * AND THE ROW ACTION IS TYPED, WHICH IS THE POINT OF THE THREE REASONS. The
 * server sends `action.pair_editor`; a row whose reason is `absent_at_source`
 * carries `false` and gets NO mapping control at all, only the step that repairs
 * it. "Offering a pair editor on a row no pair can repair is the defect this
 * typing exists to prevent" (:409).
 *
 * WHAT IS NOT BUILT IS NAMED, NEVER SILENTLY MISSING. The repair drawer (S3),
 * the proposal column and the import half of S4 do not exist yet; the controls
 * that would open them are rendered DISABLED with the server's own sentence
 * saying where the gesture lives today. The export half of S4 is real and
 * downloads the two-column file the importer already accepts.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  Panel,
  PanelHeader,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  formatPercent,
  wireWord,
} from "../ui";
import { ApiError, apiFetch, apiGet, apiPost } from "../lib/apiFetch";
import UnresolvedRepairDrawer from "./UnresolvedRepairDrawer";
import UnresolvedImportDialog from "./UnresolvedImportDialog";
import type { ValueMappingTableSummary } from "./UnresolvedRepairDrawer";

/** The closed vocabulary of the three reasons. Read from a map, never printed
 *  from the stored word: a screen that rendered `absent_at_source` would be
 *  speaking the database at somebody who came to repair a video. */
const REASON_LABEL: Record<string, string> = {
  absent_at_source: "Nothing arrived",
  unmapped: "No canonical name",
  no_reference: "Not in the reference",
};

const REASON_TONE: Record<string, "warning" | "error" | "neutral"> = {
  absent_at_source: "error",
  unmapped: "warning",
  no_reference: "warning",
};

export interface UnresolvedAction {
  kind: string;
  label: string | null;
  pair_editor: boolean;
}

export interface UnresolvedRow {
  dimension: string;
  source_value: string;
  connector: string | null;
  occurrences: number | null;
  row_share: number | null;
  metric_share: number | null;
  reason: string;
  repair: string | null;
  action: UnresolvedAction;
  datastream_id: string;
}

export interface UnresolvedGroup {
  key: string;
  datastream_id: string;
  datastream_name?: string | null;
  dimension: string;
  canonical_dimension: string;
  connector: string | null;
  state: "measured" | "unavailable" | "not_listable";
  reason: string | null;
  message: string | null;
  unresolved: number | null;
  observed_distinct: number | null;
  occurrences: number | null;
  window_rows: number | null;
  row_share: number | null;
  complete: boolean;
  truncated: boolean;
  by_reason: Record<string, number> | null;
  destination_table?: { table_id: string; table_name: string | null } | null;
  destination_state?: "known" | "unknown";
  values: UnresolvedRow[];
}

export interface UnresolvedEnvelope {
  schema: string;
  project_id: string;
  datastream_id: string | null;
  scope: "datastream" | "project";
  grouping: "dimension" | "destination_table";
  title: string;
  window: { start: string; end: string; days: number };
  state: "measured" | "unavailable" | "not_applicable";
  reason: string | null;
  message: string | null;
  summary: {
    unresolved: number | null;
    dimensions_measured: number;
    dimensions_listed: number;
    complete: boolean;
    by_reason: Record<string, number>;
  };
  groups: UnresolvedGroup[];
  ranking: { by: string; metric_share: null; note: string };
  repair_drawer: {
    available: boolean;
    note: string;
    /** The four modes, NAMED BY THE SERVER so the drawer cannot offer a fifth
     *  one or spell one of these differently. */
    match_modes: { value: string; label: string }[];
    /** WHAT MAY BE SAID AFTER A WRITE, and the three answers are the server's.
     *  A write into a value table is not a repair of this reading — the reading
     *  resolves the confirmed mappings — so both write surfaces take this same
     *  reading again and print the sentence matching what it answered. */
    after_write: {
      recheck: boolean;
      still_listed_note: string;
      cleared_note: string;
      unknown_note: string;
    };
  };
  proposals: { available: boolean; note: string };
  extract: { available: boolean; destination: string };
  import: {
    available: boolean;
    destination: string;
    write_mode: string;
    write_mode_note: string;
    /** S4 names TWO destinations; the one that is not served says so. */
    other_destination_note: string;
  };
  bounds: {
    dimensions_truncated: boolean;
    datastreams_opened: number | null;
    datastreams_total: number | null;
    datastreams_truncated: boolean;
  };
}

/** `unknown`, never `0`. The whole page exists because an unreadable mart and an
 *  empty one are two different reports. */
function count(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "unknown";
}

function share(value: number | null | undefined): string {
  // `unknown` is this page's word and stays here; the percentage is the
  // console's. An absent share is not a zero -- see `count` above.
  return typeof value === "number" ? formatPercent(value) : "unknown";
}

export default function UnresolvedValuesPanel({
  projectId,
  datastreamId,
  scope,
  onOpenDatastream,
}: {
  projectId: string;
  /** Present for S1, absent for S2. */
  datastreamId?: string;
  scope: "datastream" | "project";
  /** S2 only: every row deep-links to S1 on the Datastream that emitted the
   *  value — "one address per value, never two lists disagreeing about a count"
   *  (:427). The ADDRESS is built by the shell, never composed here: a second
   *  address grammar beside the router's has been paid for five times already. */
  onOpenDatastream?: (datastreamId: string) => void;
}) {
  const [payload, setPayload] = useState<UnresolvedEnvelope | null>(null);
  const [broken, setBroken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  /** S3 — the row the drawer is open on, and the group it belongs to. ONE at a
   *  time: "une question claire à la fois", and two open drawers would be two
   *  reaches computed against the same set. */
  const [repairing, setRepairing] = useState<{
    row: UnresolvedRow;
    group: UnresolvedGroup;
  } | null>(null);
  const [tables, setTables] = useState<ValueMappingTableSummary[]>([]);
  /** S4 — the file being brought back, per group. */
  const [importing, setImporting] = useState<UnresolvedGroup | null>(null);

  const address =
    scope === "datastream" && datastreamId
      ? `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/unresolved-values`
      : `/api/projects/${encodeURIComponent(projectId)}/unresolved-values`;

  /** Reads, renders, AND HANDS THE READING BACK. The drawer and the import
   *  dialog must not claim a value was repaired: they wait for this same
   *  reading, taken again, and say what it answered. `null` means the re-read
   *  did not happen, which is a third answer and never a success. */
  const load = useCallback(async (): Promise<UnresolvedEnvelope | null> => {
    setLoading(true);
    try {
      const body = await apiGet<UnresolvedEnvelope>(address);
      // A `200` CARRYING SOMETHING ELSE IS A FAILED READ, not a reading. Every
      // sentence below comes from the server, so a body without the envelope has
      // no sentence to render — and a panel that dereferenced it anyway would take
      // the whole tab down with it rather than say it could not read.
      if (!body || typeof body !== "object" || !body.window || !body.summary) {
        setPayload(null);
        setBroken("The answer did not carry an unresolved-values reading.");
        return null;
      }
      const read = { ...body, groups: body.groups ?? [] };
      setPayload(read);
      setBroken(null);
      return read;
    } catch (err) {
      setPayload(null);
      setBroken(err instanceof ApiError ? err.message : "The reading failed.");
      return null;
    } finally {
      setLoading(false);
    }
  }, [address]);

  useEffect(() => {
    void load();
  }, [load]);

  /** The tables a repair may write into. Read ONCE by the panel and handed to
   *  the drawer, rather than re-read on every open: two reads of the same list
   *  are two lists free to disagree while one drawer is up. */
  const loadTables = useCallback(async () => {
    try {
      const body = await apiGet<{ tables?: ValueMappingTableSummary[] }>(
        `/api/projects/${encodeURIComponent(projectId)}/value-mapping-tables`,
      );
      setTables(Array.isArray(body?.tables) ? body.tables : []);
    } catch {
      // A destination list that could not be read leaves the drawer with the
      // `Create a table` path, which is the gesture that still works.
      setTables([]);
    }
  }, [projectId]);

  useEffect(() => {
    void loadTables();
  }, [loadTables]);

  const download = useCallback(
    async (group: UnresolvedGroup) => {
      setExporting(group.key);
      setExportError(null);
      const path =
        `/api/projects/${encodeURIComponent(projectId)}/datastreams/` +
        `${encodeURIComponent(group.datastream_id)}/unresolved-values/extract` +
        `?dimension=${encodeURIComponent(group.dimension)}`;
      try {
        const response = await apiFetch(path);
        if (!response.ok) {
          setExportError(
            "The file could not be built, so nothing was downloaded. Reload the list and try again.",
          );
          return;
        }
        const blob = await response.blob();
        const href = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = href;
        anchor.download = `${group.dimension}-unresolved-pairs.csv`;
        anchor.click();
        URL.revokeObjectURL(href);
      } catch {
        setExportError(
          "The file could not be built, so nothing was downloaded. Reload the list and try again.",
        );
      } finally {
        setExporting(null);
      }
    },
    [projectId],
  );

  if (loading) {
    return (
      <Panel flush>
        <PanelHeader title="Values waiting to be mapped" description="Reading the window…" />
      </Panel>
    );
  }

  if (broken || !payload) {
    // BROKEN is not EMPTY. No list of any kind underneath, because a panel that
    // could not read must not look like a panel that read and found nothing.
    return (
      <Panel flush>
        <PanelHeader title="Values waiting to be mapped" />
        <div className="p-5">
          <Status as="block" tone="error" title="The unresolved values could not be read" data-testid="unresolved-broken"
          action={<Retry onClick={() => void loadTables()} />}
        >
            {broken ?? "The reading failed."} This is not a Datastream whose values all
            resolve.
          </Status>
        </div>
      </Panel>
    );
  }

  const { summary, window: read, groups } = payload;
  const headline =
    `${count(summary.unresolved)} across ${summary.dimensions_measured} ` +
    `${summary.dimensions_measured === 1 ? "dimension" : "dimensions"}`;

  /**
   * WHAT THE READING DID NOT OPEN — AND IT IS SAID WHETHER OR NOT THE LIST IS
   * EMPTY. Both bounds are what make the Project number SMALLER than the sum of
   * the Datastream numbers, which is exactly the divergence
   * `unresolved-values.md` refuses ("the Workbench and the Governance lens
   * answer with two different numbers for the same Project"). Until 2026-08-30
   * the Datastream bound was rendered INSIDE the `unresolved > 0` branch and the
   * dimension bound was rendered nowhere at all: a Project whose eight opened
   * streams were clean printed "Everything resolves" while a ninth stream's own
   * panel listed its gaps, and nothing on either screen said the ninth had not
   * been read. A bound that is only mentioned when the list is non-empty is a
   * bound stated exactly where it cannot mislead anybody.
   */
  const bounded =
    payload.state === "measured"
    && (payload.bounds.datastreams_truncated || payload.bounds.dimensions_truncated);

  return (
    <Panel flush>
      <PanelHeader
        title={`Values waiting to be mapped · ${headline}`}
        description={`Read over ${read.start} → ${read.end} (${read.days} days). ${payload.ranking.note}`}
      />

      {payload.state === "unavailable" && (
        <div className="p-5">
          {/* The mandated sentence, and NO table underneath it (:416). */}
          <Status as="block" tone="error" title="Unresolved values are unknown for this window" data-testid="unresolved-unavailable">
            {payload.message}
          </Status>
        </div>
      )}

      {payload.state === "not_applicable" && (
        <div className="p-5" data-testid="unresolved-not-applicable">
          <EmptyState
            title="No dimension to resolve yet"
            description={payload.message ?? ""}
          />
        </div>
      )}

      {bounded && (
        <div className="flex flex-col gap-3 px-5 pt-5">
          {payload.bounds.datastreams_truncated && (
            <Status as="block" tone="warning" title="Not every Datastream was opened" data-testid="unresolved-streams-bounded">
              {count(payload.bounds.datastreams_opened)} of{" "}
              {count(payload.bounds.datastreams_total)} Datastreams were read for this
              list. The rest were not measured, so this is not the whole Project — open
              a Datastream and read its own <span className="font-mono">Map</span> tab
              to see the values it carries.
            </Status>
          )}
          {payload.bounds.dimensions_truncated && (
            <Status as="block" tone="warning" title="Not every dimension was read" data-testid="unresolved-dimensions-bounded">
              More dimensions are declared here than one reading opens, so the ones past
              that bound were not read and none of their values is counted below. Nothing
              on this screen widens the reading; the bound is the reading&rsquo;s own.
            </Status>
          )}
        </div>
      )}

      {payload.state === "measured" && (summary.unresolved ?? 0) === 0 && (
        <div className="p-5" data-testid="unresolved-empty">
          {/* It says WHAT WAS MEASURED, not that nothing exists (:413). A sentence
              about the window is the difference between "we looked and everything
              landed" and "there is nothing here", which is the whole page.

              AND IT IS QUALIFIED WHEN THE READING WAS BOUNDED. "Everything
              resolves" over eight of forty-two Datastreams is the false green
              this surface exists to refuse: the title says what was read, and
              the bound above it says what was not. */}
          <EmptyState
            title={bounded ? "Everything that was read resolves" : "Everything resolves"}
            description={payload.message ?? ""}
          />
        </div>
      )}

      {payload.state === "measured" && (summary.unresolved ?? 0) > 0 && (
        <>
          {groups.map((group) => (
            <div key={group.key} className="border-t border-divider-base p-5" data-testid={`unresolved-group-${group.key}`}>
              <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="text-label text-text-secondary">
                  {payload.grouping === "destination_table"
                    ? (group.destination_table?.table_name ?? "No value table assigned")
                    : group.canonical_dimension}
                </span>
                <span className="font-mono text-ui">{group.dimension}</span>
                <span className="font-numeric text-caption text-text-secondary">
                  {count(group.unresolved)} unresolved of {count(group.observed_distinct)}{" "}
                  observed · {share(group.row_share)} of the window&rsquo;s rows
                </span>
              </div>

              {group.state !== "measured" && (
                <Status
                  as="block"
                  tone={group.state === "not_listable" ? "warning" : "error"}
                  title={
                    group.state === "not_listable"
                      ? "These values are classified and cannot be listed here"
                      : "This dimension could not be read"
                  }
                  data-testid={`unresolved-group-state-${group.key}`}
                >
                  {group.message}
                </Status>
              )}

              {group.state === "measured" && group.truncated && (
                <Status as="block" tone="warning" className="mb-3" title="This list stopped at its bound">
                  The heaviest values are shown; the cheap tail was not listed. The total
                  beside the dimension is still the true one.
                </Status>
              )}

              {group.state === "measured" && (
                <>
                  <TableScroll label={`Unresolved values of ${group.dimension}`}>
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Value</TableHead>
                          {payload.scope === "project" && <TableHead>Datastream</TableHead>}
                          <TableHead>Connector</TableHead>
                          <TableHead>Rows</TableHead>
                          <TableHead>Share of rows</TableHead>
                          <TableHead>Share of the metric</TableHead>
                          <TableHead>Why</TableHead>
                          <TableHead>Proposal</TableHead>
                          <TableHead>Action</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {group.values.map((row) => (
                          <TableRow key={`${group.key}::${row.source_value}`} data-testid={`unresolved-row-${row.source_value || "blank"}`}>
                            <TableCell className="font-mono">
                              {row.source_value === "" ? "(empty)" : row.source_value}
                            </TableCell>
                            {/* THE NAME IS THE SERVER'S ANSWER. The project-wide
                                read attaches `datastream_name` to every group it
                                emits, resolved from the same `_STREAMS_SQL` rows
                                it loops over, and `app.datastreams.name` is
                                `NOT NULL`. `?? row.datastream_id` was a second
                                resolution in the browser that could only fire if
                                the server had stopped answering -- and it would
                                then have put `ds_<ULID>` under a "Datastream"
                                heading rather than saying so. */}
                            {payload.scope === "project" && (
                              <TableCell>
                                {onOpenDatastream ? (
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    data-testid={`unresolved-open-${row.source_value || "blank"}`}
                                    onClick={() => onOpenDatastream(row.datastream_id)}
                                  >
                                    {group.datastream_name ?? "Unnamed Datastream"}
                                  </Button>
                                ) : (
                                  <span>{group.datastream_name ?? "Unnamed Datastream"}</span>
                                )}
                              </TableCell>
                            )}
                            <TableCell>{row.connector ?? "unknown"}</TableCell>
                            <TableCell className="font-numeric">{count(row.occurrences)}</TableCell>
                            <TableCell className="font-numeric">{share(row.row_share)}</TableCell>
                            {/* Not measured, and it says so rather than showing a
                                share nobody computed. */}
                            <TableCell className="text-text-secondary">not measured</TableCell>
                            <TableCell>
                              <Badge tone={REASON_TONE[row.reason] ?? "neutral"}>
                                {REASON_LABEL[row.reason] ?? wireWord(row.reason)}
                              </Badge>
                            </TableCell>
                            <TableCell className="text-text-secondary">—</TableCell>
                            <TableCell>
                              {row.action.pair_editor ? (
                                // S3 EXISTS DEPUIS LE 2026-08-22 : le geste que
                                // cet ecran reclame se fait ici, il ne renvoie
                                // plus ailleurs pour l'accomplir.
                                <Button
                                  size="sm"
                                  variant="secondary"
                                  data-testid={`unresolved-map-${row.source_value || "blank"}`}
                                  onClick={() => setRepairing({ row, group })}
                                >
                                  {row.action.label}
                                </Button>
                              ) : (
                                // NO mapping control at all — the step that repairs
                                // it, in the server's own words.
                                <span
                                  className="text-caption text-text-secondary"
                                  data-testid={`unresolved-repair-${row.source_value || "blank"}`}
                                >
                                  {row.repair}
                                </span>
                              )}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </TableScroll>

                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      data-testid={`unresolved-export-${group.key}`}
                      disabled={exporting === group.key}
                      onClick={() => void download(group)}
                    >
                      Export the missing rows
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      data-testid={`unresolved-import-${group.key}`}
                      onClick={() => setImporting(group)}
                    >
                      Import a filled file
                    </Button>
                    <span className="text-caption text-text-secondary">
                      The file carries the two columns the value-table importer accepts,
                      with the right-hand column empty. Only the rows a pair can repair
                      are written to it.
                    </span>
                  </div>
                </>
              )}
            </div>
          ))}

          {exportError && (
            <div className="px-5 pb-5">
              <Status as="block" tone="error" title="Nothing was downloaded" data-testid="unresolved-export-failed">
                {exportError}
              </Status>
            </div>
          )}

          {/* CE QUI RESTE ABSENT, dit une fois et depuis le serveur. La colonne
              de propositions est la seule moitie encore manquante ; le tiroir,
              lui, existe et sa note dit ce qu'il ne fait PAS (une regle est
              depliee en paires, pas conservee comme motif). Annoncer absent ce
              qui vient d'etre bati est le defaut symetrique de celui que ce bloc
              corrigeait. */}
          <div className="border-t border-divider-base p-5">
            <Status as="block" tone="info" title="What this panel cannot do yet" data-testid="unresolved-not-built">
              {payload.proposals.note}
            </Status>
          </div>
        </>
      )}

      {/* S3 — LE TIROIR, UN SEUL, MONTE PAR LE PANNEAU. Une par ligne serait
          autant de composants montes que de valeurs affichees, et deux ouverts
          en meme temps seraient deux portees calculees sur le meme ensemble. */}
      {repairing && payload && (
        <UnresolvedRepairDrawer
          projectId={projectId}
          row={repairing.row}
          group={repairing.group}
          payload={payload}
          tables={tables}
          onClose={() => setRepairing(null)}
          onRepaired={async () => {
            // LE PANNEAU RELIT, il ne rature pas sa propre liste : une liste
            // corrigee a la main et une lecture du serveur sont deux comptes
            // libres de diverger, ce que toute cette surface refuse.
            //
            // ET IL REND CETTE LECTURE au tiroir (2026-08-30). Le tiroir
            // annoncait « It applies at the next read of this window » sans que
            // rien ne l'ait mesure ; il attend desormais CETTE relecture-ci,
            // celle que la personne a sous les yeux, et dit ce qu'elle repond.
            void loadTables();
            return await load();
          }}
          onCreateTable={async (name) => {
            const created = await apiPost<ValueMappingTableSummary>(
              `/api/projects/${encodeURIComponent(projectId)}/value-mapping-tables`,
              { name, scope_level: "PROJECT", description: null },
            );
            await loadTables();
            return created;
          }}
        />
      )}

      {/* S4 — l'import, la meme classification que l'ecriture, vue avant elle. */}
      {importing && payload && (
        <UnresolvedImportDialog
          projectId={projectId}
          group={importing}
          payload={payload}
          tables={tables}
          onClose={() => setImporting(null)}
          onImported={async () => {
            void loadTables();
            return await load();
          }}
        />
      )}
    </Panel>
  );
}
