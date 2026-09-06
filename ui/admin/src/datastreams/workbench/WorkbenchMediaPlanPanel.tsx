/**
 * The media plan of a CARRIER Datastream — created here, revised here, dated.
 *
 * WHY IT IS ON THIS TAB AND NOWHERE ELSE. Ratified 2026-08-24 (commit
 * `a379ec50`), `analyze-and-test.md`, « Decided — the media plan is created and
 * imported in the carrier Datastream's Workbench »: the host screen for both
 * gestures is the Workbench of the Datastream that carries the plan, so that
 * plan creation and import live with the rest of that Datastream's file cycle.
 * The `Data` tab is where that cycle is already read — the last file that
 * arrived, the day it arrived, the ledger record — and a plan's versions ARE
 * this Datastream's dated imports.
 *
 * IT IS DRAWN ONLY FOR A CARRIER, and the server decides. `evidence.media_plan`
 * is `null` on every Datastream whose Template does not declare the plan-store
 * landing — the same declaration `import_runner` routes on, read from the same
 * sealed contract. Composing that verdict here would be a second answer to
 * "where does this file land", free to disagree with the engine's.
 *
 * NOTHING IS PROVISIONED. « no auto-provisioning on first import, and no single
 * managed per-project datastream owned by the mediaplan module »: this panel
 * creates a PLAN on a Datastream a person already made, never a Datastream. A
 * project holding a second plan makes a second file-source Datastream with its
 * own template — two agencies' spreadsheets are two templates — and the refusal
 * when a carrier is already taken says exactly that.
 *
 * A REVISION LANDS BESIDE, NEVER OVER. The import creates a CANDIDATE version;
 * publication stays the plan's own explicit act (`import_runner` refuses a
 * plan-store import that publishes). So the versions table keeps every one of
 * them with its date, and the confirmation after an import names the version the
 * file just became — the third clause of the ratified `Incomplete if`.
 *
 * IT INVENTS NO ROUTE. Creation is `POST /api/projects/{p}/mediaplans` (which
 * has taken a name and a currency since story 22.1, and takes the carrier since
 * migration 303); the import is `POST /api/datastreams/{id}/imports`, the
 * governed upload ingress every file of this Datastream already goes through —
 * receipt, quarantine, scan, ledger, then `run_import`. A plan imported by a
 * second path would be a second engine, which is what chantier 67-25 exists to
 * end.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { apiGet, apiPost } from "../../lib/apiFetch";
import { numberText, record, records, text } from "./evidence";
import {
  Badge,
  Button,
  EmptyState,
  Field,
  Input,
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
  Timestamp,
} from "../../ui";

type Row = Record<string, unknown>;

/** The dated revision an import just became — said once, in the words of the plan. */
type Landed = {
  versionNumber: string;
  createdAt: string;
  status: string;
};

function base(projectId: string, datastreamId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/workbench`;
}

/**
 * The file, as base64 — the shape `POST /api/datastreams/{id}/imports` takes.
 *
 * `FileReader` and not `Uint8Array` + `btoa`: a spreadsheet of a few megabytes
 * spread over `String.fromCharCode` blows the argument limit, and the browser
 * already knows how to do this without materialising a second copy.
 */
function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("This file could not be read."));
    reader.onload = () => {
      const result = String(reader.result ?? "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

export default function WorkbenchMediaPlanPanel({
  projectId,
  datastreamId,
  mediaPlan,
}: {
  projectId: string;
  datastreamId: string;
  /** `evidence.media_plan` — `null` when this Datastream carries no plan cycle. */
  mediaPlan: Row | null;
}) {
  const [evidence, setEvidence] = useState<Row | null>(mediaPlan);
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("EUR");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [landed, setLanded] = useState<Landed | null>(null);

  useEffect(() => setEvidence(mediaPlan), [mediaPlan]);

  const plan = record(evidence?.plan);
  const versions = useMemo(() => records(evidence?.versions), [evidence?.versions]);

  /**
   * A LOCAL RE-READ, not a route reload. Both gestures change this Datastream's
   * plan and nothing else on the Workbench; reloading the route would re-read
   * the header, the axes and the issue badge to redraw one table.
   */
  const reread = useCallback(async (): Promise<Row[]> => {
    const body = await apiGet<{ evidence?: Row }>(`${base(projectId, datastreamId)}/data`);
    const next = record((body?.evidence ?? {}).media_plan) ?? null;
    setEvidence(next);
    return records(next?.versions);
  }, [datastreamId, projectId]);

  if (evidence === null) return null;

  async function createPlan() {
    if (!name.trim()) {
      setFailure("Give the plan a name before creating it.");
      return;
    }
    setBusy(true);
    setFailure(null);
    try {
      await apiPost(`/api/projects/${encodeURIComponent(projectId)}/mediaplans`, {
        name: name.trim(),
        currency: currency.trim() || "EUR",
        // THE CARRIER IS THIS DATASTREAM, and it is sent rather than inferred:
        // the server refuses a carrier that already holds a live plan, and its
        // refusal names the second Datastream a second plan needs.
        carrier_datastream_id: datastreamId,
      });
      setName("");
      await reread();
    } catch (error: unknown) {
      setFailure(error instanceof Error ? error.message : "The plan could not be created.");
    } finally {
      setBusy(false);
    }
  }

  async function importRevision(file: File) {
    setBusy(true);
    setFailure(null);
    setLanded(null);
    // WHICH VERSIONS EXISTED BEFORE. The answer to "what did this file become"
    // is the version that was not here a moment ago — read from the plan's own
    // ledger rather than composed from a dispatch payload, so the sentence says
    // what the plan store actually holds.
    const before = new Set(versions.map((version) => text(version.id)));
    try {
      const file_base64 = await readAsBase64(file);
      await apiPost(`/api/datastreams/${encodeURIComponent(datastreamId)}/imports`, {
        project_id: projectId,
        // The key is the FILE, on this Datastream: sending the same spreadsheet
        // twice is one revision, not two, and the ingress replays it cleanly.
        idempotency_key: `workbench-mediaplan:${datastreamId}:${file.name}:${file.size}:${file.lastModified}`,
        filename: file.name,
        media_type: file.type || null,
        file_base64,
      });
      const after = await reread();
      const fresh = after.find((version) => !before.has(text(version.id)));
      if (fresh) {
        setLanded({
          versionNumber: numberText(fresh.version_number),
          createdAt: text(fresh.created_at, ""),
          status: text(fresh.status, "candidate"),
        });
      }
    } catch (error: unknown) {
      setFailure(error instanceof Error ? error.message : "This file could not be imported.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel flush data-testid="workbench-media-plan">
      <PanelHeader
        title="Media plan"
        description={
          plan
            ? text(evidence.revision_reason, "")
            : text(evidence.empty_reason, "")
        }
      />

      {failure && (
        <div className="px-5 pb-4">
          <Status as="block" tone="error" title="The media plan did not change">
            {failure}
          </Status>
        </div>
      )}

      {plan === null ? (
        <div className="p-5">
          {/* THE EMPTINESS SAYS WHY AND CARRIES THE GESTURE. It is the same rule
              the Pacing lens now follows, on the screen the decision made
              responsible: naming a gesture whose control is elsewhere is what
              this whole chantier was opened to stop. */}
          <EmptyState
            title={text(evidence.empty_message, "This Datastream carries no media plan yet")}
            description={text(evidence.empty_reason, "")}
            action={
              <div className="grid gap-3">
                <Field label="Plan name">
                  {(field) => (
                    <Input
                      {...field}
                      value={name}
                      placeholder="Q1 Brand"
                      data-testid="media-plan-name"
                      onChange={(event) => setName(event.target.value)}
                    />
                  )}
                </Field>
                <Field label="Currency">
                  {(field) => (
                    <Input
                      {...field}
                      value={currency}
                      data-testid="media-plan-currency"
                      onChange={(event) => setCurrency(event.target.value)}
                    />
                  )}
                </Field>
                <div>
                  <Button
                    disabled={busy}
                    data-testid="media-plan-create"
                    onClick={() => void createPlan()}
                  >
                    Create this media plan
                  </Button>
                </div>
              </div>
            }
          />
        </div>
      ) : (
        <div className="grid gap-4 p-5">
          <div className="flex flex-wrap items-center gap-3 text-ui">
            <span className="font-medium">{text(plan.name, text(plan.id))}</span>
            <Badge tone="neutral">{text(plan.currency, "")}</Badge>
          </div>

          {/* THE IMPORT, AND IT SAYS WHAT IT WILL BECOME BEFORE IT RUNS. The
              label names a REVISION rather than a replacement, because that is
              what the ratified model does: a dated import beside the previous
              ones, read as-of like any other file source. */}
          <label className="grid max-w-[52ch] gap-1 text-caption text-text-secondary">
            <span>Import a dated revision of this plan</span>
            <input
              type="file"
              accept=".xlsx,.xls,.csv"
              disabled={busy}
              data-testid="media-plan-import"
              aria-label="Import a dated revision of this plan"
              onChange={(event) => {
                const file = event.target.files?.[0];
                // The input is cleared so re-choosing the SAME file fires again:
                // a person correcting a spreadsheet and re-sending it under the
                // same name must not meet a control that silently does nothing.
                event.target.value = "";
                if (file) void importRevision(file);
              }}
            />
          </label>

          {/* WHICH DATED VERSION IT JUST BECAME — clause 3 of the ratified
              `Incomplete if`. Not "imported", not a row count: the version
              number, the day it carries and the fact that it is a candidate,
              because a person who believed an import had taken effect would be
              reading a state nobody reached. */}
          {landed && (
            <Status
              as="block"
              tone="success"
              title={`This file became version ${landed.versionNumber} of this plan`}
              data-testid="media-plan-landed"
            >
              <span className="grid gap-2">
                <span>
                  {`Version ${landed.versionNumber}, imported ${landed.createdAt.slice(0, 10)}, ${landed.status}. The versions below are unchanged — it landed beside them.`}
                </span>
                <span className="text-text-secondary">
                  {text(evidence.candidate_reason, "")}
                </span>
              </span>
            </Status>
          )}

          {versions.length === 0 ? (
            <EmptyState
              title={text(evidence.no_version_message, "No file has been imported into this plan yet")}
              description={text(evidence.revision_reason, "")}
            />
          ) : (
            <TableScroll label="Dated versions of this media plan">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Version</TableHead>
                    <TableHead>Imported</TableHead>
                    <TableHead>From</TableHead>
                    <TableHead>State</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {versions.map((version) => (
                    <TableRow key={text(version.id)}>
                      <TableCell>{numberText(version.version_number)}</TableCell>
                      <TableCell>
                        <Timestamp value={text(version.created_at)} />
                      </TableCell>
                      <TableCell>{text(version.source_note, "—")}</TableCell>
                      <TableCell>
                        {/* `is_active` AND `status` ARE TWO FACTS. A published
                            version that is not the active one still exists and
                            is still readable as of its own day; drawing only
                            one of the two would hide which reading Analyze
                            uses. */}
                        <Badge tone={version.is_active === true ? "success" : "neutral"}>
                          {version.is_active === true
                            ? "In use"
                            : text(version.status, "candidate")}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}
        </div>
      )}
    </Panel>
  );
}
